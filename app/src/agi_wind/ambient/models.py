from sqlalchemy import Table, Column, Text, Integer, Boolean, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import UUID

from ..archive.models import metadata, idcol, ts


# Derived operational observations. Deliberately excluded from archive exports: the
# authoritative archive contains conversations/memories, not desktop surveillance.
ambient_observations = Table(
    "ambient_observations",
    metadata,
    idcol(),
    Column("account_id", UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
    ts("captured_at"),
    Column("window_title", Text, nullable=False, default="", server_default=""),
    Column("process_name", Text, nullable=False, default="", server_default=""),
    Column("process_id", Integer),
    Column("idle_seconds", Integer, nullable=False, default=0, server_default="0"),
    Column("sensitive", Boolean, nullable=False, default=False, server_default=text("false")),
    Column("screenshot_path", Text),
    Column("screenshot_width", Integer),
    Column("screenshot_height", Integer),
    ts(),
)

Index(
    "ambient_observations_owner_time_idx",
    ambient_observations.c.account_id,
    ambient_observations.c.captured_at.desc(),
)
