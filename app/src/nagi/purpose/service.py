"""Storage- and framework-free service around the pure purpose engine.

``PurposeService`` is the only object the API layer needs. It reads a bounded amount of
context from a ``PurposeStore`` (implemented by the host with the project's own DB
sessions), runs the pure gates/engine, and optionally records the resulting closure so a
later tick can follow up. It performs no I/O itself, imports no web framework and never
sends a message: actually delivering the text remains the caller's job, which keeps the
"did we really deliver?" decision auditable and testable.

Typical host wiring::

    service = PurposeService(PurposeDeps(store=DbPurposeStore(...), settings=settings))
    decision = service.tick()                 # once per scheduler tick
    check = service.check()                   # diagnostics, no side effects
    closure = service.confirm(decision)       # after a real delivery
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from pydantic import Field

from ..core.types import DTO
from .closure import ClosureRecord, consume_closure, from_decision
from .engine import (
    PurposeDecision,
    _policy as engine_policy,
    blocking_reason,
    decide,
    gate_reason,
)
from .settings import PurposeSettings

__all__ = [
    "InMemoryPurposeStore",
    "PurposeCheck",
    "PurposeDeps",
    "PurposeService",
    "PurposeStore",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@runtime_checkable
class PurposeStore(Protocol):
    """The bounded context the host must provide; all methods are read-only except closure.

    Implementations are expected to scope rows to the current companion/conversation and
    to return smallest-first, oldest-first windows. The two closure writers are optional
    in practice (the service probes for them), so a host without closure storage still
    gets working gates and decisions.
    """

    def last_user_at(self) -> datetime | None:
        """Timestamp of the most recent real user message, if any."""

    def last_delivery_at(self) -> datetime | None:
        """Timestamp of the most recent proactive delivery, if any."""

    def sent_today(self, now: datetime) -> int:
        """How many proactive deliveries were sent during ``now``'s local day."""

    def recent_events(self, limit: int) -> Sequence[Any]:
        """Recent conversation events, oldest first (mappings or objects with role/content)."""

    def memories(self, limit: int) -> Sequence[Any]:
        """Recent relevant memories, oldest first (mappings or objects with kind/content)."""

    def last_closure(self, now: datetime) -> Any | None:
        """Stored closure record, if the host keeps one."""

    def save_closure(self, record: ClosureRecord) -> None:
        """Persist a closure for a later tick."""

    def clear_closure(self) -> None:
        """Drop the stored closure (expired, superseded or already used)."""

    def record_decision(self, decision: PurposeDecision) -> None:
        """Remember the last decision for diagnostics/audit (optional)."""


@dataclass
class PurposeDeps:
    """Everything the service needs from the host, injected instead of imported."""

    store: PurposeStore
    settings: Any = field(default_factory=PurposeSettings)
    # A host may inject ``lambda: datetime.now(timezone.utc)``; tests use a fake clock.
    clock: Callable[[], datetime] = field(default=_utc_now)
    # IANA zone used to evaluate quiet hours and the daily cap against local wall clock.
    timezone: str = "UTC"
    recent_event_limit: int = 40
    memory_limit: int = 30

    def __post_init__(self) -> None:
        if self.recent_event_limit < 0 or self.memory_limit < 0:
            raise ValueError("context limits must be non-negative")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:  # noqa: BLE001 - surface one clear config error
            raise ValueError(f"unknown timezone: {self.timezone!r}") from exc

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class PurposeCheck(DTO):
    """Explainable outcome of one tick: why it was allowed or blocked, and the decision."""

    allowed: bool
    reason: str = Field(max_length=240)
    decision: PurposeDecision | None = None
    last_user_at: datetime | None = None
    last_delivery_at: datetime | None = None
    sent_today: int = 0
    has_closure: bool = False


