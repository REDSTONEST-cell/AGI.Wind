"""Remove the retired desktop ambient-observation feature."""
from alembic import op

revision = "0020_drop_ambient_observations"
down_revision = "0019_nagi_modules"


def upgrade():
    op.drop_table("ambient_observations")


def downgrade():
    # Recreate the retired table only so a downgrade can be traversed. 0020 never
    # restores the feature code; this is schema compatibility only.
    import sqlalchemy as sa
    from sqlalchemy.dialects import postgresql

    op.create_table(
        "ambient_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_title", sa.Text(), nullable=False, server_default=""),
        sa.Column("process_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("process_id", sa.Integer(), nullable=True),
        sa.Column("idle_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("screenshot_path", sa.Text(), nullable=True),
        sa.Column("screenshot_width", sa.Integer(), nullable=True),
        sa.Column("screenshot_height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ambient_observations_owner_time_idx",
        "ambient_observations",
        ["account_id", "captured_at"],
    )
