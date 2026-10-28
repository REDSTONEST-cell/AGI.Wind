from alembic import op
from nagi.identity.models import IDENTITY_TABLES
from nagi.archive.models import metadata
revision = "0002_identity"
down_revision = "0001_archive"
def upgrade(): metadata.create_all(op.get_bind(), tables=[metadata.tables[name] for name in IDENTITY_TABLES])
def downgrade(): metadata.drop_all(op.get_bind(), tables=[metadata.tables[name] for name in reversed(IDENTITY_TABLES)])
