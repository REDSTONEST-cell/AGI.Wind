from sqlalchemy import Table, Column, Text, Integer, Float, Boolean, DateTime, Date, LargeBinary, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from ..archive.models import metadata, idcol, fk, js, ts

memory_items = Table("memory_items", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), Column("user_id", UUID(as_uuid=True), ForeignKey("users.id")), Column("kind", Text, nullable=False), Column("content", Text, nullable=False), Column("epistemic_type", Text, nullable=False), Column("confidence", Float, nullable=False, default=1), Column("status", Text, nullable=False, default="active"), Column("revision", Integer, nullable=False, default=1), Column("importance", Float, nullable=False, default=0.5, server_default="0.5"), Column("access_count", Integer, nullable=False, default=0, server_default="0"), Column("last_accessed_at", DateTime(timezone=True)), Column("last_reinforced_at", DateTime(timezone=True)), Column("ttl_days", Float, nullable=False, default=30, server_default="30"), Column("expires_at", DateTime(timezone=True)), Column("decay_type", Text, nullable=False, default="exponential", server_default="exponential"), Column("archived_at", DateTime(timezone=True)), ts(), ts("updated_at"))
source_refs = Table("source_refs", metadata, idcol(), fk("memory_id", "memory_items"), fk("event_id", "events"), Column("json_pointer", Text, nullable=False), Column("start_offset", Integer), Column("end_offset", Integer), Column("quote_hash", Text))
memory_relations = Table("memory_relations", metadata, idcol(), fk("from_memory_id", "memory_items"), fk("to_memory_id", "memory_items"), Column("relation_type", Text, nullable=False))
retrieval_chunks = Table("retrieval_chunks", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), fk("source_id", "events"), Column("content", Text, nullable=False), Column("content_hash", Text, nullable=False), Column("source_hash",Text,nullable=False), Column("start_offset", Integer, nullable=False), Column("end_offset", Integer, nullable=False), Column("token_count", Integer, nullable=False), Column("search_terms", Text, nullable=False), Column("revision", Integer, nullable=False, default=1), UniqueConstraint("source_id", "start_offset", "content_hash"))
embedding_spaces = Table("embedding_spaces", metadata, idcol(), Column("fingerprint", Text, nullable=False, unique=True), js("config"), Column("status", Text, nullable=False, default="building"), js("checkpoint"))
quarantine = Table("quarantine", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), js("candidate"), Column("reason", Text, nullable=False), ts())
MEMORY_TABLES = ["memory_items", "source_refs", "memory_relations", "retrieval_chunks", "embedding_spaces", "quarantine"]

# Authoritative fields travel with the existing memory_items archive rows. Empty defaults
# keep pre-0012 archives importable without inventing semantic identities for old facts.
for column in (
    Column('subject_key', Text, nullable=False, server_default='', default=''),
    Column('predicate', Text, nullable=False, server_default='', default=''),
    Column('value', Text, nullable=False, server_default='', default=''),
    Column('valid_from', DateTime(timezone=True)),
    Column('valid_to', DateTime(timezone=True)),
    Column('is_pinned', Boolean, nullable=False, server_default='false', default=False),
    Column('summary_level', Text, nullable=False, server_default='', default=''),
    # Logical pool ownership is enforced by the server, not by which chat window asks.
    # Old memories predate multi-user scenes, so they stay owner-private until re-derived.
    Column('pool_kind', Text, nullable=False, server_default='owner', default='owner'),
    Column('subject_principal_id', UUID(as_uuid=True), ForeignKey('external_principals.id', ondelete='SET NULL')),
    Column('origin_room_id', UUID(as_uuid=True), ForeignKey('channel_bindings.id', ondelete='SET NULL')),
    Column('visibility', Text, nullable=False, server_default='owner_private', default='owner_private'),
    Column('visibility_room_id', UUID(as_uuid=True), ForeignKey('channel_bindings.id', ondelete='SET NULL')),
):
    memory_items.append_column(column)

# Rebuildable and operational state is deliberately excluded from ARCHIVE_TABLE_ORDER.
def _derived_fk(name, table):
    return Column(name, UUID(as_uuid=True), ForeignKey(table + '.id', ondelete='CASCADE'), nullable=False)

memory_vectors = Table('memory_vectors', metadata, idcol(), _derived_fk('memory_id', 'memory_items'),
    _derived_fk('space_id', 'embedding_spaces'), Column('content_hash', Text, nullable=False),
    Column('vector', LargeBinary, nullable=False), Column('revision', Integer, nullable=False),
    ts(), UniqueConstraint('memory_id', 'space_id'))
chunk_vectors = Table('chunk_vectors', metadata, idcol(), _derived_fk('chunk_id', 'retrieval_chunks'),
    _derived_fk('space_id', 'embedding_spaces'), Column('content_hash', Text, nullable=False),
    Column('vector', LargeBinary, nullable=False), Column('revision', Integer, nullable=False),
    ts(), UniqueConstraint('chunk_id', 'space_id'))
memory_usage = Table('memory_usage', metadata, idcol(), _derived_fk('account_id', 'accounts'),
    _derived_fk('companion_id', 'companions'), Column('job_id', UUID(as_uuid=True), nullable=False),
    Column('attempt', Integer, nullable=False), Column('billing_day', Date, nullable=False),
    Column('reserved_usd', Float, nullable=False, server_default='0'),
    Column('used_usd', Float, nullable=False, server_default='0'),
    Column('prompt_tokens', Integer, nullable=False, server_default='0'),
    Column('completion_tokens', Integer, nullable=False, server_default='0'),
    Column('input_rate', Float, nullable=False, server_default='0'),
    Column('output_rate', Float, nullable=False, server_default='0'),
    Column('usage_estimated', Boolean, nullable=False, server_default='true'),
    Column('status', Text, nullable=False, server_default='reserved'), ts(),
    UniqueConstraint('job_id', 'attempt'))
MEMORY_DERIVED_TABLES = [memory_vectors, chunk_vectors, memory_usage]
