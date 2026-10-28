from alembic import op
from nagi.archive.models import stickers

revision = '0008_reasoning_stickers'
down_revision = '0007_conversation_lifecycle'


def upgrade():
    stickers.create(op.get_bind(), checkfirst=True)


def downgrade():
    stickers.drop(op.get_bind(), checkfirst=True)
