"""Persist bounded conversation task checkpoints without duplicating source text."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0017_agent_continuation'
down_revision = '0016_idle_cognition_started'


def upgrade():
    columns = {column['name'] for column in sa.inspect(op.get_bind()).get_columns('idle_cognition_runs')}
    if 'checkpoint' not in columns:
        op.add_column('idle_cognition_runs', sa.Column('checkpoint', JSONB, nullable=False,
                                                      server_default=sa.text("'{}'::jsonb")))


def downgrade():
    op.drop_column('idle_cognition_runs', 'checkpoint')
