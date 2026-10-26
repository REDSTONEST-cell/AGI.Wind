from sqlalchemy import select, func
from . import models as d
from ..archive.repository import lock
from ..core.events import StreamEvent

def finish_open_tools(conn,generation_id,epoch,code,*,cancelled=False):
    """Close real in-progress trace steps in the same transaction as failure/cancellation."""
    from ..archive import models as m
    from ..archive.repository import digest
    row=conn.execute(select(m.events).where(m.events.c.generation_id==generation_id,m.events.c.role=='assistant')).mappings().first()
    if not row:return
    content=dict(row['content'])
    content['state']='cancelled' if cancelled else ('partial' if content.get('text') else 'failed')
    for step in content.get('trace',[]):
        if step.get('type')!='tool' or step.get('status')!='running':continue
        step.update(status='failed',code=code,summary='操作已停止' if cancelled else '操作失败')
        emit(conn,generation_id,'tool.completed',{'step_id':step['id'],'tool':step['tool'],'stage':step['stage'],'status':'failed','code':code,'summary':step['summary']},epoch)
    conn.execute(m.events.update().where(m.events.c.id==row['id']).values(content=content,content_sha256=digest(content)))

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
