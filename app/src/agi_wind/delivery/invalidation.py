from sqlalchemy import select
from ..archive import models as m
from ..archive.repository import lock,digest
from .stream import emit,emit_live,finish_open_tools

def invalidate_active(conn,companion_id,reason="memory.invalidated"):
    query=select(m.generations,m.requests.c.conversation_id).select_from(m.generations.join(m.requests,m.generations.c.request_id==m.requests.c.id).join(m.conversations,m.requests.c.conversation_id==m.conversations.c.id)).where(m.conversations.c.companion_id==companion_id,m.generations.c.state.in_(["running","draining"]))
    for row in list(conn.execute(query).mappings()):
        lock(conn,row["id"])
        epoch=row["epoch"]+1
        conn.execute(m.generations.update().where(m.generations.c.id==row["id"]).values(state="cancelled",delivery_state="cancelled",epoch=epoch,error_code="STALE_CONTEXT"))
        finish_open_tools(conn,row['id'],epoch,'STALE_CONTEXT',cancelled=True)
        for event in list(conn.execute(select(m.events).where(m.events.c.generation_id==row["id"],m.events.c.role=="assistant")).mappings()):
            content={**event["content"],"state":"cancelled"}
            conn.execute(m.events.update().where(m.events.c.id==event["id"]).values(content=content,content_sha256=digest(content)))
        emit(conn,row["id"],"generation.cancelled",{"code":"STALE_CONTEXT"},epoch)
        emit_live(conn,row["conversation_id"],"delivery.epoch_changed",{"generation_id":str(row["id"]),"epoch":epoch,"reason":reason})
