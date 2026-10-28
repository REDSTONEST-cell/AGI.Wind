"""Local ambient observations (derived desktop awareness, deliberately not archived)."""
from alembic import op

from agi_wind.ambient.models import ambient_observations

revision = "0018_ambient_observations"
down_revision = "0017_agent_continuation"


def upgrade():
    ambient_observations.create(op.get_bind(), checkfirst=True)


def downgrade():
    ambient_observations.drop(op.get_bind(), checkfirst=True)
