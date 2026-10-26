"""Server-side display segmentation shared by delivery, voice and history.

The dialogue model decides how many chat messages a reply becomes: every line it
writes is one message.  ``reply-segments.js`` follows the same rule in the browser
so the live bubbles and the stored segments agree; the client only falls back to
its own splitter for old archives that predate stored segments.
"""
from __future__ import annotations

import re

_END_PUNCT = re.compile(r"[。！？!?…]+$")


def split_reply(text: str, *, final: bool = True) -> list[tuple[str, bool]]:
    """Split a reply on the model's own line breaks and return ``(message, complete)`` pairs."""
    text = str(text or "")
    parts: list[tuple[str, bool]] = []
    current = ""
    fenced = False
    inline = False

    def emit(complete: bool) -> None:
        nonlocal current
        value = _END_PUNCT.sub("", current.strip())
        if value:
            parts.append((value, complete))
        current = ""

    i, size = 0, len(text)
    while i < size:
        if text.startswith("```", i):
            fenced = not fenced
            current += "```"
            i += 3
            continue
        char = text[i]
        if not fenced and char == "`":
            inline = not inline
            current += char
            i += 1
            continue
        if fenced or inline:
            current += char
            i += 1
            continue
        if char == "\n":
            emit(True)
            i += 1
            continue
        current += char
        i += 1
    emit(final)
    return parts


class ReplySegmenter:
    """Incrementally hand out finished messages while the model is streaming.

    Every line the model closes is final, so each feed re-splits the accumulated
    visible text and hands out the complete lines it has not emitted yet.
    """

    def __init__(self) -> None:
        self.text = ""
        self.emitted = 0

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self.text += chunk
        parts = split_reply(self.text, final=False)
        ready = [value for value, complete in parts[self.emitted :] if complete]
        self.emitted += len(ready)
        return ready

    def flush(self) -> list[str]:
        parts = split_reply(self.text, final=True)
        ready = [value for value, _complete in parts[self.emitted :]]
        self.emitted = len(parts)
        return ready
