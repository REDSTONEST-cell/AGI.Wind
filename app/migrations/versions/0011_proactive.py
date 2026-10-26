from alembic import op
from agi_wind.delivery.models import proactive_messages

revision = '0011_proactive'
down_revision = '0010_memory_lifecycle'


def upgrade():
    proactive_messages.create(op.get_bind(), checkfirst=True)


def downgrade():
    proactive_messages.drop(op.get_bind(), checkfirst=True)
