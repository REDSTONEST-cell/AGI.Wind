import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nagi.integrations import qq as qq_module
from nagi.integrations.qq import QQBridge, QQSettings, private_message


def qq_settings(**overrides):
    values = dict(enabled=True, owner_qq="10001", bot_qq="10002",
                  conversation_id=uuid4(), reply_delay_seconds=0)
    values.update(overrides)
    return QQSettings(**values)


def private_event(message_id, text):
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 10002, "user_id": 10001, "message_id": message_id,
        "time": __import__("time").time(),
        "message": [{"type": "text", "data": {"text": text}}],
    }


def private_image_event(message_id):
    return {
        "post_type": "message", "message_type": "private", "sub_type": "friend",
        "self_id": 10002, "user_id": 10001, "message_id": message_id,
        "time": __import__("time").time(),
        "message": [{"type": "image", "data": {"url": "https://example.test/a.png"}}],
    }


def test_outbound_pacing_uses_a_log_curve_and_stays_bounded():
    bridge = QQBridge(None, SimpleNamespace(qq=qq_settings()), None, None, None, None, None)
    policy = qq_settings(reply_delay_seconds=1.0, reply_min_gap_seconds=1.8,
                         reply_max_delay_seconds=8)
    short = bridge._typing_delay({"type": "text", "data": {"text": "好"}}, policy)
    long = bridge._typing_delay({"type": "text", "data": {"text": "这是一段比较长的回复，" * 12}}, policy)
    assert 0 < short < long <= 8
    image = bridge._typing_delay({"type": "image", "data": {}}, policy)
    assert image >= 2


def test_zero_base_delay_disables_pacing_for_deterministic_tests():
    bridge = QQBridge(None, SimpleNamespace(qq=qq_settings()), None, None, None, None, None)
    policy = qq_settings(reply_delay_seconds=0)
    assert bridge._typing_delay({"type": "text", "data": {"text": "hello"}}, policy) == 0


async def test_pause_enforces_the_minimum_gap_and_first_segment_cap(monkeypatch):
    bridge = QQBridge(None, SimpleNamespace(qq=qq_settings()), None, None, None, None, None)
    policy = qq_settings(reply_delay_seconds=1.0, reply_min_gap_seconds=1.8)
    waits = []

    async def fake_sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(qq_module.asyncio, "sleep", fake_sleep)
    bridge._last_send_at = 0
    await bridge._pause_before_send({"type": "text", "data": {"text": "长" * 80}}, policy, first=True)
    bridge._last_send_at = asyncio.get_running_loop().time()
    await bridge._pause_before_send({"type": "image", "data": {}}, policy)
    assert waits and all(value > 0 for value in waits)
    assert waits[0] <= 2.5


def test_debounce_settings_validate_quiet_window():
    with pytest.raises(ValidationError):
        QQSettings(enabled=True, owner_qq="10001", bot_qq="10002",
                   conversation_id=uuid4(), debounce_quiet_seconds=9,
                   debounce_max_wait_seconds=2)


async def test_bridge_queues_one_batch_for_rapid_private_fragments(db, actor):
    policy = qq_settings(debounce_enabled=True, debounce_quiet_seconds=0.2,
                         debounce_max_wait_seconds=0.6, debounce_final_grace_seconds=0.05)
    settings = SimpleNamespace(qq=policy, data_dir=None)
    service = SimpleNamespace(db=db)
    bridge = QQBridge(None, settings, None, service, None, actor, None)
    bridge.connection = SimpleNamespace(connected=True)
    try:
        await bridge.receive_message(private_event(1, "我今天去开会了"))
        await bridge.receive_message(private_event(2, "然后甲方又改需求"))
        await asyncio.sleep(0.4)
        assert bridge.queue.qsize() == 1
        batch = bridge.queue.get_nowait()
        assert batch["kind"] == "private_batch"
        assert batch["text"] == "我今天去开会了\n然后甲方又改需求"
        expected = private_message(private_event(1, "x"), policy)["request_id"]
        assert batch["messages"][0]["request_id"] == expected
    finally:
        bridge.connection = None
        await bridge.debouncer.close()


async def test_stop_clears_pending_private_fragments(db, actor):
    policy = qq_settings(debounce_enabled=True, debounce_quiet_seconds=0.2)
    settings = SimpleNamespace(qq=policy, data_dir=None)
    bridge = QQBridge(None, settings, None, SimpleNamespace(db=db), None, actor, None)
    bridge.connection = SimpleNamespace(connected=True)
    try:
        await bridge.receive_message(private_event(1, "然后"))
        assert bridge.debouncer.pending_count() == 1
        await bridge.receive_message(private_event(2, "停止"))
        assert bridge.debouncer.pending_count() == 0
        await asyncio.sleep(0.05)
        assert bridge.queue.empty()
    finally:
        bridge.connection = None
        await bridge.debouncer.close()


async def test_non_text_boundary_flushes_pending_text_first(db, actor):
    policy = qq_settings(debounce_enabled=True, debounce_quiet_seconds=0.5)
    settings = SimpleNamespace(qq=policy, data_dir=None)
    bridge = QQBridge(None, settings, None, SimpleNamespace(db=db), None, actor, None)
    bridge.connection = SimpleNamespace(connected=True)
    try:
        await bridge.receive_message(private_event(1, "然后"))
        await bridge.receive_message(private_image_event(2))
        first = bridge.queue.get_nowait()
        second = bridge.queue.get_nowait()
        assert first["kind"] == "private_batch"
        assert first["messages"][0]["request_id"] == private_message(private_event(1, "x"), policy)["request_id"]
        assert second["images"] == ["https://example.test/a.png"]
    finally:
        bridge.connection = None
        await bridge.debouncer.close()


async def test_private_batch_becomes_one_chat_request(db, actor, seeded_scope):
    policy = qq_settings(conversation_id=seeded_scope.conversation_id)
    settings = SimpleNamespace(qq=policy, data_dir=None)
    submitted = []

    class Service:
        def __init__(self):
            self.db = db

        async def submit(self, actor, request):
            submitted.append(request)
            return SimpleNamespace(generation_id=uuid4())

    bridge = QQBridge(None, settings, None, Service(), None, actor, None)
    sent = []

    class Connection:
        connected = True
        async def call(self, action, payload):
            sent.append((action, payload))
            return {"message_id": 100}

    async def completed():
        return {"state": "completed", "delivery_state": "accepted"}

    async def reply_content():
        return {"text": "好"}

    bridge.connection = Connection()
    bridge.wakeup = SimpleNamespace(set=lambda: None)
    bridge._await_generation = completed
    bridge._reply_content = reply_content
    messages = [
        private_message(private_event(1, "我今天去开会了"), policy),
        private_message(private_event(2, "然后甲方又改需求"), policy),
    ]
    try:
        await bridge.respond_private(bridge.connection, {"kind": "private_batch", "messages": messages})
        assert len(submitted) == 1
        assert submitted[0].request_id == messages[0]["request_id"]
        assert submitted[0].input.text == "我今天去开会了\n然后甲方又改需求"
        assert submitted[0].input.asset_ids == []
        assert sent and sent[0][0] == "send_private_msg"
        assert sent[0][1]["user_id"] == 10001
    finally:
        bridge.connection = None
        await bridge.debouncer.close()
