"""Daily token/cost quota checks and the authoritative usage ledger.

Enforcement model:
* ``check`` is a pre-flight read of today's ledger totals. It never writes and
  never raises for ordinary over-quota conditions; the caller decides.
* ``record`` appends the metered usage after the call. Usage is always recorded,
  including calls that would have been blocked, so the ledger stays truthful.
* Both operations run on a caller-owned SQLAlchemy Session/Connection: the
  gateway keeps its transaction boundaries, and no provider SDK is involved.

Daily windows are timezone-aware: the day starts at local midnight for the
configured timezone (default: the process-local timezone) and is queried as a
UTC instant range, because ``usage_ledger.created_at`` is timezone-aware UTC.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone, tzinfo
from datetime import timezone as dt_timezone
from typing import Any, Callable, Mapping
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from ..core.types import DTO
from .models import usage_ledger
from .overview import estimate_tokens
from .settings import QuotaSettings

SessionLike = Session | Connection

CATEGORIES = ("web", "qq", "memory", "purpose", "other")
CATEGORY_LIMIT_FIELDS = {
    "web": "daily_web_tokens",
    "qq": "daily_qq_tokens",
    "memory": "daily_memory_tokens",
}

_EPSILON = 1e-9


class QuotaDecision(DTO):
    """Result of a pre-flight quota check. ``remaining``/``cost_remaining`` are None when unlimited."""

    allowed: bool
    remaining: int | None = None
    cost_remaining: float | None = None
    # Stable limit code when a configured limit is reached (or soft-reached with hard_stop=False).
    reason: str | None = None
    warning: bool = False


def local_timezone() -> tzinfo:
    """Process-local timezone; UTC when the platform cannot resolve one."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def day_bounds(moment: datetime, tz: tzinfo | None = None) -> tuple[datetime, datetime]:
    """Return the UTC instant range ``[start, end)`` of the local calendar day containing ``moment``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    zone = tz or local_timezone()
    start = moment.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def category_limit(quota: QuotaSettings, category: str) -> int:
    """Configured daily token limit for a category; 0 means unlimited (or no per-category limit)."""
    field = CATEGORY_LIMIT_FIELDS.get(category)
    return int(getattr(quota, field)) if field else 0


def estimate_usage(prompt_text: str | None = "", completion_text: str | None = "") -> dict[str, Any]:
    """Estimated usage kwargs for ``record`` when the provider returned no measurement.

    The returned ``estimated=True`` mark is what keeps cheap archive estimates out
    of billed or audited totals.
    """
    return {
        "prompt_tokens": estimate_tokens(prompt_text),
        "completion_tokens": estimate_tokens(completion_text),
        "estimated": True,
    }


def _coerce_quota(value: Any) -> QuotaSettings:
    """Accept ``QuotaSettings`` directly or a host settings object exposing usage/quota."""
    if isinstance(value, QuotaSettings):
        return value
    if value is None:
        return QuotaSettings()
    if isinstance(value, Mapping):
        for name in ("usage_quota", "quota", "usage"):
            nested = value.get(name)
            if isinstance(nested, Mapping):
                return QuotaSettings.model_validate(nested)
        return QuotaSettings.model_validate(dict(value))
    for name in ("usage_quota", "quota", "usage"):
        nested = getattr(value, name, None)
        if isinstance(nested, QuotaSettings):
            return nested
        if isinstance(nested, Mapping):
            return QuotaSettings.model_validate(nested)
    fields = {name: getattr(value, name) for name in QuotaSettings.model_fields if hasattr(value, name)}
    if fields:
        return QuotaSettings.model_validate(fields)
    raise TypeError("QuotaService requires QuotaSettings or host settings exposing usage/quota")


def _category(value: str) -> str:
    if value not in CATEGORIES:
        raise ValueError("unknown usage category: " + str(value))
    return value


def _tokens(value: Any) -> int:
    count = int(value or 0)
    if count < 0:
        raise ValueError("token counts must be non-negative")
    return count


def _model_ref(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump())
    if isinstance(value, str):
        return {"model_id": value}
    raise TypeError("model_ref must be a mapping, ModelRef or string")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class QuotaService:
    """Daily quota policy over the ``usage_ledger`` table."""

    def __init__(
        self,
        settings: QuotaSettings | Any,
        *,
        account_id: UUID | None = None,
        timezone: tzinfo | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.quota = _coerce_quota(settings)
        self.account_id = account_id
        self.timezone = timezone
        self._clock = clock or (lambda: datetime.now(dt_timezone.utc))

    # -- internals -----------------------------------------------------
    def _account(self, account_id: UUID | None) -> UUID:
        value = account_id if account_id is not None else self.account_id
        if value is None:
            raise ValueError("account_id is required for quota operations")
        return value

    def _now(self, now: datetime | None) -> datetime:
        moment = now if now is not None else self._clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    @staticmethod
    def _day_totals(session: SessionLike, account_id: UUID, start: datetime, end: datetime):
        row = session.execute(
            select(
                func.coalesce(func.sum(usage_ledger.c.prompt_tokens), 0),
                func.coalesce(func.sum(usage_ledger.c.completion_tokens), 0),
                func.coalesce(func.sum(usage_ledger.c.cost_cny), 0),
            ).where(
                usage_ledger.c.account_id == account_id,
                usage_ledger.c.created_at >= start,
                usage_ledger.c.created_at < end,
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0), float(row[2] or 0)

    @staticmethod
    def _category_tokens(session: SessionLike, account_id: UUID, category: str, start: datetime, end: datetime) -> int:
        value = session.scalar(
            select(func.coalesce(func.sum(usage_ledger.c.total_tokens), 0)).where(
                usage_ledger.c.account_id == account_id,
                usage_ledger.c.category == category,
                usage_ledger.c.created_at >= start,
                usage_ledger.c.created_at < end,
            )
        )
        return int(value or 0)

    # -- public API ----------------------------------------------------
    def check(
        self,
        category: str,
        session: SessionLike,
        *,
        account_id: UUID | None = None,
        now: datetime | None = None,
    ) -> QuotaDecision:
        """Read today's totals and decide whether another request may start (no writes)."""
        category = _category(category)
        quota = self.quota
        if not quota.enabled:
            return QuotaDecision(allowed=True)
        account = self._account(account_id)
        start, end = day_bounds(self._now(now), self.timezone)
        prompt, completion, cost = self._day_totals(session, account, start, end)
        used = prompt + completion
        category_used = self._category_tokens(session, account, category, start, end)

        limits: list[tuple[str, int, int]] = []
        if quota.daily_total_tokens > 0:
            limits.append(("daily_total_tokens", used, quota.daily_total_tokens))
        limit_field = CATEGORY_LIMIT_FIELDS.get(category)
        if limit_field and getattr(quota, limit_field) > 0:
            limits.append((f"daily_{category}_tokens", category_used, int(getattr(quota, limit_field))))

        remaining: int | None = None
        for _, consumed, limit in limits:
            headroom = limit - consumed
            remaining = headroom if remaining is None else min(remaining, headroom)
        if remaining is not None:
            remaining = max(0, remaining)

        cost_limit = float(quota.daily_cost_cny)
        cost_remaining = None if cost_limit <= 0 else max(0.0, cost_limit - cost)

        reason: str | None = None
        blocked = False
        for code, consumed, limit in limits:
            if consumed >= limit:
                code, blocked, reason = code, True, code
                break
        if not blocked and cost_limit > 0 and cost >= cost_limit - _EPSILON:
            blocked, reason = True, "daily_cost_cny"

        warning = False
        if quota.warning_percent > 0:
            threshold = quota.warning_percent / 100
            warning = any(consumed >= limit * threshold - _EPSILON for _, consumed, limit in limits)
            if not warning and cost_limit > 0:
                warning = cost >= cost_limit * threshold - _EPSILON

        if blocked and quota.hard_stop:
            return QuotaDecision(
                allowed=False,
                remaining=remaining,
                cost_remaining=cost_remaining,
                reason=reason,
                warning=warning,
            )
        return QuotaDecision(
            allowed=True,
            remaining=remaining,
            cost_remaining=cost_remaining,
            reason=reason,
            warning=warning,
        )

    def record(
        self,
        category: str,
        session: SessionLike,
        account_id: UUID,
        companion_id: UUID,
        *,
        generation_id: UUID | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost_cny: float = 0.0,
        estimated: bool = False,
        model_ref: Any = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append one metered usage row. Usage is recorded even after a blocked check."""
        category = _category(category)
        prompt, completion = _tokens(prompt_tokens), _tokens(completion_tokens)
        cost = float(cost_cny or 0)
        if not math.isfinite(cost) or cost < 0:
            raise ValueError("usage cost must be a finite non-negative number")
        row = session.execute(
            usage_ledger.insert()
            .values(
                account_id=account_id,
                companion_id=companion_id,
                generation_id=generation_id,
                category=category,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion,
                cost_cny=cost,
                estimated=bool(estimated),
                model_ref=_model_ref(model_ref),
                created_at=self._now(now),
            )
            .returning(usage_ledger)
        ).mappings().one()
        return dict(row)

    def summary(
        self,
        session: SessionLike,
        account_id: UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Aggregate ledger usage for an account, defaulting to the current local day."""
        start = _aware(since)
        end = _aware(until)
        if start is None and end is None:
            start, end = day_bounds(self._now(now), self.timezone)
        query = select(
            usage_ledger.c.category,
            func.count().label("entries"),
            func.coalesce(func.sum(usage_ledger.c.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(usage_ledger.c.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(usage_ledger.c.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(usage_ledger.c.cost_cny), 0).label("cost_cny"),
            func.count().filter(usage_ledger.c.estimated).label("estimated_entries"),
        ).where(usage_ledger.c.account_id == account_id)
        if start is not None:
            query = query.where(usage_ledger.c.created_at >= start)
        if end is not None:
            query = query.where(usage_ledger.c.created_at < end)
        query = query.group_by(usage_ledger.c.category)

        categories: dict[str, dict[str, Any]] = {}
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_cny": 0.0}
        entries = estimated_entries = 0
        for row in session.execute(query).mappings():
            item = {
                "entries": int(row["entries"]),
                "prompt_tokens": int(row["prompt_tokens"]),
                "completion_tokens": int(row["completion_tokens"]),
                "total_tokens": int(row["total_tokens"]),
                "cost_cny": float(row["cost_cny"]),
            }
            categories[row["category"]] = item
            entries += item["entries"]
            estimated_entries += int(row["estimated_entries"])
            totals["prompt_tokens"] += item["prompt_tokens"]
            totals["completion_tokens"] += item["completion_tokens"]
            totals["total_tokens"] += item["total_tokens"]
            totals["cost_cny"] += item["cost_cny"]
        totals["cost_cny"] = round(totals["cost_cny"], 8)

        quota = self.quota
        return {
            "enabled": quota.enabled,
            "window": {
                "since": start.isoformat() if start is not None else None,
                "until": end.isoformat() if end is not None else None,
            },
            **totals,
            "entries": entries,
            "estimated_entries": estimated_entries,
            "categories": categories,
            "limits": {
                "daily_total_tokens": quota.daily_total_tokens,
                "daily_web_tokens": quota.daily_web_tokens,
                "daily_qq_tokens": quota.daily_qq_tokens,
                "daily_memory_tokens": quota.daily_memory_tokens,
                "daily_cost_cny": quota.daily_cost_cny,
                "warning_percent": quota.warning_percent,
                "hard_stop": quota.hard_stop,
            },
        }