class PurposeService:
    """Runs the purpose gates against bounded store context and records closures."""

    def __init__(self, deps: PurposeDeps) -> None:
        self.deps = deps

    # ------------------------------------------------------------------ helpers

    @property
    def settings(self) -> PurposeSettings:
        return engine_policy(self.deps.settings)

    def _now(self, now: datetime | None = None) -> datetime:
        produced = now if now is not None else self.deps.clock()
        return _aware(produced) or _utc_now()

    def _local(self, current: datetime) -> datetime:
        return current.astimezone(self.deps.zone)

    def _stored_closure(self, current: datetime) -> ClosureRecord | None:
        reader = getattr(self.deps.store, "last_closure", None)
        if not callable(reader):
            return None
        return consume_closure(reader(current), current)

    def _gate_reason(
        self,
        now: datetime,
        settings: PurposeSettings,
        last_user_at: datetime | None,
        last_delivery_at: datetime | None,
        sent_today: int,
    ) -> str:
        return gate_reason(
            now,
            last_user_at=last_user_at,
            last_delivery_at=last_delivery_at,
            sent_today=sent_today,
            settings=settings,
        )

    # ------------------------------------------------------------------- public

    def check(self, now: datetime | None = None) -> PurposeCheck:
        """Evaluate gates and the engine without recording anything."""
        current = self._now(now)
        local = self._local(current)
        settings = self.settings
        store = self.deps.store
        last_user = _aware(store.last_user_at())
        last_delivery = _aware(store.last_delivery_at())
        sent = max(0, int(store.sent_today(local) or 0))
        # Gates read wall-clock hours, so they get local time; the store closure is
        # compared in UTC instants.
        gate = self._gate_reason(local, settings, last_user, last_delivery, sent)
        if gate != "ok":
            return PurposeCheck(
                allowed=False,
                reason=gate,
                last_user_at=last_user,
                last_delivery_at=last_delivery,
                sent_today=sent,
            )
        events = list(store.recent_events(self.deps.recent_event_limit) or ())
        memory_rows = list(store.memories(self.deps.memory_limit) or ())
        stored = self._stored_closure(current)
        decision = decide(
            local,
            last_user_at=last_user,
            last_delivery_at=last_delivery,
            sent_today=sent,
            recent_events=events,
            memories=memory_rows,
            last_closure=stored,
            settings=settings,
        )
        if decision is None:
            reason = blocking_reason(
                local,
                last_user_at=last_user,
                last_delivery_at=last_delivery,
                sent_today=sent,
                recent_events=events,
                memories=memory_rows,
                last_closure=stored,
                settings=settings,
            )
        else:
            reason = "ok"
        return PurposeCheck(
            allowed=decision is not None,
            reason=reason,
            decision=decision,
            last_user_at=last_user,
            last_delivery_at=last_delivery,
            sent_today=sent,
            has_closure=stored is not None,
        )

    def tick(self, now: datetime | None = None) -> PurposeDecision | None:
        """Return at most one proactive decision and record its closure; ``None`` blocks.

        Reading and recording are idempotent per store: callers own the scheduler, so an
        external tick stamp is enough. The decision is recorded before the closure so a
        host that fails between the two still has an audit trail.

        A decision that itself came from a stored closure consumes that closure instead of
        re-storing it, so a delivered opening is revisited at most once rather than
        repeating every gate window until it expires.
        """
        current = self._now(now)
        check = self.check(current)
        if not check.allowed or check.decision is None:
            return None
        decision = check.decision
        recorder = getattr(self.deps.store, "record_decision", None)
        if callable(recorder):
            recorder(decision)
        if decision.reason == "closure":
            clearer = getattr(self.deps.store, "clear_closure", None)
            if callable(clearer):
                clearer()
            return decision
        settings = self.settings
        writer = getattr(self.deps.store, "save_closure", None)
        if settings.closure_enabled and callable(writer):
            record = from_decision(decision, now=current, ttl_hours=settings.closure_ttl_hours)
            if record is not None:
                writer(record)
        return decision

    def preview(self, now: datetime | None = None) -> PurposeDecision | None:
        """What ``tick`` would return right now, without recording side effects."""
        return self.check(now).decision

    def confirm(self, decision: PurposeDecision | None, now: datetime | None = None) -> ClosureRecord | None:
        """Record a genuinely delivered decision and return the closure to revisit later.

        Returns ``None`` when the decision is empty, closure storage is disabled or the
        store keeps no closure store. The host calls this only after an actual delivery;
        the decision itself is recorded once, by ``tick``.
        """
        if decision is None:
            return None
        current = self._now(now)
        settings = self.settings
        writer = getattr(self.deps.store, "save_closure", None)
        if not settings.closure_enabled or not callable(writer):
            return None
        record = from_decision(decision, now=current, ttl_hours=settings.closure_ttl_hours)
        if record is not None:
            writer(record)
        return record

    def clear_closure(self) -> None:
        """Drop the stored closure, if the host keeps one."""
        clearer = getattr(self.deps.store, "clear_closure", None)
        if callable(clearer):
            clearer()


