import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from agi_wind.agent.settings import AgentSettings
from agi_wind.core.settings import ProactiveSettings, Settings
from agi_wind.core.types import ModelRef
from agi_wind.delivery.idle_cognition import IdleCognitionService
from agi_wind.delivery.proactive import ProactiveService
from agi_wind.delivery.service import GenerationService
from agi_wind.delivery.models import idle_cognition_runs
from agi_wind.archive import models as m

UTC = timezone.utc


class FakeProvider:
    def __init__(self, reply="你好", script=None):
        self.reply = reply
        self.script = list(script or [])
        self.complete_calls = 0
        self.complete_messages = []

    async def stream(self, *args):
        yield self.reply

    async def complete(self, model, messages):
        self.complete_calls += 1
        self.complete_messages.append(messages)
        if self.script:
            return self.script.pop(0)
        return json.dumps({"thought": "想一下", "steps": ["回忆刚才的话"], "action": "silent"})


def cognition(reply=None, script=None):
    return FakeProvider(reply=reply or "我在呢。", script=script)


def make_service(db, provider, settings, clock):
    service = GenerationService(db, provider, model=ModelRef(provider_id="fake", model_id="m"))
    service.agent_settings = lambda: AgentSettings(enabled=False)
    service.memory_extraction_enabled = False
    proactive = ProactiveService(db, settings, service)
    idle = IdleCognitionService(db, settings, service, proactive=proactive, now_fn=lambda: clock[0])
    proactive.idle_cognition = idle
    service.idle_cognition = idle
    return service, proactive, idle


def active_settings(**overrides):
    settings = Settings()
    values = dict(enabled=True, min_idle_minutes=120, min_gap_minutes=180, max_per_day=5,
                  quiet_start="00:00", quiet_end="00:00", after_reply_enabled=True,
                  after_reply_delay_seconds=30, after_reply_max_rounds=2,
                  after_reply_max_steps=4, after_reply_max_runs_per_day=6)
    values.update(overrides)
    settings.proactive = ProactiveSettings(**values)
    return settings


async def run_turn(db, actor, scope, chat_request_factory, service):
    accepted = await service.submit(actor, chat_request_factory(text="在吗", conversation_id=scope.conversation_id))
    assert await service.run(accepted.generation_id)
    return accepted


async def test_after_reply_cognition_waits_then_reaches_out(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=UTC)]
    provider = cognition(script=[json.dumps({
        "thought": "他可能忙了，问问看",
        "steps": ["回忆上一条", "决定主动开口"],
        "action": "reach_out",
        "message": "你还在吗？我刚想到一件事。",
    })])
    service, proactive, idle = make_service(db, provider, active_settings(), clock)
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    runs = db.all("idle_cognition_runs")
    assert len(runs) == 1 and runs[0]["state"] == "pending"
    assert runs[0]["due_at"] == clock[0] + timedelta(seconds=30)
    assert provider.complete_calls == 0
    clock[0] += timedelta(seconds=31)
    processed = await idle.tick()
    assert processed == 1
    run = db.all("idle_cognition_runs")[0]
    assert run["state"] == "sent" and run["decision"] == "reach_out"
    assert run["rounds_used"] == 1 and run["steps_used"] == 1
    assert run["message_event_id"]
    assert run['steps'] == [{'round':1,'step':1,'kind':'model'}]
    messages = [row["content"] for row in db.all("events", role="assistant") if row["content"].get("proactive")]
    assert messages and messages[0]["text"] == "你还在吗？我刚想到一件事。"
    assert db.count("proactive_messages") == 1
    live = {row["type"] for row in db.all("conversation_live_events")}
    assert {"idle_cognition.started", "idle_cognition.completed", "proactive.message"} <= live
    assert 'idle_cognition.step' not in live  # complete-only fixture has no native reasoning


async def test_silent_decision_does_not_send_a_message(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=UTC)]
    provider = cognition(script=[json.dumps({
        "thought": "现在没有什么想说的", "steps": ["整理了一下"], "action": "silent", "message": "",
    })])
    service, proactive, idle = make_service(db, provider, active_settings(), clock)
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    run = db.all("idle_cognition_runs")[0]
    assert run["state"] == "silent" and run["decision"] == "silent"
    assert db.count("proactive_messages") == 0
    assert not [row for row in db.all("events", role="assistant") if row["content"].get("proactive")]


async def test_user_reply_cancels_pending_cognition_before_due(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=UTC)]
    provider = cognition()
    service, proactive, idle = make_service(db, provider, active_settings(), clock)
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    with db.engine.begin() as conn:
        conn.execute(m.events.insert().values(
            account_id=actor.account_id, companion_id=seeded_scope.companion_id,
            conversation_id=seeded_scope.conversation_id, role="user", kind="user_input",
            content={"text": "我回来了"}, content_sha256="x" * 64,
        ))
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    run = db.all("idle_cognition_runs")[0]
    assert run["state"] == "cancelled" and run["reason"] == "conversation_changed"
    assert provider.complete_calls == 0


async def test_quiet_hours_skip_without_model_call(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 15, 30, tzinfo=UTC)]  # 23:30 Asia/Shanghai
    settings = active_settings(quiet_start="23:00", quiet_end="08:00")
    provider = cognition()
    service, proactive, idle = make_service(db, provider, settings, clock)
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    run = db.all("idle_cognition_runs")[0]
    assert run["state"] == "skipped" and run["reason"] == "quiet_hours"
    assert provider.complete_calls == 0


async def test_continue_uses_a_second_round_and_respects_step_budget(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=UTC)]
    provider = cognition(script=[
        json.dumps({"thought": "再想想", "steps": ["第一步", "第二步"], "action": "continue", "message": ""}),
        json.dumps({"thought": "想好了", "steps": ["第三步"], "action": "reach_out", "message": "突然想问你个事。"}),
    ])
    service, proactive, idle = make_service(db, provider, active_settings(), clock)
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    run = db.all("idle_cognition_runs")[0]
    assert provider.complete_calls == 2
    assert run["state"] == "sent" and run["rounds_used"] == 2 and run["steps_used"] == 2
    assert [step["step"] for step in run["steps"]] == [1, 1]
    assert any(row["content"].get("proactive") for row in db.all("events", role="assistant"))


async def test_invalid_json_fails_without_message(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=UTC)]
    provider = cognition(script=["这不是 JSON"])
    service, proactive, idle = make_service(db, provider, active_settings(), clock)
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    run = db.all("idle_cognition_runs")[0]
    assert run["state"] == "failed" and run["error_code"] == "COGNITION_INVALID_OUTPUT"
    assert db.count("proactive_messages") == 0


async def test_generation_persists_rounds_and_steps_metadata(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=UTC)]
    provider = cognition(reply="在的。")
    service, proactive, idle = make_service(db, provider, active_settings(), clock)
    accepted = await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    output = next(row for row in db.all("events", role="assistant")
                  if row["generation_id"] == accepted.generation_id)
    assert output["content"]["cognition"]["rounds"] == 1
    assert output["content"]["cognition"]["steps"] >= 1
    cognition_events = [row for row in db.all("generation_stream_events") if row["type"] == "cognition.completed"]
    assert cognition_events and cognition_events[0]["payload"]["rounds"] == 1
