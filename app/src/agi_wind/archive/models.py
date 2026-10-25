"""Authoritative PostgreSQL schema. Vectors/runtime history are disposable projections."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import MetaData, Table, Column, Text, Integer, BigInteger, Boolean, DateTime, ForeignKey, UniqueConstraint, CheckConstraint
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
conversations = Table("conversations", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), fk("user_id", "users"), Column("title", Text, default=""), Column("state", Text, nullable=False, default="active", server_default="active"), CheckConstraint("state IN ('active','archived','hidden')", name="conversations_state_check"), ts())
requests = Table("requests", metadata, idcol(), fk("account_id", "accounts"), fk("conversation_id", "conversations"), Column("input_hash", Text, nullable=False), js("input"), js("output"), Column("device_id", Text, nullable=False), Column("state", Text, nullable=False, default="accepted"), Column("generation_id", UUID(as_uuid=True), nullable=False), ts())
generations = Table("generations", metadata, idcol(), fk("request_id", "requests", unique=True), Column("epoch", Integer, nullable=False, default=1), Column("runtime", Text, nullable=False, default="unconfigured"), js("model_ref"), Column("persona_version", Integer, nullable=False, default=1), Column("memory_revision", Integer, nullable=False, default=1), Column("state", Text, nullable=False, default="pending"), Column("delivery_state", Text, nullable=False, default="not_started"), Column("error_code", Text), ts(), Column("finished_at", DateTime(timezone=True)))
events = Table("events", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), fk("conversation_id", "conversations"), Column("request_id", UUID(as_uuid=True), ForeignKey("requests.id")), Column("generation_id", UUID(as_uuid=True), ForeignKey("generations.id")), Column("role", Text, nullable=False), Column("kind", Text, nullable=False), js("content"), Column("content_sha256", Text, nullable=False), ts("occurred_at"), ts("recorded_at"), Column("visibility", Text, nullable=False, default="visible"))
assets = Table("assets", metadata, idcol(), Column("sha256", Text, nullable=False, unique=True), Column("relative_path", Text, nullable=False), Column("mime", Text, nullable=False), Column("bytes", BigInteger, nullable=False), Column("kind", Text, nullable=False), js("metadata"), ts())
asset_owners = Table("asset_owners", metadata, fk("asset_id", "assets", primary_key=True), fk("account_id", "accounts", primary_key=True))
asset_refs = Table("asset_refs", metadata, fk("asset_id", "assets", primary_key=True), fk("event_id", "events", primary_key=True), Column("purpose", Text, nullable=False, default="input"))
outbox = Table("outbox", metadata, idcol(), Column("event_type", Text, nullable=False), Column("aggregate_id", UUID(as_uuid=True), nullable=False), js("payload"), Column("dedupe_key", Text, nullable=False, unique=True), Column("state", Text, nullable=False, default="pending"), Column("attempts", Integer, nullable=False, default=0), Column("worker_id", Text), Column("lease_until", DateTime(timezone=True)), ts("available_at"), ts())
jobs = Table("jobs", metadata, idcol(), Column("kind", Text, nullable=False), js("scope"), Column("state", Text, nullable=False, default="pending"), js("checkpoint"), Column("attempts", Integer, nullable=False, default=0), Column("lease_until", DateTime(timezone=True)), Column("last_error_code", Text), ts())

ARCHIVE_TABLES = list(metadata.tables)

# Created by 0008, deliberately excluded from historical 0001's live table list.
stickers = Table("stickers", metadata, idcol(), fk("account_id", "accounts"), fk("companion_id", "companions"), fk("asset_id", "assets"), Column("name", Text, nullable=False), Column("description", Text, nullable=False, default=""), Column("tags", JSONB, nullable=False, default=list), Column("state", Text, nullable=False, default="active"), Column("source", Text, nullable=False, default="uploaded"), Column("source_event_id", UUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")), Column("use_count", Integer, nullable=False, default=0), Column("last_used_at", DateTime(timezone=True)), ts(), UniqueConstraint("companion_id", "asset_id"), CheckConstraint("state IN ('active','hidden')"), CheckConstraint("source IN ('captured','uploaded')"))
