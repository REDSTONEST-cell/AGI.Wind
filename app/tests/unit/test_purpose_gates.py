"""Unit tests for purpose settings, quiet-hour helpers and the pure gate matrix."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nagi.purpose import (
    PurposeDecision,
    PurposeSettings,
    decide,
    gate_reason,
    in_quiet_hours,
    parse_clock,
)

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------- settings


def test_settings_defaults_are_off_and_bounded() -> None:
    settings = PurposeSettings()
    assert settings.enabled is False
    assert settings.carrier == "qq"
    assert settings.min_idle_minutes == 30
    assert settings.min_gap_minutes == 90
    assert settings.max_per_day == 4
    assert settings.quiet_start == "23:30"
    assert settings.quiet_end == "08:30"
    assert settings.closure_enabled is True
    assert settings.closure_ttl_hours == 72


def test_settings_dump_round_trips_for_persistence() -> None:
    payload = PurposeSettings(enabled=True).model_dump(mode="json")
    assert set(payload) == {
        "enabled",
        "carrier",
        "min_idle_minutes",
        "min_gap_minutes",
        "max_per_day",
        "quiet_start",
        "quiet_end",
        "closure_enabled",
        "closure_ttl_hours",
    }
    assert PurposeSettings.model_validate(payload).enabled is True


def test_settings_carrier_is_cleaned() -> None:
    assert PurposeSettings(carrier=" QQ ").carrier == "qq"


@pytest.mark.parametrize("carrier", ["", "   ", "x" * 33])
def test_settings_rejects_bad_carrier(carrier: str) -> None:
    with pytest.raises(ValidationError):
        PurposeSettings(carrier=carrier)


@pytest.mark.parametrize("quiet_start,quiet_end", [("24:00", "08:30"), ("8:30", "08:30"), ("23:60", "08:30"), ("2330", "08:30")])
def test_settings_rejects_bad_clock(quiet_start: str, quiet_end: str) -> None:
    with pytest.raises(ValidationError):
        PurposeSettings(quiet_start=quiet_start, quiet_end=quiet_end)


@pytest.mark.parametrize(
    "field,value",
    [
        ("min_idle_minutes", 0),
        ("min_idle_minutes", 1441),
        ("min_gap_minutes", 0),
        ("max_per_day", 0),
        ("max_per_day", 21),
        ("closure_ttl_hours", 0),
    ],
)
def test_settings_rejects_out_of_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        PurposeSettings(**{field: value})


# ------------------------------------------------------------------ quiet hours


def test_parse_clock() -> None:
    assert parse_clock("08:30") == (8, 30)
    assert parse_clock("23:59") == (23, 59)


def test_in_quiet_hours_wraps_midnight() -> None:
    assert in_quiet_hours(datetime(2026, 10, 28, 23, 45), "23:30", "08:30") is True
    assert in_quiet_hours(datetime(2026, 10, 29, 0, 10), "23:30", "08:30") is True
    assert in_quiet_hours(datetime(2026, 10, 28, 8, 29), "23:30", "08:30") is True
    assert in_quiet_hours(datetime(2026, 10, 28, 8, 30), "23:30", "08:30") is False
    assert in_quiet_hours(datetime(2026, 10, 28, 12, 0), "23:30", "08:30") is False


def test_in_quiet_hours_same_day_range() -> None:
    assert in_quiet_hours(datetime(2026, 10, 28, 9, 0), "09:00", "17:00") is True
    assert in_quiet_hours(datetime(2026, 10, 28, 17, 0), "09:00", "17:00") is False
    assert in_quiet_hours(datetime(2026, 10, 28, 8, 59), "09:00", "17:00") is False


def test_in_quiet_hours_equal_bounds_never_quiet() -> None:
    assert in_quiet_hours(datetime(2026, 10, 28, 12, 0), "10:00", "10:00") is False


# ---------------------------------------------------------------------- gates


def enabled(**overrides: object) -> PurposeSettings:
    payload = {"enabled": True, "quiet_start": "23:30", "quiet_end": "08:30"}
    payload.update(overrides)
    return PurposeSettings(**payload)


def test_gate_reason_disabled() -> None:
    assert gate_reason(NOW) == "disabled"


def test_gate_reason_ok() -> None:
    last_user = NOW - timedelta(hours=2)
    assert gate_reason(NOW, last_user_at=last_user, settings=enabled()) == "ok"


def test_gate_reason_quiet_hours() -> None:
    night = datetime(2026, 10, 28, 23, 45, tzinfo=timezone.utc)
    assert gate_reason(night, settings=enabled()) == "quiet_hours"


def test_gate_reason_not_idle() -> None:
    assert (
        gate_reason(NOW, last_user_at=NOW.replace(hour=9, minute=45), settings=enabled())
        == "not_idle"
    )


def test_gate_reason_idle_boundary_is_inclusive() -> None:
    last_user = NOW.replace(hour=9, minute=30)
    assert gate_reason(NOW, last_user_at=last_user, settings=enabled()) == "ok"


def test_gate_reason_gap() -> None:
    last_delivery = NOW.replace(hour=9, minute=30)
    assert gate_reason(NOW, last_delivery_at=last_delivery, settings=enabled()) == "gap"


def test_gate_reason_daily_cap() -> None:
    assert gate_reason(NOW, sent_today=4, settings=enabled()) == "daily_cap"
    assert gate_reason(NOW, sent_today=3, settings=enabled()) == "ok"


def test_gate_reason_bad_sent_today_counts_as_zero() -> None:
    assert gate_reason(NOW, sent_today=-3, settings=enabled()) == "ok"


def test_gate_accepts_whole_settings_object() -> None:
    wrapper = SimpleNamespace(purpose=enabled())
    assert gate_reason(NOW, settings=wrapper) == "ok"


def test_gate_accepts_settings_mapping() -> None:
    assert gate_reason(NOW, settings={"purpose": {"enabled": True}}) == "ok"


def test_gate_rejects_unknown_settings_type() -> None:
    with pytest.raises(TypeError):
        gate_reason(NOW, settings=object())


def test_decide_returns_none_when_disabled() -> None:
    events = [{"role": "user", "content": "今天加班好累"}]
    assert decide(NOW, recent_events=events) is None


def test_decide_returns_none_inside_quiet_hours() -> None:
    night = datetime(2026, 10, 28, 23, 45, tzinfo=timezone.utc)
    events = [{"role": "user", "content": "今天加班好累"}]
    assert decide(night, recent_events=events, settings=enabled()) is None


def test_decide_returns_none_when_cap_reached() -> None:
    events = [{"role": "user", "content": "今天加班好累"}]
    assert decide(NOW, sent_today=4, recent_events=events, settings=enabled()) is None


def test_decide_ignores_last_user_inside_min_idle() -> None:
    events = [{"role": "user", "content": "今天加班好累"}]
    decision = decide(
        NOW,
        last_user_at=NOW.replace(hour=9, minute=45),
        recent_events=events,
        settings=enabled(),
    )
    assert decision is None


def test_decide_is_deterministic() -> None:
    events = [{"role": "user", "content": "明天面试"}]
    first = decide(NOW, recent_events=events, settings=enabled())
    second = decide(NOW, recent_events=events, settings=enabled())
    assert isinstance(first, PurposeDecision)
    assert first == second
