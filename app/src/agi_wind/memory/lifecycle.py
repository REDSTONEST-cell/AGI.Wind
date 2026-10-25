"""Time-aware memory lifecycle.

Design borrows the *concepts* of LivingMemory's atom lifecycle (type-specific TTL,
importance, access reinforcement, decay and expiry) but is an independent implementation
against AGI.Wind's PostgreSQL schema. No AGPL code is copied.
"""
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from . import models as mm

KIND_TTL_DAYS = {
    "episode": 7.0,
    "plan": 2.0,
    "fact": 180.0,
    "relationship": 90.0,
    "preference": 60.0,
    "commitment": 90.0,
    "identity": 365.0,
    "other": 30.0,
}
KIND_IMPORTANCE = {
    "episode": 0.35,
    "plan": 0.60,
    "fact": 0.60,
    "relationship": 0.80,
    "preference": 0.80,
    "commitment": 0.85,
    "identity": 0.95,
    "other": 0.50,
}
DECAY_BY_KIND = {"plan": "step", "relationship": "linear"}


def now_utc():
    return datetime.now(timezone.utc)


def importance_for(kind, confidence=0.6):
    base = KIND_IMPORTANCE.get(kind, KIND_IMPORTANCE["other"])
    return max(0.0, min(1.0, base * (0.65 + 0.35 * max(0.0, min(1.0, float(confidence))))))


def ttl_for(kind, importance=0.5, reinforcement_count=0):
    base = KIND_TTL_DAYS.get(kind, KIND_TTL_DAYS["other"])
    importance_factor = 0.5 + max(0.0, min(1.0, float(importance)))
    reinforcement_factor = 1.0 + min(0.5, max(0, int(reinforcement_count)) * 0.1)
    return max(1.0, base * importance_factor * reinforcement_factor)


def decay_type_for(kind):
    return DECAY_BY_KIND.get(kind, "exponential")


def decay_score(decay_type, ttl_days, days_since):
    """Return a 0-1 retention multiplier for a TTL/age pair."""
    ttl = max(1.0, float(ttl_days or 1.0))
    days = max(0.0, float(days_since or 0.0))
    if decay_type == "linear":
        return max(0.0, 1.0 - days / ttl)
    if decay_type == "step":
        return 1.0 if days <= ttl else 0.05
    half_life = ttl / 2.0
    return math.exp(-math.log(2) * days / max(0.5, half_life))


def freshness(item, reference=None):
    reference = reference or now_utc()
    anchor = item.get("last_accessed_at") or item.get("created_at")
    if anchor is None:
        return 1.0
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    days = max(0.0, (reference - anchor).total_seconds() / 86400.0)
    return decay_score(item.get("decay_type") or "exponential", item.get("ttl_days") or 30.0, days)


def lifecycle_values(kind, confidence=0.6, reference=None):
    reference = reference or now_utc()
    importance = importance_for(kind, confidence)
    ttl = ttl_for(kind, importance)
    return {
        "importance": round(importance, 4),
        "ttl_days": round(ttl, 3),
        "expires_at": reference + timedelta(days=ttl),
        "decay_type": decay_type_for(kind),
        "access_count": 0,
    }


def extend_after_access(item, access_count, reference=None):
    """Compute new expiry/metadata after a memory was actually injected into context."""
    reference = reference or now_utc()
    ttl = ttl_for(item["kind"], item["importance"] or 0.5, access_count)
    current = item.get("expires_at")
    target = reference + timedelta(days=ttl)
    if current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return {
        "access_count": access_count,
        "last_accessed_at": reference,
        "last_reinforced_at": reference,
        "expires_at": max(current, target) if current else target,
    }


def scored_hit_metadata(item, reference=None):
    fresh = freshness(item, reference)
    importance = float(item.get("importance") or 0.5)
    score = 0.55 + 0.25 * importance + 0.20 * fresh
    return {"importance": importance, "freshness": round(fresh, 4), "score": round(score, 4)}


def maintain(db, companion_id=None, reference=None):
    """Expire active memories whose TTL elapsed. Hidden/superseded rows keep their state."""
    reference = reference or now_utc()
    with db.engine.begin() as conn:
        condition = [mm.memory_items.c.status.in_(["active", "dormant"]), mm.memory_items.c.expires_at.is_not(None), mm.memory_items.c.expires_at <= reference]
        if companion_id is not None:
            condition.append(mm.memory_items.c.companion_id == companion_id)
        result = conn.execute(update(mm.memory_items).where(*condition).values(status="expired", archived_at=reference))
        return {"expired": result.rowcount or 0, "checked_at": reference.isoformat()}
