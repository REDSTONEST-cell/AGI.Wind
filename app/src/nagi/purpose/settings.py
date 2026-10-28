"""Purpose-driven proactive messages: settings and pure quiet-hour helpers.

The module is off by default and every bound is enforced by the pure engine in
``engine.py`` rather than by the model. This file owns configuration only; the
storage-backed service lives in ``service.py``.

``in_quiet_hours`` intentionally mirrors ``delivery.proactive.in_quiet_hours`` so the
purpose package stays free of database imports and can be tested without a gateway.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from ..core.types import DTO


def parse_clock(value: str) -> tuple[int, int]:
    """Parse an ``HH:MM`` wall-clock string into ``(hour, minute)``."""
    hour, minute = str(value or "").split(":")
    return int(hour), int(minute)


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """Quiet range is local wall-clock; equal start/end means no quiet window."""
    start_h, start_m = parse_clock(start)
    end_h, end_m = parse_clock(end)
    if (start_h, start_m) == (end_h, end_m):
        return False
    minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    if start_minutes < end_minutes:
        return start_minutes <= minutes < end_minutes
    return minutes >= start_minutes or minutes < end_minutes


class PurposeSettings(DTO):
    """Bounded purpose-driven proactive messages. Disabled until the owner enables it."""

    enabled: bool = False
    # Delivery carrier chosen by the owner. The gateway maps it to a channel; the
    # engine itself never sends anything.
    carrier: str = "qq"
    min_idle_minutes: int = Field(default=30, ge=1, le=1440)
    min_gap_minutes: int = Field(default=90, ge=1, le=1440)
    max_per_day: int = Field(default=4, ge=1, le=20)
    quiet_start: str = "23:30"
    quiet_end: str = "08:30"
    # When enabled, a delivered purpose stores one closure record (topic, unanswered
    # question, next opening) so a later tick can follow up on an explicit opening.
    closure_enabled: bool = True
    closure_ttl_hours: int = Field(default=72, ge=1, le=720)

    @field_validator("carrier")
    @classmethod
    def clean_carrier(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        if not text or len(text) > 32:
            raise ValueError("carrier must be a non-empty short channel name")
        return text

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def clock(cls, value: str) -> str:
        text = str(value or "").strip()
        if len(text) != 5 or text[2] != ":" or not (text[:2] + text[3:]).isdigit():
            raise ValueError("quiet hours must use HH:MM")
        hour, minute = int(text[:2]), int(text[3:])
        if hour > 23 or minute > 59:
            raise ValueError("quiet hours must use a valid HH:MM time")
        return text
