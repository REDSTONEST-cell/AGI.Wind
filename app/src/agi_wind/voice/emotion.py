"""Emotion vocabulary, hidden self-tag parsing and fixed Breeze instructions.

The dialogue model declares its own emotion in a hidden leading tag such as
``[情绪:开心]``. The server buffers the first few characters, strips the tag before
anything is shown or archived, and maps the label to a fixed instruction. A small
restricted fallback call is used only when the tag is missing.
"""
from __future__ import annotations

import json
import re
from ..delivery.output_guard import VisibleTextGuard

EMOTIONS = ("开心", "平静", "难过", "担心", "撒娇", "生气", "惊讶", "温柔")
DEFAULT_EMOTION = "平静"

ALIASES = {
    "高兴": "开心",
    "快乐": "开心",
    "喜悦": "开心",
    "悲伤": "难过",
    "伤心": "难过",
    "失落": "难过",
    "害怕": "担心",
    "焦虑": "担心",
    "愤怒": "生气",
    "不满": "生气",
    "吃惊": "惊讶",
    "意外": "惊讶",
    "柔和": "温柔",
    "亲切": "温柔",
}

INSTRUCTIONS = {
    "开心": "语气轻快、带笑意，语速稍快，声音明亮，像忍不住开心。",
    "平静": "语气温柔平静，自然放松，像日常聊天。",
    "难过": "语气低落、轻柔，语速放慢，带一点鼻音，像在忍住难过。",
    "担心": "语气关切、轻柔，语速稍慢，带一点不安。",
    "撒娇": "语气软糯、尾音稍微拖长，带一点撒娇的黏人感。",
    "生气": "语气微沉、克制，带一点不满，但不要吼。",
    "惊讶": "语气上扬，带一点意外和好奇。",
    "温柔": "语气温柔、亲近，像在耳边说话。",
}


_DIRECTIVE = re.compile(
    r"^\s*(?:\[|【)\s*(情绪|心情|表情包)\s*(?:[:：]\s*([^\]】\n]{0,40}?))?\s*(?:\]|】)"
)
_BARE_EMOTION = re.compile(
    r"^\s*(?:\[|【)\s*(" + "|".join(re.escape(value) for value in (*EMOTIONS, *ALIASES)) + r")\s*(?:\]|】)"
)
_DIRECTIVE_PREFIXES = ("[心情", "【心情", "[情绪", "[表情包", "【情绪", "【表情包") + tuple(
    "[" + name for name in (*EMOTIONS, *ALIASES)
) + tuple("【" + name for name in (*EMOTIONS, *ALIASES))


class EmotionTagStream:
    """Strip the leading hidden ``[情绪:xxx]`` / ``[表情包:关键词]`` directives.

    The dialogue model declares its emotion and whether it wants to send a
    sticker in leading hidden tags.  The first characters are held only until
    the complete directives, a visible character, a newline, or a reasonable
    prefix limit is reached.  Once resolved, feed() passes text through
    unchanged; flush() releases any unresolved prefix at the end of the turn.
    """

    def __init__(self, max_prefix: int = 80):
        self._visible = VisibleTextGuard()
        self.max_prefix = max_prefix
        self.buffer = ""
        self.resolved = False
        self.emotion: str | None = None
        self.raw_tag: str | None = None
        self.sticker_requested = False
        self.sticker_keyword: str = ""
        self._strip_leading_newline = False

    def _consume_directives(self) -> None:
        while True:
            match = _DIRECTIVE.match(self.buffer)
            if not match:
                bare = _BARE_EMOTION.match(self.buffer)
                if bare:
                    self.raw_tag = bare.group(1).strip("。.!！?？ ")
                    self.emotion = normalize_emotion(self.raw_tag)
                    self.buffer = self.buffer[bare.end():]
                    continue
                return
            name, value = match.group(1), (match.group(2) or "").strip()
            if name in ("情绪", "心情"):
                if value:
                    self.raw_tag = value.strip("。.!！?？ ")
                    self.emotion = normalize_emotion(self.raw_tag)
            else:
                self.sticker_requested = True
                self.sticker_keyword = value
            self.buffer = self.buffer[match.end():]

    def _resolve(self) -> str:
        self.resolved = True
        visible = self.buffer.lstrip("\r\n")
        self.buffer = ""
        self._strip_leading_newline = not visible
        return visible

    def feed(self, text: str) -> str:
        return self._visible.feed(self._feed(text))

    def _feed(self, text: str) -> str:
        if self.resolved:
            if self._strip_leading_newline:
                text = text.lstrip("\r\n")
                if text:
                    self._strip_leading_newline = False
            return text
        self.buffer += text
        self._consume_directives()
        stripped = self.buffer.lstrip()
        if not stripped:
            return ""
        if stripped[0] in "[【":
            # Wait while this can still become a directive; otherwise it is normal text.
            if any(prefix.startswith(stripped) or stripped.startswith(prefix) for prefix in _DIRECTIVE_PREFIXES):
                if len(self.buffer) > self.max_prefix:
                    return self._resolve()
                return ""
        return self._resolve()

    def flush(self) -> str:
        return self._visible.feed(self._flush()) + self._visible.flush()

    def _flush(self) -> str:
        if self.resolved:
            return ""
        self._consume_directives()
        if not self.buffer.strip():
            self.resolved = True
            self.buffer = ""
            return ""
        return self._resolve()


def normalize_emotion(value: object) -> str:
    text = str(value or "").strip()
    text = ALIASES.get(text, text)
    return text if text in EMOTIONS else DEFAULT_EMOTION


def instruction_for(emotion: object, tone: str = "") -> str:
    normalized = normalize_emotion(emotion)
    base = "保持角色的音色和平时说话习惯"
    tone = " ".join(str(tone or "").split())[:100]
    if tone:
        base += "（" + tone + "）"
    return base + "。" + INSTRUCTIONS[normalized]


def director_prompt(user_text: str, reply: str, tone: str = "") -> str:
    payload = {
        "tone": " ".join(str(tone or "").split())[:100],
        "user": str(user_text or "")[:500],
        "reply": str(reply or "")[:2000],
    }
    return (
        "你是语音语气导演。根据用户的话和角色刚说出的回复，判断这句话最主要的情绪。\n"
        '只输出严格 JSON：{"emotion":"开心|平静|难过|担心|撒娇|生气|惊讶|温柔"}。\n'
        "不要解释，不要输出其他字段。角色平时的语气："
        + payload["tone"]
        + "\n"
        + json.dumps({"user": payload["user"], "reply": payload["reply"]}, ensure_ascii=False)
    )
