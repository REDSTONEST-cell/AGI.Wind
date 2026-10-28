from alembic import op
from nagi.archive.provenance import archive_origins
revision="0006_restore_origins"
down_revision="0005_deletions"
def upgrade(): archive_origins.create(op.get_bind())
def downgrade(): archive_origins.drop(op.get_bind())
