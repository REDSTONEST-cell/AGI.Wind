"""Daily token/cost quota ledger and enforcement tests.

These are database-backed on purpose: the ledger is the source of truth for quota
totals. The ``ledger`` fixture creates ``usage_ledger`` in the per-test PostgreSQL
schema because the matching Alembic migration is attached by the Lead separately;
``checkfirst=True`` makes it a no-op once that migration exists.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from nagi.core.types import Actor
from nagi.usage import (
    QuotaService,
    QuotaSettings,
    USAGE_TABLES,
    category_limit,
    day_bounds,
    estimate_usage,
    usage_ledger,
)

UTC = timezone.utc
CN = timezone(timedelta(hours=8))
DAY = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def ledger(db):
    usage_ledger.create(db.engine, checkfirst=True)
    return db


def quota(**overrides):
    values = {"enabled": True}
    values.update(overrides)
    return QuotaSettings(**values)


def service(**overrides):
    return QuotaService(quota(**overrides))


def write(db, scope, category, prompt=0, completion=0, cost=0.0, *, estimated=False,
          now=DAY, service=None):
    service = service or QuotaService(QuotaSettings())
    with db.engine.begin() as conn:
        return service.record(
            category,
            conn,
            scope.account_id,
            scope.companion_id,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cost_cny=cost,
            estimated=estimated,
            now=now,
        )


def read(svc, db, scope, category="web", now=DAY):
    with db.engine.connect() as conn:
        return svc.check(category, conn, account_id=scope.account_id, now=now)


def test_disabled_quota_allows_without_touching_the_ledger():
    decision = QuotaService(QuotaSettings()).check("web", None)
    assert decision.allowed is True
    assert decision.remaining is None and decision.cost_remaining is None
    assert decision.reason is None and decision.warning is False


def test_zero_limits_are_unlimited(ledger, seeded_scope):
    svc = service()
    write(ledger, seeded_scope, "web", 500, 500, 9.99)
    decision = read(svc, ledger, seeded_scope)
    assert decision.allowed is True
    assert decision.remaining is None and decision.cost_remaining is None
    assert decision.warning is False and decision.reason is None


def test_per_category_limits_only_apply_to_their_category(ledger, seeded_scope):
    svc = service(daily_web_tokens=100, daily_qq_tokens=50)
    write(ledger, seeded_scope, "web", 60)
    decision = read(svc, ledger, seeded_scope, "web")
    assert decision.allowed is True
    assert decision.remaining == 40
    assert decision.reason is None and decision.warning is False

    write(ledger, seeded_scope, "qq", 60)
    blocked = read(svc, ledger, seeded_scope, "qq")
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert blocked.reason == "daily_qq_tokens"

    # The web bucket is independent, and memory has no per-category limit here.
    assert read(svc, ledger, seeded_scope, "web").allowed is True
    assert read(svc, ledger, seeded_scope, "memory").allowed is True


def test_total_limit_blocks_every_category(ledger, seeded_scope):
    svc = service(daily_total_tokens=100, daily_web_tokens=1000)
    write(ledger, seeded_scope, "web", 60)
    write(ledger, seeded_scope, "qq", 30)
    assert read(svc, ledger, seeded_scope, "memory").remaining == 10

    write(ledger, seeded_scope, "web", 15)
    for category in ("web", "qq", "memory", "purpose", "other"):
        decision = read(svc, ledger, seeded_scope, category)
        assert decision.allowed is False
        assert decision.reason == "daily_total_tokens"
        assert decision.remaining == 0


def test_cost_limit_blocks_usage_and_reports_remaining(ledger, seeded_scope):
    svc = service(daily_cost_cny=1.0)
    write(ledger, seeded_scope, "web", 10, cost=0.79)
    decision = read(svc, ledger, seeded_scope)
    assert decision.allowed is True
    assert decision.cost_remaining == pytest.approx(0.21)
    assert decision.warning is False

    write(ledger, seeded_scope, "web", 10, cost=0.01)
    warned = read(svc, ledger, seeded_scope)
    assert warned.allowed is True
    assert warned.warning is True

    write(ledger, seeded_scope, "web", 10, cost=0.2)
    blocked = read(svc, ledger, seeded_scope)
    assert blocked.allowed is False
    assert blocked.reason == "daily_cost_cny"
    assert blocked.cost_remaining == pytest.approx(0.0)


def test_warning_boundary_is_inclusive(ledger, seeded_scope):
    svc = service(daily_total_tokens=100, warning_percent=80)
    write(ledger, seeded_scope, "web", 79)
    assert read(svc, ledger, seeded_scope).warning is False

    write(ledger, seeded_scope, "web", 1)  # exactly 80%
    warned = read(svc, ledger, seeded_scope)
    assert warned.allowed is True
    assert warned.warning is True
    assert warned.remaining == 20
    assert warned.reason is None

    write(ledger, seeded_scope, "web", 20)  # exactly the limit
    blocked = read(svc, ledger, seeded_scope)
    assert blocked.allowed is False
    assert blocked.reason == "daily_total_tokens"
    assert blocked.remaining == 0


def test_hard_stop_false_only_warns_and_keeps_allowing(ledger, seeded_scope):
    svc = service(daily_total_tokens=100, hard_stop=False)
    write(ledger, seeded_scope, "web", 150)
    decision = read(svc, ledger, seeded_scope)
    assert decision.allowed is True
    assert decision.warning is True
    assert decision.reason == "daily_total_tokens"
    assert decision.remaining == 0


def test_local_day_rollover_uses_timezone_aware_bounds(ledger, seeded_scope):
    start, end = day_bounds(datetime(2026, 1, 1, 23, 0, tzinfo=CN), CN)
    assert start == datetime(2025, 12, 31, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 1, 1, 16, 0, tzinfo=UTC)

    svc = QuotaService(quota(daily_web_tokens=100), timezone=CN)
    write(ledger, seeded_scope, "web", 50, now=datetime(2026, 1, 1, 23, 0, tzinfo=CN))
    same_day = read(svc, ledger, seeded_scope, now=datetime(2026, 1, 1, 23, 30, tzinfo=CN))
    assert same_day.remaining == 50
    next_day = read(svc, ledger, seeded_scope, now=datetime(2026, 1, 2, 0, 30, tzinfo=CN))
    assert next_day.allowed is True
    assert next_day.remaining == 100


def test_accounts_are_isolated(ledger, seeded_scope):
    other_scope = ledger.seed(Actor(account_id=uuid4(), user_id=uuid4()))
    svc = service(daily_web_tokens=100)
    write(ledger, seeded_scope, "web", 90)
    # The other account asked later and has its own day budget.
    assert read(svc, ledger, other_scope, "web").remaining == 100
    assert read(svc, ledger, seeded_scope, "web").remaining == 10


def test_record_persists_row_and_estimated_helper_marks_estimates(ledger, seeded_scope):
    usage = estimate_usage("你好世界", "hello world")
    assert usage == {"prompt_tokens": 3, "completion_tokens": 3, "estimated": True}

    row = write(ledger, seeded_scope, "purpose", estimated=usage["estimated"],
                prompt=usage["prompt_tokens"], completion=usage["completion_tokens"],
                cost=0.0003, service=QuotaService(QuotaSettings()))
    assert row["estimated"] is True
    assert row["total_tokens"] == 6
    assert row["cost_cny"] == pytest.approx(0.0003)
    assert row["model_ref"] is None

    rows = ledger.all("usage_ledger", account_id=seeded_scope.account_id)
    assert len(rows) == 1
    assert rows[0]["category"] == "purpose"
    assert rows[0]["estimated"] is True

    actual = write(ledger, seeded_scope, "web", 10, 5, service=QuotaService(QuotaSettings()))
    assert actual["estimated"] is False
    assert actual["total_tokens"] == 15


def test_record_and_check_reject_invalid_input(ledger, seeded_scope):
    svc = QuotaService(QuotaSettings())
    with ledger.engine.begin() as conn:
        with pytest.raises(ValueError):
            svc.record("web", conn, seeded_scope.account_id, seeded_scope.companion_id, prompt_tokens=-1)
        with pytest.raises(ValueError):
            svc.record("web", conn, seeded_scope.account_id, seeded_scope.companion_id, cost_cny=-0.01)
        with pytest.raises(ValueError):
            svc.record("chat", conn, seeded_scope.account_id, seeded_scope.companion_id)
        with pytest.raises(ValueError):
            svc.check("chat", conn, account_id=seeded_scope.account_id)


def test_summary_totals_categories_and_window(ledger, seeded_scope):
    svc = QuotaService(quota(daily_total_tokens=1000), timezone=UTC)
    write(ledger, seeded_scope, "web", 10, 5, cost=0.1, now=datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
    write(ledger, seeded_scope, "qq", 20, cost=0.2, now=datetime(2026, 1, 1, 11, 0, tzinfo=UTC))
    write(ledger, seeded_scope, "memory", 0, 7, now=datetime(2026, 1, 1, 12, 0, tzinfo=UTC))
    write(ledger, seeded_scope, "other", 1, estimated=True, now=datetime(2026, 1, 1, 13, 0, tzinfo=UTC))

    with ledger.engine.connect() as conn:
        summary = svc.summary(conn, seeded_scope.account_id, now=DAY)

    assert summary["enabled"] is True
    assert summary["prompt_tokens"] == 31
    assert summary["completion_tokens"] == 12
    assert summary["total_tokens"] == 43
    assert summary["cost_cny"] == pytest.approx(0.3)
    assert summary["entries"] == 4
    assert summary["estimated_entries"] == 1
    assert set(summary["categories"]) == {"web", "qq", "memory", "other"}
    assert summary["categories"]["web"]["total_tokens"] == 15
    assert summary["limits"]["daily_total_tokens"] == 1000
    assert summary["window"]["since"] == datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    assert summary["window"]["until"] == datetime(2026, 1, 2, tzinfo=UTC).isoformat()

    with ledger.engine.connect() as conn:
        windowed = svc.summary(
            conn,
            seeded_scope.account_id,
            since=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            until=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
        )
    assert windowed["entries"] == 1
    assert windowed["total_tokens"] == 20
    assert set(windowed["categories"]) == {"qq"}


def test_ledger_table_columns_are_stable_contract():
    assert USAGE_TABLES == ["usage_ledger"]
    assert set(usage_ledger.c.keys()) == {
        "id", "account_id", "companion_id", "generation_id", "category",
        "prompt_tokens", "completion_tokens", "total_tokens", "cost_cny",
        "estimated", "model_ref", "created_at",
    }
    assert usage_ledger.c.generation_id.nullable is True
    assert usage_ledger.c.estimated.nullable is False


def test_service_accepts_host_settings_shapes():
    direct = QuotaService(QuotaSettings(enabled=True, daily_total_tokens=5))
    assert direct.quota.daily_total_tokens == 5

    nested = QuotaService(SimpleNamespace(usage=QuotaSettings(enabled=True, daily_web_tokens=7)))
    assert nested.quota.daily_web_tokens == 7

    mapped = QuotaService({"usage": {"enabled": True, "daily_cost_cny": 2}})
    assert mapped.quota.daily_cost_cny == 2

    flat = QuotaService(SimpleNamespace(enabled=True, daily_qq_tokens=9))
    assert flat.quota.daily_qq_tokens == 9

    assert QuotaService(None).quota.enabled is False
    assert category_limit(QuotaSettings(daily_memory_tokens=42), "memory") == 42
    assert category_limit(QuotaSettings(daily_memory_tokens=42), "purpose") == 0
