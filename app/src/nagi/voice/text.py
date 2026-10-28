"""Turn a display reply into something worth reading aloud."""
from __future__ import annotations

from dataclasses import dataclass
import re

_EMOTION_TAG = re.compile(r"[\[【]\s*情绪\s*[:：][^\]】]{1,12}[\]】]")
_FENCED = re.compile(r"```[\s\S]*?```")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HTML = re.compile(r"<[^>]+>")
_HEADING = re.compile(r"(?m)^\s{0,3}(?:#{1,6}|>|[-*+]|\d+[.)])\s*")
_INLINE_CODE = re.compile(r"`([^`]*)`")
_STAGE = re.compile(r"[（(]\s*(?:笑|叹气|停顿|轻声|小声|沉默|哭|哼)\s*[）)]")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D]"
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SPACE = re.compile(r"\s+")
_REPEAT_COMMA = re.compile(r"[，,、；;]{2,}")
_TRIM_END = "，,、；;:："


@dataclass(frozen=True)
class SpokenText:
    text: str
    truncated: bool = False


def clean_for_speech(value: str, max_chars: int = 400) -> SpokenText:
    text = str(value or "")
    if not text.strip():
        return SpokenText("")
    text = _EMOTION_TAG.sub("", text)
    text = _FENCED.sub(" 代码。 ", text)
    text = _IMAGE.sub(" 图片 ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" 链接 ", text)
    text = _HTML.sub("", text)
    text = _HEADING.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _STAGE.sub("", text)
    text = re.sub(r"[*_~`#>]", "", text)
    text = _EMOJI.sub("", text)
    text = _CONTROL.sub("", text)
    text = re.sub(r"\n+", "。", text)
    text = _SPACE.sub(" ", text)
    text = _REPEAT_COMMA.sub("，", text)
    text = re.sub(r"([！？!?])\s*[。.]", r"\1", text)
    text = re.sub(r"。[ \t]*([！？!?])", r"\1", text)
    text = re.sub(r"[。]{2,}", "。", text)
    text = re.sub(r"\s*([，。！？；：、])\s*", r"\1", text)
    text = text.strip(" \t\r\n，,、；;：:")
    text = text.strip()
    if not text:
        return SpokenText("")
    if len(text) <= max_chars:
        return SpokenText(text if text.endswith(("。", "！", "？", "…", ".", "!", "?")) else text + "。")

    window = text[:max_chars]
    cut = max(window.rfind(mark) for mark in "。！？!?；;")
    if cut >= max(20, int(max_chars * 0.55)):
        spoken = window[: cut + 1]
    else:
        spoken = text[: max(1, max_chars - 1)].rstrip(" \t" + _TRIM_END) + "。"
    spoken = spoken.strip()
    if len(spoken) > max_chars:
        spoken = spoken[:max_chars].rstrip(" \t" + _TRIM_END)
    if not spoken:
        return SpokenText("")
    return SpokenText(spoken, truncated=True)
