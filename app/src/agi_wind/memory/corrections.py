from uuid import uuid4
from sqlalchemy import select, or_
from . import models as mm
from .lifecycle import lifecycle_values, now_utc
from ..archive import models as m
from ..archive.repository import digest, lock
from ..identity import models as identity
from ..identity.service import authorize
from ..core.errors import AppError

class CorrectionService:
    def __init__(self, db, retention_days=None):
        self.db, self.retention_days = db, retention_days
    async def apply_correction(self, actor, memory_id, correction_text, source_event_id):
        if not correction_text.strip(): raise AppError("EMPTY_CORRECTION", "修正不能为空", 422)
        with self.db.engine.begin() as conn:
            memory = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id == memory_id)).mappings().first()
            if not memory: raise AppError("NOT_FOUND", "记忆不存在", 404)
            lock(conn, memory["companion_id"])
            companion = authorize(conn, actor, memory["companion_id"])
            memory = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id == memory_id)).mappings().first()
            if not memory: raise AppError('NOT_FOUND','记忆不存在',404)
            if memory["account_id"] != actor.account_id: raise AppError("FORBIDDEN", "无权修正记忆", 403)
            if memory['user_id'] is not None and memory['user_id'] != actor.user_id:
                raise AppError('FORBIDDEN','无权修正其他用户的记忆',403)
            if memory['status'] == 'superseded':
                raise AppError('MEMORY_STATE','这条记忆已有新版本，请先查看更新记录',409)
            cohort = [memory]
            if memory['status'] != 'hidden' and memory['subject_key'] and memory['predicate']:
                edges = conn.execute(select(mm.memory_relations).where(mm.memory_relations.c.relation_type == 'contradicts',
                    or_(mm.memory_relations.c.from_memory_id == memory_id, mm.memory_relations.c.to_memory_id == memory_id))).mappings()
                linked = {key for edge in edges for key in (edge['from_memory_id'],edge['to_memory_id'])}
                related = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id.in_(linked),
                    mm.memory_items.c.id != memory_id,mm.memory_items.c.account_id == actor.account_id,
                    mm.memory_items.c.companion_id == memory['companion_id'],
                    or_(mm.memory_items.c.user_id == actor.user_id,mm.memory_items.c.user_id.is_(None)),
                    mm.memory_items.c.subject_key == memory['subject_key'],mm.memory_items.c.predicate == memory['predicate'],
                    mm.memory_items.c.status.in_(['active','disputed']))).mappings().all()
                cohort.extend(related)
            pinned = any(item['is_pinned'] for item in cohort)
            source = conn.execute(select(m.events).join(m.conversations,m.events.c.conversation_id==m.conversations.c.id).where(m.events.c.id == source_event_id, m.events.c.account_id == actor.account_id, m.events.c.companion_id == memory["companion_id"], m.events.c.visibility == "visible",m.conversations.c.user_id==actor.user_id,m.conversations.c.state!='hidden')).mappings().first()
            if not source: raise AppError("INVALID_SOURCE", "来源不存在或不可访问", 403)
            content = {"text": correction_text, "corrects_memory_id": str(memory_id)}
            event_id, replacement_id, revision = uuid4(), uuid4(), companion["memory_revision"] + 1
            corrected_at = now_utc()
            conn.execute(m.events.insert().values(id=event_id, account_id=actor.account_id, companion_id=memory["companion_id"], conversation_id=source["conversation_id"], role="user", kind="correction", content=content, content_sha256=digest(content), recorded_at=corrected_at))
            conn.execute(mm.memory_items.update().where(mm.memory_items.c.id.in_([item['id'] for item in cohort])).values(status="superseded", archived_at=corrected_at, valid_to=corrected_at))
            life = lifecycle_values(memory["kind"], 1.0, corrected_at, self.retention_days)
            if pinned:
                life["expires_at"] = None
            conn.execute(mm.memory_items.insert().values(id=replacement_id, account_id=actor.account_id, companion_id=memory["companion_id"], user_id=actor.user_id, kind=memory["kind"], content=correction_text, epistemic_type="user_asserted", confidence=1.0, revision=revision,
                subject_key=memory["subject_key"], predicate=memory["predicate"], value="",
                valid_from=corrected_at, is_pinned=pinned,
                status="hidden" if memory["status"] == "hidden" else "active", **life))
            conn.execute(mm.source_refs.insert().values(memory_id=replacement_id, event_id=event_id, json_pointer="/text"))
            for item in cohort:
                conn.execute(mm.memory_relations.insert().values(from_memory_id=replacement_id, to_memory_id=item['id'], relation_type="supersedes"))
            conn.execute(m.companions.update().where(m.companions.c.id == memory["companion_id"]).values(memory_revision=revision))
            conn.execute(identity.runtime_bindings.update().where(identity.runtime_bindings.c.companion_id == memory["companion_id"]).values(status="stale"))
            from ..delivery.invalidation import invalidate_active
            invalidate_active(conn,memory["companion_id"])
            return {"memory_id": replacement_id, "event_id": event_id, "memory_revision": revision}
