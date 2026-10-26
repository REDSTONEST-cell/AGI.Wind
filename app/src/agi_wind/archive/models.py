"""Authoritative PostgreSQL schema. Vectors/runtime history are disposable projections."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import MetaData, Table, Column, Text, Integer, BigInteger, Boolean, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, Index, text
from sqlalchemy.dialects.postgresql import UUID, JSONB

metadata = MetaData()
def now(): return datetime.now(timezone.utc)
def idcol(name="id", **kw): return Column(name, UUID(as_uuid=True), primary_key=True, default=uuid4, **kw)
def fk(name, table, **kw): return Column(name, UUID(as_uuid=True), ForeignKey(table + ".id"), nullable=False, **kw)
def js(name, **kw): return Column(name, JSONB, nullable=False, default=dict, **kw)
def ts(name="created_at"): return Column(name, DateTime(timezone=True), nullable=False, default=now)

accounts = Table("accounts", metadata, idcol(), ts())
users = Table("users", metadata, idcol(), fk("account_id", "accounts"))
companions = Table("companions", metadata, idcol(), fk("account_id", "accounts"), Column("name", Text, nullable=False), Column("active_persona_version", Integer, nullable=False, default=1), Column("memory_revision", Integer, nullable=False, default=1), Column("status", Text, nullable=False, default="development"), Column("memory_extract_cursor_at", DateTime(timezone=True)), Column("memory_extract_cursor_id", UUID(as_uuid=True)), ts())
conversations = Table("conversations", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), fk("user_id", "users"), Column("title", Text, default=""), Column("state", Text, nullable=False, default="active", server_default="active"), Column("channel_kind", Text, nullable=False, default="web", server_default="web"), Column("external_chat_id", Text, nullable=False, default="", server_default=""), CheckConstraint("state IN ('active','archived','hidden')", name="conversations_state_check"), CheckConstraint("channel_kind IN ('web','private','group')", name="conversations_channel_kind_check"), ts())
# One stable external member identity per account/platform. Nickname and group card are
# display-only; group owner/admin `sender.role` is never an AGI.Wind permission.
external_principals = Table("external_principals", metadata, idcol(), fk("account_id", "accounts"),
    Column("platform", Text, nullable=False), Column("external_user_id", Text, nullable=False),
    Column("display_name", Text, nullable=False, default=""), Column("card_name", Text, nullable=False, default=""),
    Column("is_owner", Boolean, nullable=False, default=False, server_default=text("false")),
    ts(), ts("last_seen_at"), UniqueConstraint("account_id", "platform", "external_user_id", name="external_principals_identity_uniq"))
# A persistent window is a conversation plus its stable room identity. The unique key makes
# concurrent first messages for one room converge on the same window instead of two.
channel_bindings = Table("channel_bindings", metadata, idcol(), fk("account_id", "accounts"),
    fk("companion_id", "companions"), Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
    Column("platform", Text, nullable=False), Column("bot_external_id", Text, nullable=False),
    Column("channel_kind", Text, nullable=False, default="group", server_default="group"),
    Column("external_chat_id", Text, nullable=False), Column("title", Text, nullable=False, default=""),
    Column("enabled", Boolean, nullable=False, default=True, server_default=text("true")),
    Column("member_extract_cursor_at", DateTime(timezone=True)), Column("member_extract_cursor_id", UUID(as_uuid=True)),
    ts(), ts("updated_at"),
    UniqueConstraint("account_id", "companion_id", "platform", "bot_external_id", "channel_kind", "external_chat_id",
                     name="channel_bindings_identity_uniq"),
    CheckConstraint("channel_kind IN ('web','private','group')", name="channel_bindings_kind_check"))
requests = Table("requests", metadata, idcol(), fk("account_id", "accounts"), fk("conversation_id", "conversations"), Column("input_hash", Text, nullable=False), js("input"), js("output"), Column("device_id", Text, nullable=False), Column("state", Text, nullable=False, default="accepted"), Column("generation_id", UUID(as_uuid=True), nullable=False), ts())
generations = Table("generations", metadata, idcol(), fk("request_id", "requests", unique=True), Column("epoch", Integer, nullable=False, default=1), Column("runtime", Text, nullable=False, default="unconfigured"), js("model_ref"), Column("persona_version", Integer, nullable=False, default=1), Column("memory_revision", Integer, nullable=False, default=1), Column("state", Text, nullable=False, default="pending"), Column("delivery_state", Text, nullable=False, default="not_started"), Column("error_code", Text), ts(), Column("finished_at", DateTime(timezone=True)))
events = Table("events", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), fk("conversation_id", "conversations"), Column("request_id", UUID(as_uuid=True), ForeignKey("requests.id")), Column("generation_id", UUID(as_uuid=True), ForeignKey("generations.id")), Column("role", Text, nullable=False), Column("kind", Text, nullable=False), js("content"), Column("content_sha256", Text, nullable=False), Column("sender_principal_id", UUID(as_uuid=True), ForeignKey("external_principals.id", ondelete="SET NULL")), Column("external_message_id", Text), ts("occurred_at"), ts("recorded_at"), Column("visibility", Text, nullable=False, default="visible"))
Index("events_external_message_uniq", events.c.conversation_id, events.c.external_message_id, unique=True, postgresql_where=text("external_message_id IS NOT NULL"))
Index("events_sender_principal_idx", events.c.sender_principal_id)
# OneBot send receipts prove a quoted message really belongs to this bot/room; the model
# never supplies the target room or a "this is me" claim.
qq_message_receipts = Table("qq_message_receipts", metadata, idcol(), fk("account_id", "accounts"),
    Column("binding_id", UUID(as_uuid=True), ForeignKey("channel_bindings.id", ondelete="CASCADE"), nullable=False),
    Column("platform_message_id", Text, nullable=False), Column("generation_id", UUID(as_uuid=True), ForeignKey("generations.id", ondelete="SET NULL")),
    Column("segment_index", Integer, nullable=False, default=0), ts(),
    UniqueConstraint("binding_id", "platform_message_id", name="qq_message_receipts_identity_uniq"))
assets = Table("assets", metadata, idcol(), Column("sha256", Text, nullable=False, unique=True), Column("relative_path", Text, nullable=False), Column("mime", Text, nullable=False), Column("bytes", BigInteger, nullable=False), Column("kind", Text, nullable=False), js("metadata"), ts())
asset_owners = Table("asset_owners", metadata, fk("asset_id", "assets", primary_key=True), fk("account_id", "accounts", primary_key=True))
asset_refs = Table("asset_refs", metadata, fk("asset_id", "assets", primary_key=True), fk("event_id", "events", primary_key=True), Column("purpose", Text, nullable=False, default="input"))
outbox = Table("outbox", metadata, idcol(), Column("event_type", Text, nullable=False), Column("aggregate_id", UUID(as_uuid=True), nullable=False), js("payload"), Column("dedupe_key", Text, nullable=False, unique=True), Column("state", Text, nullable=False, default="pending"), Column("attempts", Integer, nullable=False, default=0), Column("worker_id", Text), Column("lease_until", DateTime(timezone=True)), ts("available_at"), ts())
jobs = Table("jobs", metadata, idcol(), Column("kind", Text, nullable=False), js("scope"), Column("state", Text, nullable=False, default="pending"), js("checkpoint"), Column("attempts", Integer, nullable=False, default=0), Column("lease_until", DateTime(timezone=True)), Column("last_error_code", Text), ts())

ARCHIVE_TABLES = list(metadata.tables)

# Created by 0008, deliberately excluded from historical 0001's live table list.
stickers = Table("stickers", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), fk("asset_id", "assets"), Column("name", Text, nullable=False), Column("description", Text, nullable=False, default=""), Column("tags", JSONB, nullable=False, default=list), Column("state", Text, nullable=False, default="active"), Column("source", Text, nullable=False, default="uploaded"), Column("source_event_id", UUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")), Column("use_count", Integer, nullable=False, default=0), Column("last_used_at", DateTime(timezone=True)), ts(), UniqueConstraint("companion_id", "asset_id"), CheckConstraint("state IN ('active','hidden')"), CheckConstraint("source IN ('captured','uploaded')"))
