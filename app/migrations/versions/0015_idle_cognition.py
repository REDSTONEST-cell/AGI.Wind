"""After-reply idle cognition runs (derived operational state, not exported)."""
from alembic import op

from agi_wind.delivery.models import idle_cognition_runs

revision = '0015_idle_cognition'
down_revision = '0014_qq_channels'


def upgrade():
    idle_cognition_runs.create(op.get_bind(), checkfirst=True)


def downgrade():
    idle_cognition_runs.drop(op.get_bind(), checkfirst=True)
