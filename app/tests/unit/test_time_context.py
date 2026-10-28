from datetime import datetime, timezone

from nagi.delivery.output_guard import time_context


def test_clock_is_local_and_timeline_is_ordered():
    now = datetime(2026, 10, 28, 10, 0, 0, tzinfo=timezone.utc)
    events = [
        {
            "role": "assistant",
            "content": {"text": "我在。"},
            "recorded_at": "2026-10-28T09:55:00+00:00",
        },
        {
            "role": "user",
            "content": {"text": "哎你在干什么"},
            "recorded_at": "2026-10-28T10:00:00+00:00",
        },
    ]
    text = time_context(events, now=now)

    assert "当前时刻：2026-10-28 18:00:00（周三" in text
    assert "本轮历史最近一条消息：今天 18:00:00（刚刚，主人）" in text
    assert "最近对话时间线" in text
    assert text.index("今天 17:55:00 你：我在。") < text.index("今天 18:00:00 主人：")
    assert "18:00:00 主人：哎你在干什么" in text
    assert "不要主动报时" in text


def test_no_history_says_first_message():
    now = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
    text = time_context([], now=now)
    assert "这是本次会话的第一条消息" in text
    assert "当前时刻：2026-10-28 18:00:00" in text


def test_old_message_gets_day_label_and_gap_warning():
    now = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
    events = [
        {
            "role": "user",
            "content": {"text": "晚安"},
            "recorded_at": "2026-10-27T15:30:00+00:00",
        }
    ]
    text = time_context(events, now=now)
    assert "昨天 23:30:00" in text
    assert "距上一条消息已经有一段时间" in text


def test_invalid_timestamps_do_not_break_context():
    now = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)
    text = time_context([{"role": "user", "content": {"text": "在吗"}, "recorded_at": "bad"}], now=now)
    assert "这是本次会话的第一条消息" in text
