"""Server-side display segmentation shared by delivery, voice and history.

The browser's ``reply-segments.js`` splits a finished reply into chat bubbles and
drops the sentence-ending punctuation.  This module keeps the same rules on the
server so one authoritative segment list can drive both the text bubbles and the
per-segment voice decisions.  The client only falls back to its own splitter for
old archives that predate stored segments.
"""
from __future__ import annotations

import re

_FAMILY = {
    "。": "。.",
    ".": "。.",
    "？": "？?",
    "?": "？?",
    "！": "！!",
    "!": "！!",
}
_TOKEN = re.compile(r"\S+$")
_URL = re.compile(r"(?:https?://|www\.)", re.IGNORECASE)
_PREFIX_CHARS = re.compile(r"^[\s?!！？。.…\"'“”‘’（）()]+", re.UNICODE)
_PUNCT_ONLY = re.compile(r"[。！？.!?]+\s*")
_CLOSERS = set("”’\"'）)】]")
_END_PUNCT = set("。！？!?")


def split_reply(text: str, *, final: bool = True) -> list[tuple[str, bool]]:
    """Mirror ``reply-segments.js`` and return ``(bubble text, complete)`` pairs."""
    text = str(text or "")
    parts: list[tuple[str, bool]] = []
    current = ""
    fenced = False
    inline = False

    def emit(complete: bool) -> None:
        nonlocal current
        value = current.strip()
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
        match = _TOKEN.search(current)
        token = match.group(0) if match else ""
        in_url = bool(_URL.search(token))
        boundary = char == "\n" or char in "。！？" or (char in "!?" and not in_url)
        if not boundary:
            current += char
            i += 1
            continue
        if char != "\n" and not _PREFIX_CHARS.sub("", current):
            # Expressive question/exclamation prefixes belong to the next sentence.
            current += char
            i += 1
            continue
        if char == "\n":
            emit(True)
            i += 1
            continue
        family = _FAMILY.get(char, "！!")
        while i + 1 < size and text[i + 1] in family:
            i += 1
        if _PUNCT_ONLY.fullmatch(text[i + 1 :]):
            while i + 1 < size and text[i + 1] in "。！？.!?":
                i += 1
        while i + 1 < size and text[i + 1] in _CLOSERS:
            current += text[i + 1]
            i += 1
        emit(True)
        i += 1
    emit(final)
    return parts


def _ends_with_sentence_punctuation(text: str) -> bool:
    return bool(text) and text.rstrip()[-1:] in _END_PUNCT and text == text.rstrip()


class ReplySegmenter:
    """Incrementally emit stable bubble segments while the model is streaming.

    ``split_reply`` is cheap, so each feed re-splits the accumulated visible
    text and only hands out segments that can no longer change.  When the text
    currently ends exactly on sentence punctuation the newest segment is held
    back one feed so a trailing closing quote/bracket can still join it.
    """

    def __init__(self) -> None:
        self.text = ""
        self.emitted = 0

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self.text += chunk
        parts = split_reply(self.text, final=False)
        complete = [index for index, item in enumerate(parts) if item[1]]
        limit = len(complete)
        if limit and _ends_with_sentence_punctuation(self.text):
            limit -= 1
        ready: list[str] = []
        while self.emitted < limit:
            ready.append(parts[self.emitted][0])
            self.emitted += 1
        return ready

    def flush(self) -> list[str]:
        parts = split_reply(self.text, final=True)
        ready = [text for text, _complete in parts[self.emitted :]]
        self.emitted = len(parts)
        return ready
