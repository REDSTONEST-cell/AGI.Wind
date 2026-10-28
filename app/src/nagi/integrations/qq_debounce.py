"""Short-window aggregation for QQ private text messages.

The design intentionally follows the private-only approach used by active
open-source QQ bots instead of sleeping inside the generation worker:

* AstrBot's message merger (MIT): merge follow-ups while the reply pipeline is
  still running, cap messages/characters, never merge commands or non-text.
* qq-maid-bot (MIT): a private-only quiet window (1.2s) plus a hard deadline
  (3s), with all source IDs retained and dispatch kept ordered. Group
  aggregation is deliberately unsupported there because per-sender batches
  would reorder a shared group scope.

This module keeps the wait outside the provider/generation path. It only
returns one logical private message batch; the caller decides how to submit it.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger("nagi.qq.debounce")

SENTENCE_END = re.compile(r"([。！？!?…~～]|\.{3})[\s\"'”’」』】)\]]*$")
CONTINUATION_END = re.compile(
    r"([，,、；;：:]|(?:然后|还有|而且|但是|不过|因为|所以|就是|如果|假如|虽然|"
    r"那个|这个|我跟你说|我想说|我觉得|就是说))[\s。.!！?？…~～]*$"
)
OPEN_END = re.compile(r"[（(【\[《“\"‘'`]$")
SHORT_COMPLETE = re.compile(
    r"^(?:嗯+|哦+|啊+|好+|行+|可以|对|是的|确实|没事|没事的|哈哈+|在吗|在么|人呢|咋了|怎么了)"
    r"[。.!！?？…~～]*$"
)
# Leading commands must bypass aggregation; /stop is handled earlier by the bridge.
COMMAND_START = re.compile(r"^[/#!！]")


@dataclass(frozen=True)
class DebounceConfig:
    enabled: bool = True
    quiet_seconds: float = 2.2
    max_wait_seconds: float = 8.0
    final_grace_seconds: float = 0.9
    continuation_seconds: float = 2.0
    max_messages: int = 8
    max_chars: int = 4000
    min_chars: int = 2


def config_from_policy(policy) -> DebounceConfig:
    """Read current settings without freezing them at bridge construction."""
    return DebounceConfig(
        enabled=bool(getattr(policy, "debounce_enabled", True)),
        quiet_seconds=float(getattr(policy, "debounce_quiet_seconds", 2.2)),
        max_wait_seconds=float(getattr(policy, "debounce_max_wait_seconds", 8.0)),
        final_grace_seconds=float(getattr(policy, "debounce_final_grace_seconds", 0.9)),
        continuation_seconds=float(getattr(policy, "debounce_continuation_seconds", 2.0)),
        max_messages=max(1, int(getattr(policy, "debounce_max_messages", 8))),
        max_chars=max(100, int(getattr(policy, "debounce_max_chars", 4000))),
        min_chars=max(1, int(getattr(policy, "debounce_min_chars", 2))),
    )


def merge_text(messages) -> str:
    """Preserve arrival order and the user's own text; no artificial labels."""
    parts = []
    for message in messages:
        text = str((message or {}).get("text") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def looks_finished(text: str, min_chars: int = 2) -> bool:
    """Cheap deterministic guess: terminal punctuation is done; trailing
    conjunctions/commas/open brackets mean the thought is still continuing.

    This is intentionally not an LLM call: an extra model round would cost more
    latency than the entire aggregation window and would be unreliable for
    short chat fragments.
    """
    value = str(text or "").strip()
    if not value:
        return False
    if COMMAND_START.match(value):
        return True
    if OPEN_END.search(value):
        return False
    if CONTINUATION_END.search(value):
        return False
    if SENTENCE_END.search(value) or SHORT_COMPLETE.match(value):
        return True
    return len(re.sub(r"\s", "", value)) >= max(1, min_chars)


@dataclass
class Batch:
    key: str
    messages: list[dict] = field(default_factory=list)
    request_ids: set[str] = field(default_factory=set)
    first_at: float = 0.0
    last_at: float = 0.0
    hard_deadline: float = 0.0
    event: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None

    @property
    def text(self) -> str:
        return merge_text(self.messages)


class QQDebouncer:
    """Collect private text fragments and flush one batch after a quiet window."""

    def __init__(self, flush, *, config_provider, clock=time.monotonic,
                 jitter: Callable[[], float] | None = None, logger_=logger):
        self._flush = flush
        self._config = config_provider
        self._clock = clock
        self._jitter = jitter or (lambda: random.uniform(-0.15, 0.15))
        self._logger = logger_
        self._batches: dict[str, Batch] = {}
        self._closed = False

    def pending_count(self) -> int:
        return len(self._batches)

    def pending_text(self) -> str:
        return "\n".join(batch.text for batch in self._batches.values())

    async def push(self, key: str, message: dict) -> bool:
        """Append one physical message. Returns False when aggregation is off."""
        if self._closed:
            return False
        config = self._config()
        if not config.enabled:
            return False
        text = str(message.get("text") or "").strip()
        if not text:
            return False
        request_id = str(message.get("request_id") or message.get("platform_message_id") or "")
        now = self._clock()
        batch = self._batches.get(key)
        if batch is not None:
            if request_id and request_id in batch.request_ids:
                return True
            if now >= batch.hard_deadline:
                await self._flush_batch(key, batch, "max_wait")
                batch = None
            elif (len(batch.messages) >= config.max_messages
                    or len(batch.text) + len(text) >= config.max_chars):
                await self._flush_batch(key, batch, "max_limits")
                batch = None
        if batch is None:
            batch = Batch(
                key=key,
                first_at=now,
                last_at=now,
                hard_deadline=now + max(config.quiet_seconds, config.max_wait_seconds),
            )
            self._batches[key] = batch
            batch.worker = asyncio.create_task(self._run(key, batch), name="qq-debounce")
        else:
            batch.last_at = now
            batch.event.set()
        batch.messages.append(message)
        if request_id:
            batch.request_ids.add(request_id)
        return True

    async def cancel_all(self, reason: str = "cancelled") -> None:
        """Drop pending batches without generating replies (stop/close path)."""
        for key, batch in list(self._batches.items()):
            self._batches.pop(key, None)
            if batch.worker is not None and not batch.worker.done():
                batch.worker.cancel()
            self._logger.info("qq debounce cleared key=%s reason=%s messages=%s", key, reason, len(batch.messages))
        await asyncio.sleep(0)

    async def flush_key(self, key: str, reason: str = "barrier") -> None:
        """Flush one key immediately, used before a non-text boundary message."""
        batch = self._batches.get(key)
        if batch is not None:
            await self._flush_batch(key, batch, reason)

    async def close(self) -> None:
        self._closed = True
        await self.cancel_all("closed")

    async def _run(self, key: str, batch: Batch) -> None:
        try:
            while True:
                config = self._config()
                if not config.enabled:
                    await self._flush_batch(key, batch, "disabled")
                    return
                now = self._clock()
                if len(batch.messages) >= config.max_messages or len(batch.text) >= config.max_chars:
                    await self._flush_batch(key, batch, "max_limits")
                    return
                if now >= batch.hard_deadline:
                    await self._flush_batch(key, batch, "max_wait")
                    return
                quiet_deadline = min(
                    batch.last_at + max(0.05, config.quiet_seconds + self._jitter()),
                    batch.hard_deadline,
                )
                if await self._wait_until(quiet_deadline, batch.event):
                    continue
                if self._batches.get(key) is not batch:
                    return
                now = self._clock()
                if now >= batch.hard_deadline:
                    await self._flush_batch(key, batch, "max_wait")
                    return
                if not looks_finished(batch.text, config.min_chars):
                    # Keep listening for the rest of the thought, but never
                    # extend beyond the hard deadline. A new message wakes us
                    # and gets its own quiet window.
                    arrived = await self._wait_until(batch.hard_deadline, batch.event)
                    if self._batches.get(key) is not batch:
                        return
                    if arrived:
                        continue
                    await self._flush_batch(key, batch, "max_wait")
                    return
                # The user's sentence looks finished. Give a short final grace
                # period so a trailing fragment can still join the same turn.
                grace_deadline = min(now + max(0.05, config.final_grace_seconds), batch.hard_deadline)
                if await self._wait_until(grace_deadline, batch.event):
                    continue
                await self._flush_batch(key, batch, "complete")
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.warning("qq debounce worker failed key=%s", key, exc_info=True)
            await self._flush_batch(key, batch, "error")

    async def _wait_until(self, deadline: float, event: asyncio.Event) -> bool:
        """Return True if a new message arrived, False on deadline."""
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return False
            event.clear()
            return True

    async def _flush_batch(self, key: str, batch: Batch, reason: str) -> None:
        if self._batches.get(key) is not batch:
            return
        self._batches.pop(key, None)
        if batch.worker is not None and batch.worker is not asyncio.current_task() and not batch.worker.done():
            batch.worker.cancel()
        try:
            await self._flush(batch, reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.warning("qq debounce flush failed key=%s reason=%s", key, reason, exc_info=True)
