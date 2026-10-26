from uuid import uuid4

from agi_wind.integrations.qq_attention import GroupAttention
from agi_wind.integrations.qq_groups import group_allowed, group_message


def message(text="你好", **overrides):
    value = {"room_key": "room-1", "sender_principal_id": "member-1", "is_self": False,
             "anonymous": False, "bot_qq": "334193096", "mentioned_qq_ids": [],
             "reply_to_platform_message_id": None, "text": text, "platform_message_id": "100"}
    value.update(overrides)
    return value


def test_mention_and_quote_of_bot_are_strong_signals_without_any_model():
    attention = GroupAttention()
    assert attention.decide(message(mentioned_qq_ids=["334193096"])).reason == "mention"
    assert attention.decide(message(reply_to_platform_message_id="55"), receipt_exists=True).reason == "reply_to_bot"
    assert attention.decide(message(reply_to_platform_message_id="55"), receipt_exists=False).reason == "reply_to_other"


def test_follow_up_needs_active_window_and_explicit_cue():
    clock = [0.0]
    attention = GroupAttention(followup_seconds=90, max_rounds=2, clock=lambda: clock[0])
    assert attention.decide(message("那明天呢")).reason == "no_active_thread"
    attention.mark_replied("room-1", "member-1", ["900"])
    assert attention.decide(message("那明天呢")).reason == "follow_up"
    assert attention.decide(message("今天天气不错")).reason == "no_followup_cue"
    assert attention.decide(message("甲你说呢", mentioned_qq_ids=["111"])).reason == "addressed_other"
    clock[0] = 91
    assert attention.decide(message("那明天呢")).reason == "no_active_thread"


def test_round_limit_returns_to_observation_until_new_mention():
    clock = [0.0]
    attention = GroupAttention(followup_seconds=90, max_rounds=2, clock=lambda: clock[0])
    attention.mark_replied("room-1", "member-1")
    attention.mark_replied("room-1", "member-1")
    attention.mark_replied("room-1", "member-1")
    assert attention.decide(message("为什么")).reason == "rounds_exhausted"
    # A fresh explicit @ is not bound by the follow-up round budget.
    assert attention.decide(message("@我", mentioned_qq_ids=["334193096"])).reason == "mention"


def test_mention_only_mode_never_infers_continuation():
    attention = GroupAttention()
    attention.mark_replied("room-1", "member-1")
    assert attention.decide(message("那明天呢"), trigger_mode="mention").reason == "mention_only_mode"


def test_anonymous_or_self_messages_never_reply():
    attention = GroupAttention()
    attention.mark_replied("room-1", "member-1")
    assert attention.decide(message("那明天呢", anonymous=True)).reason == "anonymous"
    assert attention.decide(message("那明天呢", is_self=True)).reason == "self_message"


def group_event(**overrides):
    value = {
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "self_id": 334193096, "group_id": 123456789, "user_id": 3548782171, "message_id": 77,
        "sender": {"user_id": 3548782171, "nickname": "小明", "card": "群名片", "role": "admin"},
        "message": [{"type": "at", "data": {"qq": "334193096"}}, {"type": "text", "data": {"text": " 你好"}}],
    }
    value.update(overrides)
    return value


def test_group_message_normalizes_identity_and_never_trusts_role():
    from agi_wind.integrations.qq import QQSettings
    settings = QQSettings(enabled=True, owner_qq="3548782171", bot_qq="334193096", conversation_id=uuid4(),
                          group_enabled=True, allowed_groups=["123456789"])
    normalized = group_message(group_event(), settings)
    assert normalized["group_id"] == "123456789" and normalized["sender_qq"] == "3548782171"
    assert normalized["mentioned_qq_ids"] == ["334193096"]
    assert normalized["text"] == "你好"
    assert "is_owner" not in normalized  # server-side owner match only, never sender.role


def test_group_message_rejects_foreign_bot_and_disabled_groups():
    from agi_wind.integrations.qq import QQSettings
    settings = QQSettings(enabled=True, owner_qq="3548782171", bot_qq="334193096", conversation_id=uuid4(),
                          group_enabled=True, allowed_groups=["123456789"])
    assert group_message(group_event(self_id=999), settings) is None
    assert group_allowed(settings, "123456789") is True
    assert group_allowed(settings, "987654321") is False
    disabled = QQSettings(enabled=True, owner_qq="3548782171", bot_qq="334193096", conversation_id=uuid4())
    assert group_allowed(disabled, "123456789") is False
