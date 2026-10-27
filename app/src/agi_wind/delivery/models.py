from sqlalchemy import Table, Column, Text, Integer, ForeignKey, UniqueConstraint, DateTime, Index, CheckConstraint, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from ..archive.models import metadata, idcol, fk, js, ts, now

generation_stream_events = Table("generation_stream_events", metadata, idcol(), fk("generation_id", "generations"), Column("sequence", Integer, nullable=False), Column("type", Text, nullable=False), js("payload"), Column("epoch", Integer, nullable=False), ts(), UniqueConstraint("generation_id", "sequence"))
conversation_live_events = Table("conversation_live_events", metadata, idcol(), fk("conversation_id", "conversations"), Column("sequence", Integer, nullable=False), Column("type", Text, nullable=False), js("payload"), ts(), UniqueConstraint("conversation_id", "sequence"))
delivery_commands = Table("delivery_commands", metadata, idcol(), fk("generation_id", "generations"), Column("operation", Text, nullable=False), js("result"), ts())
speech_segments = Table("speech_segments", metadata, idcol(), fk("generation_id", "generations"), Column("epoch", Integer, nullable=False), Column("segment_index", Integer, nullable=False), Column("source_event_id", UUID(as_uuid=True), ForeignKey("events.id")), Column("display_text", Text, nullable=False), Column("spoken_text", Text, nullable=False), Column("voice_id", Text, nullable=False), Column("voice_version", Text, nullable=False), Column("asset_id", UUID(as_uuid=True), ForeignKey("assets.id")), Column("sample_rate", Integer), Column("channels", Integer), Column("duration_ms", Integer), Column("state", Text, nullable=False, default="queued"), Column("error_code", Text), UniqueConstraint("generation_id", "epoch", "segment_index"))
avatar_cues = Table("avatar_cues", metadata, idcol(), fk("generation_id", "generations"), Column("epoch", Integer, nullable=False), Column("segment_id", UUID(as_uuid=True), ForeignKey("speech_segments.id")), js("cue"))
playback_receipts = Table("playback_receipts", metadata, fk("segment_id", "speech_segments", primary_key=True), Column("device_id", Text, primary_key=True), fk("generation_id", "generations"), Column("state", Text, nullable=False), Column("played_ms", Integer, nullable=False), ts("received_at"))
proactive_messages = Table(
    "proactive_messages",
    metadata,
    idcol(),
    Column("account_id", UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
    Column("companion_id", UUID(as_uuid=True), ForeignKey("companions.id", ondelete="CASCADE"), nullable=False),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
    Column("event_id", UUID(as_uuid=True), ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("trigger", Text, nullable=False, default="schedule"),
    ts(),
)
DELIVERY_TABLES = ["generation_stream_events", "conversation_live_events", "delivery_commands", "speech_segments", "avatar_cues", "playback_receipts"]

# Operational "after reply" cognition runs. Derived runtime state, deliberately excluded
# from archive exports; conversation deletion cascades the rows.
idle_cognition_runs = Table(
    "idle_cognition_runs",
    metadata,
    idcol(),
    Column("account_id", UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
    Column("companion_id", UUID(as_uuid=True), ForeignKey("companions.id", ondelete="CASCADE"), nullable=False),
    Column("conversation_id", UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
    Column("trigger_generation_id", UUID(as_uuid=True), ForeignKey("generations.id", ondelete="SET NULL")),
    Column("trigger_event_id", UUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")),
    Column("message_event_id", UUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL")),
    Column("due_at", DateTime(timezone=True), nullable=False),
    Column("state", Text, nullable=False, default="pending", server_default="pending"),
    Column("decision", Text, nullable=False, default="", server_default=""),
    Column("reason", Text, nullable=False, default="", server_default=""),
    Column("error_code", Text),
    Column("rounds_used", Integer, nullable=False, default=0, server_default="0"),
    Column("steps_used", Integer, nullable=False, default=0, server_default="0"),
    Column("steps", JSONB, nullable=False, default=list),
    Column("checkpoint", JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")),
    Column("attempts", Integer, nullable=False, default=0, server_default="0"),
    Column("lease_until", DateTime(timezone=True)),
    Column("scheduled_at", DateTime(timezone=True), nullable=False, default=now),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    ts(),
    ts("updated_at"),
    CheckConstraint("state IN ('pending','running','sent','silent','skipped','cancelled','failed')", name="idle_cognition_runs_state_check"),
)
Index("idle_cognition_runs_active_uniq", idle_cognition_runs.c.conversation_id, unique=True,
      postgresql_where=text("state IN ('pending','running')"))
Index("idle_cognition_runs_due_idx", idle_cognition_runs.c.state, idle_cognition_runs.c.due_at)
