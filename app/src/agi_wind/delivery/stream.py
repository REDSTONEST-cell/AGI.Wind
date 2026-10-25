from sqlalchemy import select, func
from . import models as d
from ..archive.repository import lock
from ..core.events import StreamEvent

def emit(conn, generation_id, type, payload, epoch):
    lock(conn, generation_id)
    sequence = (conn.scalar(select(func.max(d.generation_stream_events.c.sequence)).where(d.generation_stream_events.c.generation_id == generation_id)) or 0) + 1
    return conn.execute(d.generation_stream_events.insert().values(generation_id=generation_id, sequence=sequence, type=type, payload=payload, epoch=epoch).returning(d.generation_stream_events)).mappings().one()

def emit_live(conn, conversation_id, type, payload):
    lock(conn, "live:" + str(conversation_id))
    sequence = (conn.scalar(select(func.max(d.conversation_live_events.c.sequence)).where(d.conversation_live_events.c.conversation_id == conversation_id)) or 0) + 1
    conn.execute(d.conversation_live_events.insert().values(conversation_id=conversation_id, sequence=sequence, type=type, payload=payload))

def to_event(row):
    return StreamEvent(event_id=row["id"], sequence=row["sequence"], type=row["type"], generation_id=row["generation_id"], epoch=row["epoch"], timestamp=row["created_at"], payload=row["payload"])

def encode_sse(event):
    return f"id: {event.sequence}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
