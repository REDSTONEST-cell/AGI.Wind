import asyncio

import pytest

from agi_wind.integrations.qq import QQSettings
from agi_wind.integrations.qq_debounce import (
    DebounceConfig, QQDebouncer, config_from_policy, looks_finished, merge_text,
)


def test_finished_heuristics_cover_continuations_and_open_sentences():
    assert looks_finished("我今天去开会了。")
    assert looks_finished("然后呢？")
    assert looks_finished("嗯嗯")
    assert looks_finished("你好")
    assert looks_finished("我去")
    assert not looks_finished("我今天去开会了，")
    assert not looks_finished("然后")
    assert not looks_finished("因为")
    assert not looks_finished("我想说（")
    assert looks_finished("/stop")
    assert not looks_finished("")


def test_merge_preserves_order_and_source_text():
    messages = [{"text": "第一段"}, {"text": ""}, {"text": "第二段"}]
    assert merge_text(messages) == "第一段\n第二段"


def test_config_reads_live_qq_settings():
    config = config_from_policy(QQSettings())
    assert config.enabled is True
    assert config.quiet_seconds == pytest.approx(2.2)
    assert config.max_wait_seconds == pytest.approx(8.0)
    assert config.final_grace_seconds == pytest.approx(0.9)


def make_debouncer(flushed, **overrides):
    config = DebounceConfig(
        enabled=True, quiet_seconds=0.02, max_wait_seconds=0.2,
        final_grace_seconds=0.01, continuation_seconds=0.02,
        max_messages=8, max_chars=4000, min_chars=4,
    )
    config = DebounceConfig(**{**config.__dict__, **overrides})
    return QQDebouncer(
        lambda batch, reason: _record(flushed, batch, reason),
        config_provider=lambda: config,
        jitter=lambda: 0,
    )


async def _record(flushed, batch, reason):
    flushed.append((batch.text, reason, list(batch.messages)))


async def wait_flushed(flushed, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not flushed and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    return flushed


async def test_quiet_window_merges_fragments_into_one_logical_turn():
    flushed = []
    debouncer = make_debouncer(flushed)
    try:
        await debouncer.push("k", {"request_id": "1", "text": "我今天去开会了"})
        await asyncio.sleep(0.006)
        await debouncer.push("k", {"request_id": "2", "text": "然后甲方又改需求"})
        await wait_flushed(flushed)
        assert len(flushed) == 1
        text, reason, messages = flushed[0]
        assert text == "我今天去开会了\n然后甲方又改需求"
        assert [item["request_id"] for item in messages] == ["1", "2"]
        assert reason in ("complete", "max_wait")
    finally:
        await debouncer.close()


async def test_unfinished_fragment_keeps_waiting_and_accepts_the_tail():
    flushed = []
    debouncer = make_debouncer(flushed, max_wait_seconds=0.3)
    try:
        await debouncer.push("k", {"request_id": "1", "text": "然后"})
        await asyncio.sleep(0.04)
        assert flushed == []  # quiet window passed but the sentence is unfinished
        await debouncer.push("k", {"request_id": "2", "text": "我去开会了"})
        await wait_flushed(flushed)
        assert flushed and flushed[0][0] == "然后\n我去开会了"
    finally:
        await debouncer.close()


async def test_complete_message_gets_a_final_grace_then_flushes():
    flushed = []
    debouncer = make_debouncer(flushed, quiet_seconds=0.03, final_grace_seconds=0.05)
    try:
        await debouncer.push("k", {"request_id": "1", "text": "我去开会了。"})
        await asyncio.sleep(0.04)
        assert flushed == []
        await wait_flushed(flushed)
        assert flushed and flushed[0][1] == "complete"
    finally:
        await debouncer.close()


async def test_hard_deadline_never_waits_forever():
    flushed = []
    debouncer = make_debouncer(flushed, quiet_seconds=0.02, max_wait_seconds=0.05)
    try:
        await debouncer.push("k", {"request_id": "1", "text": "然后"})
        await wait_flushed(flushed)
        assert flushed and flushed[0][1] == "max_wait"
    finally:
        await debouncer.close()


async def test_message_limit_flushes_without_losing_messages():
    flushed = []
    debouncer = make_debouncer(flushed, max_messages=2, quiet_seconds=0.2)
    try:
        await debouncer.push("k", {"request_id": "1", "text": "第一段"})
        await debouncer.push("k", {"request_id": "2", "text": "第二段"})
        await wait_flushed(flushed)
        assert flushed and flushed[0][0] == "第一段\n第二段"
        assert len(flushed[0][2]) == 2
    finally:
        await debouncer.close()


async def test_duplicate_platform_message_is_ignored():
    flushed = []
    debouncer = make_debouncer(flushed)
    try:
        await debouncer.push("k", {"request_id": "same", "text": "第一段"})
        await debouncer.push("k", {"request_id": "same", "text": "第一段"})
        await wait_flushed(flushed)
        assert len(flushed[0][2]) == 1
    finally:
        await debouncer.close()


async def test_stop_cancels_pending_batch_without_flushing():
    flushed = []
    debouncer = make_debouncer(flushed, quiet_seconds=0.1)
    try:
        await debouncer.push("k", {"request_id": "1", "text": "然后"})
        await asyncio.sleep(0.02)
        await debouncer.cancel_all("stopped")
        await asyncio.sleep(0.15)
        assert flushed == []
    finally:
        await debouncer.close()
