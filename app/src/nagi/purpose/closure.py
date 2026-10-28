"""Pure conversation-closure records.

A closure captures a thread the companion deliberately leaves open after a proactive
message: the topic, a question that was not answered, the opening to come back to, and
a hard expiry. The record is a plain value: building and consuming one never touches
storage, the model or the clock beyond the timestamp passed in by the caller.

The gateway persists closures through its own store (no new table is required); this
module only defines the shape and the pure rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from pydantic import Field, ValidationError

from ..core.types import DTO

ClosurePurpose = Literal["care", "share", "invite", "curiosity", "follow_up"]
CLOSURE_PURPOSE_NAMES: tuple[str, ...] = ("care", "share", "invite", "curiosity", "follow_up")
MAX_TOPIC_CHARS = 120
MAX_TEXT_CHARS = 400
DEFAULT_TTL_HOURS = 72


def _aware(value: datetime | None) -> datetime | None:
    """Normalise a timestamp for comparison; naive values are treated as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _purpose(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in CLOSURE_PURPOSE_NAMES:
        return text
    return "follow_up"


class ClosureRecord(DTO):
    """One open conversational thread left by a delivered proactive message."""

    topic: str = Field(min_length=1, max_length=MAX_TOPIC_CHARS)
    unanswered_question: str = Field(default="", max_length=MAX_TEXT_CHARS)
    next_opening: str = Field(default="", max_length=MAX_TEXT_CHARS)
    purpose: ClosurePurpose = "follow_up"
    created_at: datetime
    expires_at: datetime


def as_closure(value: Any) -> ClosureRecord | None:
    """Coerce a stored closure (record, mapping or attribute object) into a record.

    Returns ``None`` for anything malformed: a broken closure must never crash a tick.
    """
    if value is None:
        return None
    if isinstance(value, ClosureRecord):
        return value
    payload: dict[str, Any]
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        payload = {}
        for name in ("topic", "unanswered_question", "next_opening", "purpose", "created_at", "expires_at"):
            item = getattr(value, name, None)
            if item is not None:
                payload[name] = item
    if not payload.get("topic") or payload.get("created_at") is None or payload.get("expires_at") is None:
        return None
    try:
        return ClosureRecord.model_validate(payload)
    except ValidationError:
        return None


def build_closure(
    *,
    topic: Any,
    unanswered_question: Any = "",
    next_opening: Any = "",
    purpose: Any = "follow_up",
    now: datetime,
    ttl_hours: Any = DEFAULT_TTL_HOURS,
) -> ClosureRecord | None:
    """Build a closure record; returns ``None`` when the topic or TTL is unusable."""
    created = _aware(now) or datetime.now(timezone.utc)
    clean_topic = _clip(topic, MAX_TOPIC_CHARS)
    if not clean_topic:
        return None
    try:
        ttl = float(ttl_hours)
    except (TypeError, ValueError):
        return None
    if ttl <= 0:
        return None
    expires = created + timedelta(hours=ttl)
    if expires <= created:
        return None
    return ClosureRecord(
        topic=clean_topic,
        unanswered_question=_clip(unanswered_question, MAX_TEXT_CHARS),
        next_opening=_clip(next_opening, MAX_TEXT_CHARS),
        purpose=_purpose(purpose),
        created_at=created,
        expires_at=expires,
    )


def from_decision(decision: Any, *, now: datetime | None = None, ttl_hours: Any = DEFAULT_TTL_HOURS) -> ClosureRecord | None:
    """Turn an engine decision into the closure that a later tick may revisit."""
    if decision is None:
        return None
    purpose = _purpose(getattr(decision, "purpose", "follow_up"))
    topic = getattr(decision, "topic", "")
    seed = getattr(decision, "seed", "")
    created = _aware(now) or _aware(getattr(decision, "created_at", None)) or datetime.now(timezone.utc)
    return build_closure(
        topic=topic,
        unanswered_question=seed if purpose == "curiosity" else "",
        next_opening=seed,
        purpose=purpose,
        now=created,
        ttl_hours=ttl_hours,
    )


def is_expired(record: ClosureRecord | None, now: datetime) -> bool:
    """A closure is expired at (not before) its stored expiry instant."""
    if record is None:
        return True
    current = _aware(now) or datetime.now(timezone.utc)
    expires = _aware(record.expires_at) or current
    return current >= expires


def consume_closure(value: Any, now: datetime) -> ClosureRecord | None:
    """Return the closure when it is still usable, otherwise ``None``.

    Consuming is side-effect free: the caller decides whether to clear its stored copy
    after using the opening.
    """
    record = as_closure(value)
    if record is None or is_expired(record, now):
        return None
    return record


def closure_material(record: ClosureRecord) -> str:
    """The text a later purpose opening may be built from."""
    return _clip(record.next_opening or record.unanswered_question, MAX_TEXT_CHARS)
