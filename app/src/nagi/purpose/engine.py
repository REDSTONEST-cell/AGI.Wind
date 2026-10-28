"""Pure purpose selection for proactive companion messages.

The engine turns already-loaded conversation context into at most one opening move. It
never calls a model, never touches storage and never writes the text a user will see:
the caller hands the resulting decision to the dialogue model (or drops it).

Hard gates, in order: enabled, quiet hours (local wall clock), minimum idle time since
the last user message, minimum gap since the last proactive delivery, daily cap, and
finally "is there material worth opening with". A gate failure means ``None``; the same
inputs always produce the same decision.

A purpose is opened only once per bounded event window: purposes already delivered in
``recent_events`` are suppressed, except the deliberate continuation of a stored
closure, which exists precisely to be revisited later.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import Field

from ..core.types import DTO
from .closure import closure_material, consume_closure
from .settings import PurposeSettings, in_quiet_hours

PurposeName = Literal["care", "share", "invite", "curiosity", "follow_up"]
PURPOSES: tuple[PurposeName, ...] = ("care", "share", "invite", "curiosity", "follow_up")
# Selection priority, most deliberate first: a follow-up on an explicit open loop beats
# a fresh wellbeing check, which beats curiosity, an invitation and a shared anecdote.
PRIORITY: tuple[PurposeName, ...] = ("follow_up", "care", "curiosity", "invite", "share")

GUIDANCE: dict[str, str] = {
    "care": "先轻轻关心对方此刻的状态，不要追问细节，也不要给建议。",
    "share": "分享一件你最近想到的小事或感受，不要复述聊天记录。",
    "invite": "提出一个轻松、可以一起做的小提议，并接受对方拒绝。",
    "curiosity": "顺着之前没聊完的问题继续好奇地问一句。",
    "follow_up": "自然地问一下之前提到的事情后来怎么样了。",
}

MAX_TOPIC_CHARS = 120
MAX_SEED_CHARS = 160

# Material keywords stay deliberately small and literal so a decision is explainable.
CARE_KEYWORDS = (
    "累", "困", "熬夜", "通宵", "加班", "生病", "感冒", "发烧", "不舒服", "头疼", "头痛",
    "胃疼", "失眠", "压力", "焦虑", "难过", "伤心", "哭", "心情不好", "失恋", "吵架",
    "委屈", "孤单", "tired", "exhausted", "sick", "stressed", "anxious",
)
FOLLOW_UP_KEYWORDS = (
    "等结果", "结果出来", "明天", "后天", "下周", "下个月", "晚点", "到时候", "回头",
    "等我", "再说", "面试", "考试", "出差", "复查", "体检", "出院", "搬家", "入职",
    "报告出来", "消息给我", "告诉你", "跟你说", "pending", "tomorrow",
)
INVITE_KEYWORDS = (
    "一起", "想去看", "想去", "有空", "周末", "假期", "旅行", "旅游", "电影", "演唱会",
    "展览", "咖啡", "散步", "爬山", "露营", "桌游", "游戏", "吃饭", "探店", "市集",
)
SHARE_KEYWORDS = (
    "发现", "有趣", "原来", "想起", "记得", "第一次", "遇到", "看了", "听了", "去了",
    "喜欢", "惊喜", "好玩",
)
QUESTION_MARKS = ("?", "？")


class PurposeDecision(DTO):
    """One explainable proactive opening move selected by the engine."""

    purpose: PurposeName
    # Short label used for dedupe/closure identity, never shown to the user.
    topic: str = Field(min_length=1, max_length=MAX_TOPIC_CHARS)
    # Material the dialogue model may draw on. Not a finished message.
    seed: str = Field(min_length=1, max_length=400)
    guidance: str = Field(default="", max_length=400)
    reason: str = Field(default="", max_length=240)
    source_event_ids: list[str] = Field(default_factory=list, max_length=5)
    source_memory_ids: list[str] = Field(default_factory=list, max_length=5)
    dedupe_key: str = Field(default="", max_length=200)
    created_at: datetime
    expires_at: datetime | None = None


@dataclass(frozen=True)
class _Event:
    role: str
    text: str
    purpose: str | None
    at: datetime | None
    id: str


@dataclass(frozen=True)
class _Memory:
    text: str
    kind: str
    at: datetime | None
    id: str


@dataclass(frozen=True)
class _Candidate:
    purpose: PurposeName
    topic: str
    seed: str
    reason: str
    event_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None
    from_closure: bool = False


# --------------------------------------------------------------------------- inputs


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _content_text(value: Any) -> str:
    """Extract visible text from an archived ``content`` value (JSONB or plain string)."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Mapping):
        for key in ("text", "content"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return " ".join(text.split())
        return ""
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return " ".join(text.split())
    return ""


def _content_purpose(value: Any) -> str | None:
    """Read the proactive purpose marker stored on a delivered assistant event."""
    if not isinstance(value, Mapping):
        return None
    candidate = value.get("purpose")
    proactive = value.get("proactive")
    if isinstance(proactive, Mapping) and proactive.get("purpose") is not None:
        candidate = proactive.get("purpose")
    if isinstance(candidate, str) and candidate.strip().lower() in PURPOSES:
        return candidate.strip().lower()
    return None


def _normalize_event(value: Any) -> _Event | None:
    role = str(_get(value, "role", "") or "").strip().lower()
    if role not in ("user", "assistant", "system"):
        return None
    content = _get(value, "content")
    text = _content_text(content)
    if not text:
        raw_text = _get(value, "text")
        if isinstance(raw_text, str):
            text = " ".join(raw_text.split())
    purpose = _content_purpose(content)
    if purpose is None and isinstance(value, Mapping):
        purpose = _content_purpose(value)
    stamp = _aware(_get(value, "occurred_at") or _get(value, "recorded_at") or _get(value, "at"))
    raw_id = _get(value, "id")
    return _Event(role=role, text=text, purpose=purpose, at=stamp, id="" if raw_id is None else str(raw_id))


def _normalize_memory(value: Any) -> _Memory | None:
    raw_content = _get(value, "content")
    text = _content_text(raw_content)
    if not text:
        text = _clip(raw_content, 400)
    if not text:
        return None
    kind = str(_get(value, "kind", "") or "").strip().lower()
    stamp = _aware(
        _get(value, "created_at") or _get(value, "occurred_at") or _get(value, "last_reinforced_at")
    )
    raw_id = _get(value, "id") or _get(value, "memory_id")
    return _Memory(text=text, kind=kind, at=stamp, id="" if raw_id is None else str(raw_id))


def _normalize_events(values: Sequence[Any] | None) -> list[_Event]:
    result: list[_Event] = []
    for value in values or ():
        event = _normalize_event(value)
        if event is not None:
            result.append(event)
    return result


def _normalize_memories(values: Sequence[Any] | None) -> list[_Memory]:
    result: list[_Memory] = []
    for value in values or ():
        memory = _normalize_memory(value)
        if memory is not None:
            result.append(memory)
    return result


def _user_events(events: Sequence[_Event]) -> list[_Event]:
    return [event for event in events if event.role == "user" and event.text]


def _matches(text: str, keywords: Sequence[str]) -> bool:
    lowered = text.casefold()
    return any(keyword in lowered for keyword in keywords)


def _topic(text: Any, fallback: str = "近况") -> str:
    cleaned = " ".join(str(text or "").split())
    for mark in ("。", "！", "？", "!", "?", "，", ",", "\n"):
        index = cleaned.find(mark)
        if index > 0:
            cleaned = cleaned[:index]
            break
    cleaned = cleaned.strip()[:MAX_TOPIC_CHARS]
    return cleaned or fallback


def _dedupe_topic(topic: str) -> str:
    return _clip(topic.casefold(), 40)


# ----------------------------------------------------------------------- candidates


def _closure_candidate(last_closure: Any, now: datetime, last_user_at: datetime | None) -> _Candidate | None:
    record = consume_closure(last_closure, now)
    if record is None:
        return None
    # A user reply after the closure was written means the thread moved on; the stored
    # opening must not be replayed as if nothing happened.
    if last_user_at is not None and _aware(last_user_at) > _aware(record.created_at):
        return None
    material = closure_material(record)
    if not material:
        return None
    return _Candidate(
        purpose=record.purpose,  # type: ignore[arg-type]
        topic=record.topic,
        seed="之前留下的引子：" + _clip(material, MAX_SEED_CHARS),
        reason="closure",
        expires_at=record.expires_at,
        from_closure=True,
    )


def _care_candidate(events: Sequence[_Event]) -> _Candidate | None:
    for event in reversed(_user_events(events)):
        if _matches(event.text, CARE_KEYWORDS):
            return _Candidate(
                purpose="care",
                topic=_topic(event.text),
                seed="用户最近说过：" + _clip(event.text, 120),
                reason="user_wellbeing",
                event_ids=(event.id,) if event.id else (),
            )
    return None


def _follow_up_candidate(events: Sequence[_Event]) -> _Candidate | None:
    for event in reversed(_user_events(events)):
        if _matches(event.text, FOLLOW_UP_KEYWORDS):
            return _Candidate(
                purpose="follow_up",
                topic=_topic(event.text, "没聊完的事"),
                seed="用户之前提过：" + _clip(event.text, 120),
                reason="open_loop",
                event_ids=(event.id,) if event.id else (),
            )
    return None


def _curiosity_candidate(events: Sequence[_Event]) -> _Candidate | None:
    views = list(events)
    for index in range(len(views) - 1, -1, -1):
        event = views[index]
        if event.role != "user" or not event.text:
            continue
        if not _matches(event.text, QUESTION_MARKS):
            continue
        # Any later assistant turn means the question was already engaged with.
        if any(later.role == "assistant" and later.text for later in views[index + 1:]):
            continue
        return _Candidate(
            purpose="curiosity",
            topic=_topic(event.text, "一个没聊完的问题"),
            seed="可以顺着这个问题继续问：" + _clip(event.text, 120),
            reason="unanswered_question",
            event_ids=(event.id,) if event.id else (),
        )
    return None


def _invite_candidate(events: Sequence[_Event], memories: Sequence[_Memory]) -> _Candidate | None:
    for memory in reversed(memories):
        if _matches(memory.text, INVITE_KEYWORDS):
            return _Candidate(
                purpose="invite",
                topic=_topic(memory.text, "一起做点什么"),
                seed="可以邀请对方一起：" + _clip(memory.text, 120),
                reason="shared_interest",
                memory_ids=(memory.id,) if memory.id else (),
            )
    for event in reversed(_user_events(events)):
        if _matches(event.text, INVITE_KEYWORDS):
            return _Candidate(
                purpose="invite",
                topic=_topic(event.text, "一起做点什么"),
                seed="可以邀请对方一起：" + _clip(event.text, 120),
                reason="mentioned_activity",
                event_ids=(event.id,) if event.id else (),
            )
    return None


def _share_candidate(memories: Sequence[_Memory]) -> _Candidate | None:
    for memory in reversed(memories):
        if memory.kind == "episode":
            return _Candidate(
                purpose="share",
                topic=_topic(memory.text, "一件小事"),
                seed="可以分享最近想到的经历：" + _clip(memory.text, 120),
                reason="episode_memory",
                memory_ids=(memory.id,) if memory.id else (),
            )
        if memory.kind in ("fact", "preference") and _matches(memory.text, SHARE_KEYWORDS):
            return _Candidate(
                purpose="share",
                topic=_topic(memory.text, "一件小事"),
                seed="可以分享最近想到的小事：" + _clip(memory.text, 120),
                reason="interest_memory",
                memory_ids=(memory.id,) if memory.id else (),
            )
    return None


def _candidates(
    now: datetime,
    last_user_at: datetime | None,
    events: Sequence[_Event],
    memories: Sequence[_Memory],
    last_closure: Any,
) -> list[_Candidate]:
    ordered: list[_Candidate] = []
    closure = _closure_candidate(last_closure, now, last_user_at)
    if closure is not None:
        ordered.append(closure)
    extractors = (
        lambda: _follow_up_candidate(events),
        lambda: _care_candidate(events),
        lambda: _curiosity_candidate(events),
        lambda: _invite_candidate(events, memories),
        lambda: _share_candidate(memories),
    )
    for extract in extractors:
        candidate = extract()
        if candidate is not None:
            ordered.append(candidate)
    # One opening per purpose: a closure continuation wins over a fresh extraction of
    # the same purpose, and two matching events never produce two candidates.
    seen: set[str] = set()
    unique: list[_Candidate] = []
    for candidate in ordered:
        if candidate.purpose in seen:
            continue
        seen.add(candidate.purpose)
        unique.append(candidate)
    return unique


def _delivered_purposes(events: Sequence[_Event]) -> set[str]:
    return {event.purpose for event in events if event.role == "assistant" and event.purpose}


def _decision(candidate: _Candidate, now: datetime) -> PurposeDecision:
    created = _aware(now) or datetime.now(timezone.utc)
    return PurposeDecision(
        purpose=candidate.purpose,
        topic=candidate.topic,
        seed=candidate.seed,
        guidance=GUIDANCE.get(candidate.purpose, ""),
        reason=candidate.reason,
        source_event_ids=list(candidate.event_ids[:5]),
        source_memory_ids=list(candidate.memory_ids[:5]),
        dedupe_key=f"{candidate.purpose}:{_dedupe_topic(candidate.topic)}",
        created_at=created,
        expires_at=candidate.expires_at,
    )


# ---------------------------------------------------------------------------- gates


def _policy(value: Any) -> PurposeSettings:
    """Accept a bare PurposeSettings, a whole Settings object or a settings mapping."""
    if isinstance(value, PurposeSettings):
        return value
    if value is None:
        return PurposeSettings()
    inner = getattr(value, "purpose", None)
    if isinstance(inner, PurposeSettings):
        return inner
    if isinstance(value, Mapping):
        payload = value["purpose"] if "purpose" in value else value
        return PurposeSettings.model_validate(payload)
    if isinstance(inner, Mapping):
        return PurposeSettings.model_validate(inner)
    raise TypeError("settings must be PurposeSettings or an object exposing .purpose")


def _elapsed(now: datetime, then: datetime | None) -> timedelta:
    current = _aware(now) or datetime.now(timezone.utc)
    stamp = _aware(then)
    if stamp is None:
        return timedelta.max
    return current - stamp


def gate_reason(
    now: datetime,
    last_user_at: datetime | None = None,
    last_delivery_at: datetime | None = None,
    sent_today: int = 0,
    *,
    settings: Any = None,
) -> str:
    """Return ``"ok"`` when every time/cap gate passes, otherwise the blocking reason."""
    policy = _policy(settings)
    if not policy.enabled:
        return "disabled"
    if in_quiet_hours(now, policy.quiet_start, policy.quiet_end):
        return "quiet_hours"
    if last_user_at is not None and _elapsed(now, last_user_at) < timedelta(minutes=policy.min_idle_minutes):
        return "not_idle"
    if last_delivery_at is not None and _elapsed(now, last_delivery_at) < timedelta(minutes=policy.min_gap_minutes):
        return "gap"
    try:
        sent = max(0, int(sent_today or 0))
    except (TypeError, ValueError):
        sent = 0
    if sent >= policy.max_per_day:
        return "daily_cap"
    return "ok"


def blocking_reason(
    now: datetime,
    last_user_at: datetime | None = None,
    last_delivery_at: datetime | None = None,
    sent_today: int = 0,
    recent_events: Sequence[Any] | None = (),
    memories: Sequence[Any] | None = (),
    last_closure: Any = None,
    *,
    settings: Any = None,
) -> str:
    """Explain why ``decide`` would return ``None`` without building the decision."""
    reason = gate_reason(
        now,
        last_user_at=last_user_at,
        last_delivery_at=last_delivery_at,
        sent_today=sent_today,
        settings=settings,
    )
    if reason != "ok":
        return reason
    events = _normalize_events(recent_events)
    memory_rows = _normalize_memories(memories)
    candidates = _candidates(now, last_user_at, events, memory_rows, last_closure)
    if not candidates:
        return "no_material"
    delivered = _delivered_purposes(events)
    if all(candidate.purpose in delivered and not candidate.from_closure for candidate in candidates):
        return "duplicate_purpose"
    return "ok"


def decide(
    now: datetime,
    last_user_at: datetime | None = None,
    last_delivery_at: datetime | None = None,
    sent_today: int = 0,
    recent_events: Sequence[Any] | None = (),
    memories: Sequence[Any] | None = (),
    last_closure: Any = None,
    *,
    settings: Any = None,
) -> PurposeDecision | None:
    """Select at most one proactive purpose, or ``None`` when any gate blocks.

    ``recent_events`` and ``memories`` are the caller's bounded windows, oldest first;
    ``recent_events`` entries may be mappings or objects with ``role`` and ``content``.
    """
    policy = _policy(settings)
    reason = gate_reason(
        now,
        last_user_at=last_user_at,
        last_delivery_at=last_delivery_at,
        sent_today=sent_today,
        settings=policy,
    )
    if reason != "ok":
        return None
    events = _normalize_events(recent_events)
    memory_rows = _normalize_memories(memories)
    candidates = _candidates(now, last_user_at, events, memory_rows, last_closure)
    delivered = _delivered_purposes(events)
    for candidate in candidates:
        if candidate.purpose in delivered and not candidate.from_closure:
            continue
        return _decision(candidate, now)
    return None
