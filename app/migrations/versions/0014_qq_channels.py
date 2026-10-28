"""QQ group rooms, external member principals and dual-pool memory ownership."""
from alembic import op
import sqlalchemy as sa

from nagi.archive import models as m

revision = '0014_qq_channels'
down_revision = '0013_memory_projection_compat'


NEW_TABLES = (m.external_principals, m.channel_bindings, m.qq_message_receipts)


def _columns(bind, table):
    return {column['name'] for column in sa.inspect(bind).get_columns(table)}


def _add(bind, table, name, column, foreign_key=None):
    if name in _columns(bind, table):
        return False
    op.add_column(table, column)
    if foreign_key:
        target, target_column, ondelete = foreign_key
        op.create_foreign_key(f'{table}_{name}_fkey', table, target, [name], [target_column], ondelete=ondelete)
    return True


def upgrade():
    bind = op.get_bind()
    for table in NEW_TABLES:
        table.create(bind, checkfirst=True)
    _add(bind, 'conversations', 'channel_kind',
         sa.Column('channel_kind', sa.Text(), nullable=False, server_default='web'))
    _add(bind, 'conversations', 'external_chat_id',
         sa.Column('external_chat_id', sa.Text(), nullable=False, server_default=''))
    _add(bind, 'events', 'sender_principal_id',
         sa.Column('sender_principal_id', sa.Uuid()), ('external_principals', 'id', 'SET NULL'))
    _add(bind, 'events', 'external_message_id', sa.Column('external_message_id', sa.Text()))
    _add(bind, 'memory_items', 'pool_kind',
         sa.Column('pool_kind', sa.Text(), nullable=False, server_default='owner'))
    _add(bind, 'memory_items', 'subject_principal_id',
         sa.Column('subject_principal_id', sa.Uuid()), ('external_principals', 'id', 'SET NULL'))
    _add(bind, 'memory_items', 'origin_room_id',
         sa.Column('origin_room_id', sa.Uuid()), ('channel_bindings', 'id', 'SET NULL'))
    _add(bind, 'memory_items', 'visibility',
         sa.Column('visibility', sa.Text(), nullable=False, server_default='owner_private'))
    _add(bind, 'memory_items', 'visibility_room_id',
         sa.Column('visibility_room_id', sa.Uuid()), ('channel_bindings', 'id', 'SET NULL'))
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS events_external_message_uniq ON events (conversation_id, external_message_id) WHERE external_message_id IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS events_sender_principal_idx ON events (sender_principal_id)")
    op.execute("CREATE INDEX IF NOT EXISTS memory_items_pool_room_idx ON memory_items (companion_id, pool_kind, visibility_room_id)")
    # Old rows are owner-private by definition; never promote them to group-visible.
    op.execute("UPDATE memory_items SET pool_kind='owner' WHERE pool_kind IS NULL OR pool_kind=''")
    op.execute("UPDATE memory_items SET visibility='owner_private' WHERE visibility IS NULL OR visibility=''")


def downgrade():
    bind = op.get_bind()
    op.execute("DROP INDEX IF EXISTS memory_items_pool_room_idx")
    op.execute("DROP INDEX IF EXISTS events_sender_principal_idx")
    op.execute("DROP INDEX IF EXISTS events_external_message_uniq")
    existing = _columns(bind, 'memory_items')
    for name in ('visibility_room_id', 'visibility', 'origin_room_id', 'subject_principal_id', 'pool_kind'):
        if name in existing:
            op.drop_column('memory_items', name)
    existing = _columns(bind, 'events')
    for name in ('external_message_id', 'sender_principal_id'):
        if name in existing:
            op.drop_column('events', name)
    existing = _columns(bind, 'conversations')
    for name in ('external_chat_id', 'channel_kind'):
        if name in existing:
            op.drop_column('conversations', name)
    for table in reversed(NEW_TABLES):
        table.drop(bind, checkfirst=True)
