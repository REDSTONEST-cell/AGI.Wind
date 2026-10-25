from alembic import op
from agi_wind.archive.deletions import deletion_tombstones
revision="0005_deletions"
down_revision="0004_delivery"
def upgrade(): deletion_tombstones.create(op.get_bind())
def downgrade(): deletion_tombstones.drop(op.get_bind())
