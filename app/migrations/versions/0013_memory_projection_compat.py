"""Bring pre-existing experimental memory projections up to the final schema."""
from alembic import op
import sqlalchemy as sa
from agi_wind.memory.models import MEMORY_DERIVED_TABLES

revision = '0013_memory_projection_compat'
down_revision = '0012_memory_completion'


def upgrade():
    bind = op.get_bind()
    for table in MEMORY_DERIVED_TABLES:
        table.create(bind, checkfirst=True)
    existing = {item['name'] for item in sa.inspect(bind).get_columns('memory_usage')}
    columns = [sa.Column('input_rate',sa.Float(),nullable=False,server_default='0'),
               sa.Column('output_rate',sa.Float(),nullable=False,server_default='0'),
               sa.Column('usage_estimated',sa.Boolean(),nullable=False,server_default=sa.true())]
    for column in columns:
        if column.name not in existing:
            op.add_column('memory_usage',column)
    expected = {'memory_vectors':{'memory_id':'memory_items','space_id':'embedding_spaces'},
                'chunk_vectors':{'chunk_id':'retrieval_chunks','space_id':'embedding_spaces'},
                'memory_usage':{'account_id':'accounts','companion_id':'companions'}}
    for table, relationships in expected.items():
        for constraint in sa.inspect(bind).get_foreign_keys(table):
            names = constraint['constrained_columns']
            if len(names) != 1 or names[0] not in relationships:
                continue
            target = relationships[names[0]]
            if constraint['referred_table'] != target or constraint['referred_columns'] != ['id']:
                raise RuntimeError('Unexpected memory projection foreign-key identity; refusing repair')
            if str(constraint.get('options',{}).get('ondelete','')).upper() != 'CASCADE':
                op.drop_constraint(constraint['name'],table,type_='foreignkey')
                op.create_foreign_key(constraint['name'],table,target,names,['id'],ondelete='CASCADE')


def downgrade():
    # Keep deletion-safe projection foreign keys; rolling back accounting only removes
    # the new rate snapshots. This never removes authoritative events or memories.
    existing = {item['name'] for item in sa.inspect(op.get_bind()).get_columns('memory_usage')}
    for name in ('usage_estimated','output_rate','input_rate'):
        if name in existing:
            op.drop_column('memory_usage',name)
