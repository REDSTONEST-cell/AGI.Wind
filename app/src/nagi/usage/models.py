"""Authoritative per-account usage ledger.

One row per metered provider call (or explicitly recorded purpose). Runtime
statistics in ``usage.overview`` stay derived from the archive; this table is the
only write path and exists so quota checks never have to infer cost.
"""

from sqlalchemy import Table, Column, Text, Integer, Float, Boolean, ForeignKey, Index, CheckConstraint, text
from sqlalchemy.dialects.postgresql import UUID, JSONB

from ..archive.models import metadata, idcol, fk, ts

# Operational, derived state: excluded from archive exports like memory_usage.
usage_ledger = Table(
    "usage_ledger",
    metadata,
    idcol(),
    fk("account_id", "accounts"),
    fk("companion_id", "companions"),
    Column("generation_id", UUID(as_uuid=True), ForeignKey("generations.id", ondelete="SET NULL")),
    Column("category", Text, nullable=False),
    Column("prompt_tokens", Integer, nullable=False, default=0, server_default="0"),
    Column("completion_tokens", Integer, nullable=False, default=0, server_default="0"),
    Column("total_tokens", Integer, nullable=False, default=0, server_default="0"),
    Column("cost_cny", Float, nullable=False, default=0.0, server_default="0"),
    Column("estimated", Boolean, nullable=False, default=False, server_default=text("false")),
    Column("model_ref", JSONB, nullable=True),
    ts(),
    CheckConstraint(
        "category IN ('web','qq','memory','purpose','other')",
        name="usage_ledger_category_check",
    ),
    CheckConstraint(
        "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
        name="usage_ledger_tokens_check",
    ),
)

# Daily aggregation always filters by account and a UTC instant window.
Index("usage_ledger_account_created_idx", usage_ledger.c.account_id, usage_ledger.c.created_at)
Index("usage_ledger_generation_idx", usage_ledger.c.generation_id)

USAGE_TABLES = ["usage_ledger"]
