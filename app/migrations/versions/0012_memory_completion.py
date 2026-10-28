"""Structured memory authority and rebuildable semantic/operational storage."""
from alembic import op
import sqlalchemy as sa
from nagi.memory.models import MEMORY_DERIVED_TABLES

revision = '0012_memory_completion'
down_revision = '0011_proactive'


def upgrade():
    bind = op.get_bind()
    existing = {item['name'] for item in sa.inspect(bind).get_columns('memory_items')}
    fields = [sa.Column(name, sa.Text(), nullable=False, server_default='')
              for name in ('subject_key', 'predicate', 'value', 'summary_level')]
    fields += [sa.Column(name, sa.DateTime(timezone=True)) for name in ('valid_from', 'valid_to')]
    fields += [sa.Column('is_pinned', sa.Boolean(), nullable=False, server_default=sa.false())]
    for field in fields:
        if field.name not in existing:
            op.add_column('memory_items', field)
    for table in MEMORY_DERIVED_TABLES:
        table.create(bind, checkfirst=True)
    op.execute('CREATE INDEX IF NOT EXISTS memory_items_semantic_slot_idx ON memory_items (companion_id, subject_key, predicate, status)')
    op.execute('CREATE INDEX IF NOT EXISTS memory_usage_account_day_idx ON memory_usage (account_id, billing_day)')
    op.execute('CREATE INDEX IF NOT EXISTS source_refs_memory_idx ON source_refs (memory_id)')
    op.execute('CREATE INDEX IF NOT EXISTS source_refs_event_idx ON source_refs (event_id)')
    op.execute('CREATE INDEX IF NOT EXISTS retrieval_chunks_source_idx ON retrieval_chunks (source_id, source_hash)')


def downgrade():
    op.execute('DROP INDEX IF EXISTS retrieval_chunks_source_idx')
    op.execute('DROP INDEX IF EXISTS source_refs_event_idx')
    op.execute('DROP INDEX IF EXISTS source_refs_memory_idx')
    op.execute('DROP INDEX IF EXISTS memory_usage_account_day_idx')
    op.execute('DROP INDEX IF EXISTS memory_items_semantic_slot_idx')
    for table in reversed(MEMORY_DERIVED_TABLES):
        table.drop(op.get_bind(), checkfirst=True)
    existing = {item['name'] for item in sa.inspect(op.get_bind()).get_columns('memory_items')}
    for name in ('subject_key', 'predicate', 'value', 'summary_level', 'valid_from', 'valid_to', 'is_pinned'):
        if name in existing:
            op.drop_column('memory_items', name)
