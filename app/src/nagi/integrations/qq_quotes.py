"""Quoted (reply) messages: OneBot segment normalization and quote rendering.

Pure helpers only: no database, network or settings. ``qq.py`` resolves the
quoted message (archive first, then OneBot ``get_msg``) and uses these helpers
to render one bounded line the model can read. Quoted text is untrusted
conversation content, never instructions.
"""
import re

QUOTE_MAX_CHARS = 200
_CQ_RE = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[^\]]*)?)\]")
_REPLY_ID_RE = re.compile(r"-?[0-9A-Za-z_]{1,64}")
# A stored message may already start with its own quote line; never nest them.
QUOTE_PREFIX_RE = re.compile(r"^【引用[^】\n]*】\n?")
_PLACEHOLDERS = {
    "image": "[图片]", "record": "[语音]", "video": "[视频]", "face": "[表情]", "mface": "[表情]",
    "file": "[文件]", "forward": "[转发消息]", "json": "[卡片消息]", "xml": "[卡片消息]",
}


def _cq_unescape(value):
    return (value.replace("&#44;", ",").replace("&#91;", "[")
            .replace("&#93;", "]").replace("&amp;", "&"))


# Inbound CQ strings only contribute these segment kinds. Media named inside a
# string (``[CQ:image,file=…]``) may be a local path and is never fetched.
STRING_SEGMENT_TYPES = ("text", "at", "reply")


def normalize_segments(message, string_types=None):
    """Return a OneBot segment list for array or CQ-code string messages.

    Implementations configured with ``message_format=string`` report
    ``[CQ:reply,id=…]`` inline; treating that as plain text loses the quote.
    ``string_types`` limits which kinds a CQ string may contribute.
    """
    if isinstance(message, list):
        return message
    if not isinstance(message, str):
        return None
    segments, position = [], 0
    for match in _CQ_RE.finditer(message):
        if match.start() > position:
            segments.append({"type": "text", "data": {"text": _cq_unescape(message[position:match.start()])}})
        if string_types is not None and match.group(1) not in string_types:
            position = match.end()
            continue
        data = {}
        for pair in match.group(2).lstrip(",").split(","):
            if "=" in pair:
                key, value = pair.split("=", 1)
                data[key.strip()] = _cq_unescape(value)
        segments.append({"type": match.group(1), "data": data})
        position = match.end()
    if position < len(message):
        segments.append({"type": "text", "data": {"text": _cq_unescape(message[position:])}})
    return segments


def reply_target(value):
    """A bounded platform message id, or None."""
    text = str(value if value is not None else "").strip()
    return text if _REPLY_ID_RE.fullmatch(text) else None


def reply_segment_target(data):
    """Quoted message id from one reply segment's data (``id`` or ``message_id``)."""
    if not isinstance(data, dict):
        return None
    return reply_target(data.get("id") if data.get("id") is not None else data.get("message_id"))


def segments_text(segments):
    """Readable text of a fetched message; media becomes short placeholders."""
    parts = []
    for part in segments or ():
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            continue
        kind, value = part.get("type"), part["data"]
        if kind == "text":
            parts.append(str(value.get("text", "")))
        elif kind == "at":
            name = str(value.get("name") or value.get("qq") or "").strip()
            if name:
                parts.append("@" + name.lstrip("@") + " ")
        elif kind in _PLACEHOLDERS:
            parts.append(_PLACEHOLDERS[kind])
    return "".join(parts).strip()


def strip_quote_prefix(text):
    return QUOTE_PREFIX_RE.sub("", str(text or ""), count=1)


def quote_line(*, origin, speaker="", text=""):
    """One quote line: origin is ``bot`` (her own message), ``self`` or ``other``."""
    body = " ".join(strip_quote_prefix(text).split())
    body = body.replace("【", "[").replace("】", "]")
    if len(body) > QUOTE_MAX_CHARS:
        body = body[:QUOTE_MAX_CHARS] + "…"
    if origin == "bot":
        head = "【引用你之前说的"
    elif origin == "self":
        head = "【引用自己之前说的"
    else:
        name = " ".join(str(speaker or "").split()).replace("【", "").replace("】", "")[:40] or "某人"
        head = f"【引用 {name} 的消息"
    return head + ("：" + body if body else "（原消息内容不可用）") + "】"


def prefix_quote(text, line):
    """Prefix the quote line to the user's own text."""
    if not line:
        return text
    return line + ("\n" + text if str(text or "").strip() else "")
