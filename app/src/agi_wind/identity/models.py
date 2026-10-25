from sqlalchemy import Table, Column, Text, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from ..archive.models import metadata, idcol, fk, js, ts

persona_versions = Table("persona_versions", metadata, idcol(), fk("companion_id", "companions"), Column("version", Integer, nullable=False), js("core"), js("style"), Column("human_notes", Text, nullable=False, default=""), Column("origin", Text, nullable=False, default="user"), js("source_refs"), ts(), UniqueConstraint("companion_id", "version"))
relationships = Table("relationships", metadata, idcol(), fk("companion_id", "companions"), fk("user_id", "users"), js("data"), Column("version", Integer, nullable=False, default=1), UniqueConstraint("companion_id", "user_id"))
commitments = Table("commitments", metadata, idcol(), fk("companion_id", "companions"), fk("user_id", "users"), Column("content", Text, nullable=False), Column("status", Text, nullable=False, default="pending"), Column("source_event_id", UUID(as_uuid=True), ForeignKey("events.id")), ts())
runtime_bindings = Table("runtime_bindings", metadata, idcol(), fk("companion_id", "companions"), Column("runtime", Text, nullable=False), Column("external_agent_id", Text), Column("projected_persona_version", Integer, nullable=False, default=0), Column("projected_memory_revision", Integer, nullable=False, default=0), js("model_ref"), Column("status", Text, nullable=False, default="stale"), UniqueConstraint("companion_id", "runtime"))
IDENTITY_TABLES = ["persona_versions", "relationships", "commitments", "runtime_bindings"]
