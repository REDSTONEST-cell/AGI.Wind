from alembic import op
import sqlalchemy as sa

revision = "0007_conversation_lifecycle"
down_revision = "0006_restore_origins"


def upgrade():
    # Earlier revisions create tables from the live metadata, so a brand-new database may
    # already contain the column/constraint before this revision runs. Only add what an
    # already-migrated database is missing.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "state" not in columns:
        op.add_column("conversations", sa.Column("state", sa.Text(), nullable=False, server_default="active"))
    constraints = {constraint["name"] for constraint in inspector.get_check_constraints("conversations")}
    if "conversations_state_check" not in constraints:
        op.create_check_constraint("conversations_state_check", "conversations", "state IN ('active','archived','hidden')")
    # The single-user companion keeps one active conversation. Older databases may hold
    # several; archive all but the newest so the invariant starts clean.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY companion_id ORDER BY created_at DESC, id DESC) AS position
            FROM conversations WHERE state = 'active'
        )
        UPDATE conversations AS conversation SET state = 'archived'
        FROM ranked WHERE conversation.id = ranked.id AND ranked.position > 1
        """
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    constraints = {constraint["name"] for constraint in inspector.get_check_constraints("conversations")}
    if "conversations_state_check" in constraints:
        op.drop_constraint("conversations_state_check", "conversations", type_="check")
    columns = {column["name"] for column in inspector.get_columns("conversations")}
    if "state" in columns:
        op.drop_column("conversations", "state")
