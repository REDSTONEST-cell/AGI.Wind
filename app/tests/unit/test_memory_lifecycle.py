from datetime import datetime, timedelta, timezone

from agi_wind.memory.lifecycle import (
    decay_score,
    extend_after_access,
    freshness,
    importance_for,
    lifecycle_values,
    ttl_for,
)


def test_kind_ttl_and_importance_follow_living_memory_concepts():
    assert importance_for("identity", 1.0) > importance_for("preference", 1.0) > importance_for("episode", 1.0)
    assert ttl_for("episode", 0.4) < ttl_for("fact", 0.6) < ttl_for("identity", 0.9)
    assert ttl_for("preference", 0.8, reinforcement_count=4) > ttl_for("preference", 0.8)
    values = lifecycle_values("preference", 0.8)
    assert values["importance"] > 0.5
    assert values["expires_at"] > datetime.now(timezone.utc) + timedelta(days=30)
    assert values["decay_type"] == "exponential"


def test_decay_and_freshness_are_bounded():
    assert decay_score("exponential", 60, 0) == 1.0
    assert 0 < decay_score("exponential", 60, 30) < 1
    assert decay_score("linear", 10, 20) == 0.0
    assert decay_score("step", 10, 11) == 0.05
    item = {"last_accessed_at": datetime.now(timezone.utc) - timedelta(days=30), "ttl_days": 60, "decay_type": "exponential"}
    assert 0 < freshness(item) < 1
    assert freshness({"created_at": datetime.now(timezone.utc), "ttl_days": 60}) > 0.99


def test_access_reinforcement_extends_expiry_but_never_shrinks_it():
    current = datetime.now(timezone.utc)
    item = {
        "kind": "preference",
        "importance": 0.8,
        "expires_at": current + timedelta(days=5),
        "ttl_days": 60,
    }
    extended = extend_after_access(item, access_count=2, reference=current)
    assert extended["access_count"] == 2
    assert extended["last_accessed_at"] == current
    assert extended["expires_at"] > item["expires_at"]
