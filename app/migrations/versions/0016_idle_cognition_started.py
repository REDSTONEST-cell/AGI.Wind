"""Track actual first model invocation for daily idle-cognition quotas."""
from alembic import op
import sqlalchemy as sa

revision = '0016_idle_cognition_started'
down_revision = '0015_idle_cognition'


def upgrade():
    bind = op.get_bind()
    columns = {column['name'] for column in sa.inspect(bind).get_columns('idle_cognition_runs')}
    if 'started_at' not in columns:
        op.add_column('idle_cognition_runs', sa.Column('started_at', sa.DateTime(timezone=True)))
    # Older runs lack an exact first-call timestamp. Preserve a conservative
    # historical estimate only for runs that recorded an actual model round.
    op.execute(sa.text('UPDATE idle_cognition_runs SET started_at = created_at '
                       'WHERE started_at IS NULL AND rounds_used > 0'))


def downgrade():
    op.drop_column('idle_cognition_runs', 'started_at')
