"""Usage accounting: read-only archive statistics plus the quota ledger.

This package replaces the former ``nagi.usage`` module. Import sites such as
``from nagi.usage import ProcessMonitor, usage_overview`` and
``from nagi.usage import estimate_tokens`` keep working through the
re-exports below.
"""

from .models import USAGE_TABLES, usage_ledger
from .overview import (
    ProcessMonitor,
    estimate_tokens,
    process_working_set_bytes,
    usage_overview,
)
from .service import (
    CATEGORIES,
    CATEGORY_LIMIT_FIELDS,
    QuotaDecision,
    QuotaService,
    category_limit,
    day_bounds,
    estimate_usage,
    local_timezone,
)
from .settings import QuotaSettings

__all__ = [
    "CATEGORIES",
    "CATEGORY_LIMIT_FIELDS",
    "ProcessMonitor",
    "QuotaDecision",
    "QuotaService",
    "QuotaSettings",
    "USAGE_TABLES",
    "category_limit",
    "day_bounds",
    "estimate_tokens",
    "estimate_usage",
    "local_timezone",
    "process_working_set_bytes",
    "usage_ledger",
    "usage_overview",
]
