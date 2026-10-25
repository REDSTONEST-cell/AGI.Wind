from uuid import uuid4
from sqlalchemy import select
from . import models as mm
from ..archive import models as m
from ..archive.repository import digest, lock
from ..identity import models as identity
from ..identity.service import authorize
from ..core.errors import AppError

class CorrectionService:
    def __init__(self, db): self.db = db
    async def apply_correction(self, actor, memory_id, correction_text, source_event_id):
        if not correction_text.strip(): raise AppError("EMPTY_CORRECTION", "修正不能为空", 422)
        with self.db.engine.begin() as conn:
            memory = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id == memory_id)).mappings().first()
            if not memory: raise AppError("NOT_FOUND", "记忆不存在", 404)
            lock(conn, memory["companion_id"])
            companion = authorize(conn, actor, memory["companion_id"])
            if memory["account_id"] != actor.account_id: raise AppError("FORBIDDEN", "无权修正记忆", 403)
            source = conn.execute(select(m.events).where(m.events.c.id == source_event_id, m.events.c.account_id == actor.account_id, m.events.c.companion_id == memory["companion_id"], m.events.c.visibility == "visible")).mappings().first()
            if not source: raise AppError("INVALID_SOURCE", "来源不存在或不可访问", 403)
            content = {"text": correction_text, "corrects_memory_id": str(memory_id)}
            event_id, replacement_id, revision = uuid4(), uuid4(), companion["memory_revision"] + 1
            conn.execute(m.events.insert().values(id=event_id, account_id=actor.account_id, companion_id=memory["companion_id"], conversation_id=source["conversation_id"], role="user", kind="correction", content=content, content_sha256=digest(content)))
            conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == memory_id).values(status="superseded"))
            conn.execute(mm.memory_items.insert().values(id=replacement_id, account_id=actor.account_id, companion_id=memory["companion_id"], user_id=actor.user_id, kind=memory["kind"], content=correction_text, epistemic_type="user_asserted", revision=revision))
            conn.execute(mm.source_refs.insert().values(memory_id=replacement_id, event_id=event_id, json_pointer="/text"))
            conn.execute(mm.memory_relations.insert().values(from_memory_id=replacement_id, to_memory_id=memory_id, relation_type="supersedes"))
            conn.execute(m.companions.update().where(m.companions.c.id == memory["companion_id"]).values(memory_revision=revision))
            conn.execute(identity.runtime_bindings.update().where(identity.runtime_bindings.c.companion_id == memory["companion_id"]).values(status="stale"))
            from ..delivery.invalidation import invalidate_active
            invalidate_active(conn,memory["companion_id"])
            return {"memory_id": replacement_id, "event_id": event_id, "memory_revision": revision}
