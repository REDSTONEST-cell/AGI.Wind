"""Operational purpose/closure state (not archive history)."""

from sqlalchemy import Table, Column, Text, Boolean, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB

from ..archive.models import metadata, idcol, fk, ts, js

purpose_state = Table(
    "purpose_state",
    metadata,
    idcol(),
    fk("account_id", "accounts"),
    fk("companion_id", "companions"),
    Column("closure", JSONB, nullable=True),
    Column("last_decision", JSONB, nullable=True),
    ts("updated_at"),
    UniqueConstraint("account_id", "companion_id", name="purpose_state_companion_uniq"),
)

purpose_runs = Table(
    "purpose_runs",
    metadata,
    idcol(),
    fk("account_id", "accounts"),
    fk("companion_id", "companions"),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")),
    Column("decision", JSONB, nullable=False, default=dict),
    Column("delivered", Boolean, nullable=False, default=False, server_default="false"),
    ts(),
)

Index("purpose_runs_account_created_idx", purpose_runs.c.account_id, purpose_runs.c.created_at)
Index("purpose_runs_companion_created_idx", purpose_runs.c.companion_id, purpose_runs.c.created_at)

PURPOSE_TABLES = ["purpose_state", "purpose_runs"]