class InMemoryPurposeStore:
    """Small reference store for tests and for a first host integration.

    Not shared between processes and not persistent: it exists so the service can be
    exercised without PostgreSQL and so the host has an obvious contract to copy.
    """

    def __init__(
        self,
        *,
        events: Sequence[Any] | None = None,
        memories: Sequence[Any] | None = None,
        deliveries: Sequence[datetime] | None = None,
        last_user_at: datetime | None = None,
        closure: Any = None,
    ) -> None:
        self._events: list[dict[str, Any]] = [dict(event) for event in events or ()]
        self._memories: list[dict[str, Any]] = [dict(memory) for memory in memories or ()]
        self._deliveries: list[datetime] = [stamp for stamp in deliveries or ()]
        self._last_user_at = last_user_at
        self._closure: Any = closure
        self.decisions: list[PurposeDecision] = []
        self._next_id = 1

    # -- mutation used by tests/first host integration

    def _new_id(self, prefix: str) -> str:
        value = f"{prefix}{self._next_id}"
        self._next_id += 1
        return value

    def add_event(
        self,
        role: str,
        text: str,
        *,
        at: datetime,
        purpose: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        content: dict[str, Any] = {"text": text}
        if purpose is not None:
            content["purpose"] = purpose
        event = {
            "id": event_id or self._new_id("e"),
            "role": role,
            "content": content,
            "occurred_at": at,
        }
        self._events.append(event)
        if role == "user":
            self._last_user_at = at
        return event

    def add_memory(
        self,
        kind: str,
        content: str,
        *,
        at: datetime,
        memory_id: str | None = None,
    ) -> dict[str, Any]:
        memory = {
            "id": memory_id or self._new_id("m"),
            "kind": kind,
            "content": content,
            "created_at": at,
        }
        self._memories.append(memory)
        return memory

    def record_delivery(self, at: datetime) -> None:
        self._deliveries.append(at)

    # -- PurposeStore contract

    def last_user_at(self) -> datetime | None:
        return self._last_user_at

    def last_delivery_at(self) -> datetime | None:
        present = [_aware(stamp) for stamp in self._deliveries if stamp is not None]
        present = [stamp for stamp in present if stamp is not None]
        return max(present) if present else None

    def sent_today(self, now: datetime) -> int:
        current = _aware(now)
        assert current is not None
        zone = current.tzinfo or timezone.utc
        day = current.astimezone(zone).date()
        return sum(
            1
            for stamp in self._deliveries
            if stamp is not None and _aware(stamp).astimezone(zone).date() == day
        )

    def recent_events(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return self._events[-limit:]

    def memories(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return self._memories[-limit:]

    def last_closure(self, now: datetime) -> Any | None:
        return self._closure

    def save_closure(self, record: ClosureRecord) -> None:
        self._closure = record

    def clear_closure(self) -> None:
        self._closure = None

    def record_decision(self, decision: PurposeDecision) -> None:
        self.decisions.append(decision)
