import httpx
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from agi_wind.archive import models as m
from agi_wind.archive.repository import digest
from agi_wind.core.settings import ProactiveSettings, Settings
from agi_wind.core.types import ModelRef
from agi_wind.delivery import models as dm
from agi_wind.delivery.proactive import ProactiveService, in_quiet_hours
from agi_wind.delivery.service import GenerationService
from test_client import client_app, connect


class ProactiveProvider:
    def __init__(self, text="[情绪:温和]\n今天风有点大，你那边呢？"):
        self.text = text
        self.calls = []

    async def stream(self, model, messages):
        self.calls.append(messages)
        yield self.text


def old_user_event(db, actor, scope, occurred_at):
    with db.engine.begin() as conn:
        conn.execute(
            m.events.insert().values(
                account_id=actor.account_id,
                companion_id=scope.companion_id,
                conversation_id=scope.conversation_id,
                role="user",
                kind="user_input",
                content={"text": "我先忙一会儿"},
                content_sha256=digest({"text": "我先忙一会儿"}),
                occurred_at=occurred_at,
                recorded_at=occurred_at,
            )
        )


def service_with_provider(db, provider, settings):
    service = GenerationService(db, provider, model=ModelRef(provider_id="fake", model_id="proactive"))
    return ProactiveService(db, settings, service)


def active_settings(**overrides):
    settings = Settings()
    settings.proactive = ProactiveSettings(enabled=True, **overrides)
    return settings


def test_quiet_hours_wrap_and_no_window():
    tz = ZoneInfo("Asia/Shanghai")
    assert in_quiet_hours(datetime(2026, 10, 25, 23, 30, tzinfo=tz), "23:00", "08:00")
    assert in_quiet_hours(datetime(2026, 10, 25, 7, 30, tzinfo=tz), "23:00", "08:00")
    assert not in_quiet_hours(datetime(2026, 10, 25, 12, 0, tzinfo=tz), "23:00", "08:00")
    assert not in_quiet_hours(datetime(2026, 10, 25, 12, 0, tzinfo=tz), "08:00", "08:00")


async def test_due_rejects_quiet_hours(db, actor, seeded_scope):
    settings = active_settings(min_idle_minutes=5)
    service = service_with_provider(db, ProactiveProvider(), settings)
    tz = ZoneInfo("Asia/Shanghai")
    service._now_fn = lambda: datetime(2026, 10, 25, 23, 30, tzinfo=tz)
    old_user_event(db, actor, seeded_scope, datetime(2026, 10, 25, 20, 0, tzinfo=tz))
    ok, reason = service.due(actor, seeded_scope.companion_id)
    assert ok is False and reason == "quiet_hours"


async def test_due_rejects_daily_cap(db, actor, seeded_scope):
    settings = active_settings(min_idle_minutes=5, max_per_day=1)
    service = service_with_provider(db, ProactiveProvider(), settings)
    tz = ZoneInfo("Asia/Shanghai")
    service._now_fn = lambda: datetime(2026, 10, 25, 14, 0, tzinfo=tz)
    old_user_event(db, actor, seeded_scope, datetime(2026, 10, 25, 10, 0, tzinfo=tz))
    service._sent_today = lambda *args: 1
    ok, reason = service.due(actor, seeded_scope.companion_id)
    assert ok is False and reason == "daily_cap"


async def test_proactive_send_archives_visible_message_without_user_turn(db, actor, seeded_scope):
    # Equal quiet start/end disables the quiet window so this test does not depend on the wall clock.
    settings = active_settings(min_idle_minutes=30, min_gap_minutes=5, max_per_day=3, quiet_start="00:00", quiet_end="00:00")
    provider = ProactiveProvider()
    service = service_with_provider(db, provider, settings)
    old_user_event(db, actor, seeded_scope, datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(hours=3))
    user_before = db.count("events")
    result = await service.send(actor, seeded_scope.companion_id)
    assert result["sent"] is True
    assert result["text"] == "今天风有点大，你那边呢？"
    user_after = db.count("events")
    assert user_after == user_before + 1  # only the assistant message, no fake user turn
    content = db.all("events", role="assistant")[0]["content"]
    assert content["text"] == "今天风有点大，你那边呢？"
    assert content["proactive"] is True
    assert db.count("proactive_messages") == 1
    live = db.all("conversation_live_events")
    assert any(row["type"] == "proactive.message" for row in live)
    assert provider.calls and "主动消息" in provider.calls[0][0]["content"]
    assert "[情绪:" not in str(content)


async def test_proactive_force_skips_idle_but_requires_model(db, actor, seeded_scope):
    settings = active_settings(min_idle_minutes=999)
    service = service_with_provider(db, ProactiveProvider(), settings)
    old_user_event(db, actor, seeded_scope, datetime.now(ZoneInfo("Asia/Shanghai")))
    blocked = await service.send(actor, seeded_scope.companion_id)
    assert blocked["sent"] is False
    forced = await service.send(actor, seeded_scope.companion_id, force=True)
    assert forced["sent"] is True


async def test_proactive_settings_api_and_manual_send(db, actor, seeded_scope, tmp_path):
    provider = ProactiveProvider()
    app = client_app(db, actor, tmp_path, provider=provider)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        _, csrf = await connect(client)
        view = (await client.get("/api/v1/proactive/settings", params={"companion_id": str(seeded_scope.companion_id)})).json()
        assert view["enabled"] is False
        headers = {"Origin": "http://test", "X-AGI-CSRF": csrf}
        response = await client.patch(
            "/api/v1/proactive/settings",
            json={"enabled": True, "min_idle_minutes": 30, "quiet_start": "23:00", "quiet_end": "08:00"},
            headers=headers,
        )
        assert response.status_code == 200 and response.json()["enabled"] is True
        sent = await client.post(
            "/api/v1/proactive/send",
            json={"companion_id": str(seeded_scope.companion_id)},
            headers=headers,
        )
        assert sent.status_code == 200 and sent.json()["sent"] is True
        history = (
            await client.get(f"/api/v1/conversations/{seeded_scope.conversation_id}/events")
        ).json()
        assert any((item.get("content") or {}).get("proactive") for item in history)
        bad = await client.patch("/api/v1/proactive/settings", json={"quiet_start": "25:00"}, headers=headers)
        assert bad.status_code == 422
