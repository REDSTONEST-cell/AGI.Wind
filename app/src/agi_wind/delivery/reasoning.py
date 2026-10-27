"""Coalesce streamed native reasoning before it is persisted.

Providers stream reasoning a few characters at a time. Persisting every delta
costs one transaction plus a rewrite of the whole assistant event, which grows
quadratically over a long trace; short batches keep the log live and cheap.
"""
import time


class ReasoningBuffer:
    def __init__(self, write, *, max_chars=400, max_seconds=0.5, clock=time.monotonic):
        self.write, self.max_chars, self.max_seconds, self.clock = write, max_chars, max_seconds, clock
        self.parts, self.size, self.started = [], 0, None

    async def add(self, text):
        if not text: return
        if not self.parts: self.started = self.clock()
        self.parts.append(text)
        self.size += len(text)
        if self.size >= self.max_chars or self.clock() - self.started >= self.max_seconds:
            await self.flush()

    async def flush(self):
        if not self.parts: return
        text = ''.join(self.parts)
        self.parts, self.size, self.started = [], 0, None
        await self.write(text)
