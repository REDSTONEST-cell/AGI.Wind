"""Memory extraction lifecycle columns and event scan index (phase 1)."""
from alembic import op
import sqlalchemy as sa

revision = "0009_memory_extraction"
down_revision = "0008_reasoning_stickers"


def upgrade():
    # Earlier revisions create tables from live metadata, so a fresh database already has
    # these columns. Only alter an existing archive that predates this revision.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("companions")}
    if "memory_extract_cursor_at" not in columns:
        op.add_column("companions", sa.Column("memory_extract_cursor_at", sa.DateTime(timezone=True), nullable=True))
    if "memory_extract_cursor_id" not in columns:
        op.add_column("companions", sa.Column("memory_extract_cursor_id", sa.UUID(as_uuid=True), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS events_companion_recorded_idx ON events (companion_id, recorded_at, id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS events_companion_recorded_idx")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("companions")}
    if "memory_extract_cursor_id" in columns:
        op.drop_column("companions", "memory_extract_cursor_id")
    if "memory_extract_cursor_at" in columns:
        op.drop_column("companions", "memory_extract_cursor_at")
