"""Authoritative archive and durable request/outbox transaction."""
from alembic import op
from agi_wind.archive.models import metadata, ARCHIVE_TABLES
revision = "0001_archive"
down_revision = None

def upgrade():
    metadata.create_all(op.get_bind(), tables=[metadata.tables[name] for name in ARCHIVE_TABLES])

def downgrade():
    metadata.drop_all(op.get_bind(), tables=[metadata.tables[name] for name in ARCHIVE_TABLES])
