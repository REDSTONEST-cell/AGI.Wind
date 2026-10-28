import time
from uuid import uuid4

import httpx
import pytest
from test_client import client_app

from nagi.archive import models as m
from nagi.archive.deletions import DeletionLedger
from nagi.integrations.qq import QQSettings, TOKEN_REF
from nagi.integrations.qq_groups import ensure_group_binding
from nagi.providers.protocol import ProviderChunk

BOT = "10002"
OWNER = "10001"
GROUP = "123456789"


class Provider:
    def __init__(self, text="你好。\n今天怎么样？"):
        self.calls = 0
        self.text = text

    async def stream(self, *args):
        self.calls += 1
        yield ProviderChunk(kind="reasoning", text="synthetic-group-reasoning")
        yield self.text


def group_event(message_id, *, group_id=GROUP, sender_qq="736357841", text="你好",
                mention=True, reply_to=None, card="小明", nickname="小明"):
    segments = []
    if reply_to is not None:
        segments.append({"type": "reply", "data": {"id": str(reply_to)}})
    if mention:
        segments.append({"type": "at", "data": {"qq": BOT}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "self_id": int(BOT), "group_id": int(group_id), "user_id": int(sender_qq),
        "message_id": message_id,
        "sender": {"user_id": int(sender_qq), "nickname": nickname, "card": card, "role": "member"},
        "message": segments,
    }


def configure(app, seeded_scope, provider, groups=(GROUP,), trigger="smart"):
    app.state.settings.qq = QQSettings(enabled=True, owner_qq=OWNER, bot_qq=BOT,
        conversation_id=seeded_scope.conversation_id, reply_delay_seconds=0,
        group_enabled=True, allowed_groups=list(groups), group_trigger_mode=trigger)
    token = "synthetic-qq-token-32-characters-long"
    app.state.settings_store.set(TOKEN_REF, token)
    DeletionLedger(app.state.settings.data_dir / "governance").initialize()
    return token


def echo(ws, action, message_id):
    ws.send_json({"status": "ok", "retcode": 0, "data": {"message_id": message_id}, "echo": action["echo"]})


async def test_group_mention_opens_persistent_window_and_reuses_archived_event(db, actor, seeded_scope, tmp_path):
    provider = Provider()
    app = client_app(db, actor, tmp_path, provider)
    app.state.service.memory_extraction_enabled = False
    app.state.settings.memory_extraction_enabled = False
    app.state.settings.memory_maintenance_enabled = False
    token = configure(app, seeded_scope, provider)
    event = group_event(1)
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/integrations/qq/ws", headers={
                "X-Self-ID": BOT, "Authorization": "Bearer " + token}) as ws:
            ws.send_json(event)
            action = ws.receive_json()
            assert action["action"] == "send_group_msg"
            assert action["params"]["group_id"] == int(GROUP)
            assert action["params"]["message"][0]["data"]["text"] == "你好"
            echo(ws, action, 9001)
            # Duplicate platform report must not archive or generate twice.
            ws.send_json(event)
            time.sleep(0.3)
            assert provider.calls == 1
    group_conversations = [row for row in db.all("conversations") if row["channel_kind"] == "group"]
    assert len(group_conversations) == 1
    binding = db.all("channel_bindings")[0]
    assert binding["conversation_id"] == group_conversations[0]["id"]
    assert binding["external_chat_id"] == GROUP
    principal = db.all("external_principals")[0]
    assert principal["is_owner"] is False and principal["card_name"] == "小明"
    # The owner's private window is not archived when a group room is created.
    assert db.all("conversations", id=seeded_scope.conversation_id)[0]["state"] == "active"
    assert db.count("requests") == 1
    group_events = db.all("events", conversation_id=group_conversations[0]["id"])
    user_events = [row for row in group_events if row["role"] == "user"]
    assert len(user_events) == 1 and user_events[0]["external_message_id"] == "1"
    assert user_events[0]["request_id"] is not None and user_events[0]["sender_principal_id"] == principal["id"]
    assert any(row["role"] == "assistant" for row in group_events)
    assert db.count("qq_message_receipts") >= 1


async def test_group_follow_up_without_mention_but_not_after_expiry(db, actor, seeded_scope, tmp_path):
    from nagi.integrations.qq_attention import GroupAttention
    provider = Provider("好呀")
    app = client_app(db, actor, tmp_path, provider)
    app.state.service.memory_extraction_enabled = False
    app.state.settings.memory_extraction_enabled = False
    app.state.settings.memory_maintenance_enabled = False
    token = configure(app, seeded_scope, provider)
    clock = [0.0]
    app.state.qq_bridge.attention = GroupAttention(followup_seconds=90, max_rounds=4, clock=lambda: clock[0])
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/integrations/qq/ws", headers={
                "X-Self-ID": BOT, "Authorization": "Bearer " + token}) as ws:
            ws.send_json(group_event(10))
            action = ws.receive_json()
            assert action["action"] == "send_group_msg"
            echo(ws, action, 9101)
            ws.send_json(group_event(11, mention=False, text="那明天呢"))
            action = ws.receive_json()
            assert action["action"] == "send_group_msg"
            echo(ws, action, 9102)
            time.sleep(0.4)  # let the successful send mark the short reply window
            assert provider.calls == 2
            clock[0] = 10000
            ws.send_json(group_event(12, mention=False, text="为什么"))
            time.sleep(0.3)
    assert db.count("requests") == 2


