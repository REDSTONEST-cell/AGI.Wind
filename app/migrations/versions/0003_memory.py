from alembic import op
from sqlalchemy import text
from nagi.memory.models import MEMORY_TABLES
from nagi.archive.models import metadata
revision = "0003_memory"
down_revision = "0002_identity"
def upgrade():
    metadata.create_all(op.get_bind(), tables=[metadata.tables[name] for name in MEMORY_TABLES])
    op.execute("CREATE INDEX chunks_terms_idx ON retrieval_chunks USING gin (to_tsvector('simple', search_terms))")
def downgrade(): metadata.drop_all(op.get_bind(), tables=[metadata.tables[name] for name in reversed(MEMORY_TABLES)])
