"""Memory lifecycle: importance, TTL/expiry, access reinforcement (phase 2, inspired by
LivingMemory's atom model; implemented independently, no AGPL code copied)."""
from alembic import op
import sqlalchemy as sa

revision = "0010_memory_lifecycle"
down_revision = "0009_memory_extraction"

_COLUMNS = {
    "importance": sa.Column("importance", sa.Float(), nullable=False, server_default=sa.text("0.5")),
    "access_count": sa.Column("access_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    "last_accessed_at": sa.Column("last_accessed_at", sa.DateTime(timezone=True), nullable=True),
    "last_reinforced_at": sa.Column("last_reinforced_at", sa.DateTime(timezone=True), nullable=True),
    "ttl_days": sa.Column("ttl_days", sa.Float(), nullable=False, server_default=sa.text("30")),
    "expires_at": sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    "decay_type": sa.Column("decay_type", sa.Text(), nullable=False, server_default=sa.text("'exponential'")),
    "archived_at": sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("memory_items")}
    for name, column in _COLUMNS.items():
        if name not in existing:
            op.add_column("memory_items", column)
    op.execute("CREATE INDEX IF NOT EXISTS memory_items_companion_status_expiry_idx ON memory_items (companion_id, status, expires_at)")
    op.execute("CREATE INDEX IF NOT EXISTS memory_items_companion_importance_idx ON memory_items (companion_id, importance)")
    # Existing memories predate the lifecycle: give them a grace window instead of hiding
    # everything at once. New rows get a kind-specific TTL at write time.
    op.execute("UPDATE memory_items SET expires_at = now() + interval '90 days' WHERE status = 'active' AND expires_at IS NULL")


def downgrade():
    op.execute("DROP INDEX IF EXISTS memory_items_companion_importance_idx")
    op.execute("DROP INDEX IF EXISTS memory_items_companion_status_expiry_idx")
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("memory_items")}
    for name in reversed(list(_COLUMNS)):
        if name in existing:
            op.drop_column("memory_items", name)