async def test_quote_of_bot_second_segment_is_a_strong_signal(db, actor, seeded_scope, tmp_path):
    provider = Provider("第一段。\n第二段？")
    app = client_app(db, actor, tmp_path, provider)
    app.state.service.memory_extraction_enabled = False
    app.state.settings.memory_extraction_enabled = False
    app.state.settings.memory_maintenance_enabled = False
    token = configure(app, seeded_scope, provider)
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/integrations/qq/ws", headers={
                "X-Self-ID": BOT, "Authorization": "Bearer " + token}) as ws:
            ws.send_json(group_event(20))
            first = ws.receive_json()
            echo(ws, first, 9201)
            second = ws.receive_json()
            echo(ws, second, 9202)
            assert provider.calls == 1
            ws.send_json(group_event(21, mention=False, reply_to=9202, text="这个是什么意思"))
            action = ws.receive_json()
            assert action["action"] == "send_group_msg"
            echo(ws, action, 9203)
    assert db.count("requests") == 2


async def test_unlisted_group_is_ignored_before_any_archive(db, actor, seeded_scope, tmp_path):
    provider = Provider()
    app = client_app(db, actor, tmp_path, provider)
    app.state.service.memory_extraction_enabled = False
    app.state.settings.memory_extraction_enabled = False
    app.state.settings.memory_maintenance_enabled = False
    token = configure(app, seeded_scope, provider)
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/integrations/qq/ws", headers={
                "X-Self-ID": BOT, "Authorization": "Bearer " + token}) as ws:
            ws.send_json(group_event(30, group_id="987654321"))
            time.sleep(0.3)
            ws.send_json(group_event(31))
            action = ws.receive_json()
            assert action["action"] == "send_group_msg"
            echo(ws, action, 9301)
    assert db.count("channel_bindings") == 1
    assert db.all("channel_bindings")[0]["external_chat_id"] == GROUP


async def test_observed_group_message_is_archived_and_queues_coalesced_member_memory(db, actor, seeded_scope, tmp_path):
    from nagi.memory.jobs import MEMBER_EXTRACT
    provider = Provider()
    app = client_app(db, actor, tmp_path, provider)
    app.state.service.memory_extraction_enabled = True
    app.state.settings.memory_extraction_enabled = False
    app.state.settings.memory_maintenance_enabled = False
    token = configure(app, seeded_scope, provider)
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/integrations/qq/ws", headers={
                "X-Self-ID": BOT, "Authorization": "Bearer " + token}) as ws:
            ws.send_json(group_event(50, mention=False, text="今天天气不错"))
            time.sleep(0.4)
    assert db.count("requests") == 0
    assert db.count("events") == 1
    jobs = db.all("outbox", event_type=MEMBER_EXTRACT)
    assert len(jobs) == 1 and jobs[0]["state"] == "pending"
    assert jobs[0]["payload"]["observed"] is True and jobs[0]["aggregate_id"]
    assert jobs[0]["available_at"] > m.now()


async def test_group_reply_channel_and_member_identity_survive_restart(db, actor, seeded_scope, tmp_path):
    provider = Provider()
    from fastapi.testclient import TestClient

    def one_round(message_id, reply_id):
        app = client_app(db, actor, tmp_path, provider)
        app.state.service.memory_extraction_enabled = False
        app.state.settings.memory_extraction_enabled = False
        app.state.settings.memory_maintenance_enabled = False
        token = configure(app, seeded_scope, provider)
        with TestClient(app) as client:
            with client.websocket_connect("/api/v1/integrations/qq/ws", headers={
                    "X-Self-ID": BOT, "Authorization": "Bearer " + token}) as ws:
                ws.send_json(group_event(message_id))
                action = ws.receive_json()
                assert action["action"] == "send_group_msg"
                echo(ws, action, reply_id)

    one_round(40, 9401)
    one_round(41, 9402)
    assert db.count("channel_bindings") == 1
    assert len([row for row in db.all("conversations") if row["channel_kind"] == "group"]) == 1
    assert db.count("external_principals") == 1
    assert db.count("requests") == 2


async def test_per_group_toggle_updates_allowlist_binding_and_listing(db, actor, seeded_scope, tmp_path):
    provider = Provider()
    app = client_app(db, actor, tmp_path, provider)
    app.state.service.memory_extraction_enabled = False
    app.state.settings.memory_extraction_enabled = False
    app.state.settings.memory_maintenance_enabled = False
    configure(app, seeded_scope, provider)
    binding = ensure_group_binding(db, actor, companion_id=seeded_scope.companion_id,
                                   bot_qq=BOT, group_id=GROUP, title="测试群")
    headers = {"Authorization": "Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        disabled = await client.patch(f"/api/v1/integrations/qq/groups/{binding['id']}",
                                      json={"enabled": False}, headers=headers)
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["allowed_groups"] == []
        assert app.state.settings.qq.allowed_groups == []
        assert db.all("channel_bindings", id=binding["id"])[0]["enabled"] is False
        rooms = (await client.get("/api/v1/integrations/qq/groups", params={
            "companion_id": str(seeded_scope.companion_id)}, headers=headers)).json()
        assert rooms[0]["enabled"] is False
        enabled = await client.patch(f"/api/v1/integrations/qq/groups/{binding['id']}",
                                     json={"enabled": True}, headers=headers)
        assert enabled.status_code == 200
        assert GROUP in enabled.json()["allowed_groups"]
        assert db.all("channel_bindings", id=binding["id"])[0]["enabled"] is True
        rooms = (await client.get("/api/v1/integrations/qq/groups", params={
            "companion_id": str(seeded_scope.companion_id)}, headers=headers)).json()
        assert rooms[0]["enabled"] is True
        missing = await client.patch(f"/api/v1/integrations/qq/groups/{uuid4()}",
                                     json={"enabled": False}, headers=headers)
        assert missing.status_code == 404
