from uuid import UUID
from datetime import timedelta
from sqlalchemy import select, or_, and_, func, literal_column, exists, update
from sqlalchemy.dialects.postgresql import insert
from . import models as mm
from .keyword import terms
from .chunking import chunk_text, estimate_tokens
from .lifecycle import extend_after_access, freshness, now_utc, scored_hit_metadata, ttl_for
from ..archive import models as m
from ..archive.repository import digest, lock
from ..identity.service import authorize
from ..core.types import Actor, MemoryHit, SourceRef
from ..core.errors import AppError


class MemoryService:
    def __init__(self, db, token_count=estimate_tokens): self.db, self.token_count = db, token_count

    async def index_events(self, scope):
        with self.db.engine.begin() as conn:
            lock(conn, scope.companion_id)
            companion = authorize(conn, scope, scope.companion_id)
            already=exists(select(mm.retrieval_chunks.c.id).where(mm.retrieval_chunks.c.source_id==m.events.c.id,mm.retrieval_chunks.c.source_hash==m.events.c.content_sha256))
            rows = list(conn.execute(select(m.events).where(m.events.c.account_id == scope.account_id, m.events.c.companion_id == scope.companion_id, m.events.c.visibility == "visible",~already)).mappings())
            for row in rows:
                conn.execute(mm.retrieval_chunks.delete().where(mm.retrieval_chunks.c.source_id==row["id"]))
                text = row["content"].get("text", "")
                for piece in chunk_text(text, token_count=self.token_count):
                    conn.execute(insert(mm.retrieval_chunks).values(account_id=scope.account_id, companion_id=scope.companion_id, source_id=row["id"], content=piece.text, content_hash=digest(piece.text),source_hash=row["content_sha256"], start_offset=piece.start, end_offset=piece.end, token_count=self.token_count(piece.text), search_terms=" ".join(terms(piece.text)), revision=companion["memory_revision"]).on_conflict_do_nothing())

    async def search(self, scope, query, time_range=None):
        query_terms = terms(query)[:30]
        if not query_terms: return []
        reference = now_utc()
        with self.db.engine.begin() as conn:
            authorize(conn, scope, scope.companion_id)
            c, e = mm.retrieval_chunks, m.events
            base = select(c, e.c.role, e.c.kind, e.c.occurred_at).join(e, c.c.source_id == e.c.id).where(c.c.account_id == scope.account_id, c.c.companion_id == scope.companion_id, e.c.visibility == "visible")
            if time_range:
                base = base.where(e.c.occurred_at >= time_range[0], e.c.occurred_at < time_range[1])
            exact = list(conn.execute(base.where(c.c.content.ilike("%" + query.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\")).order_by(e.c.recorded_at.desc()).limit(20)).mappings())
            vector = func.to_tsvector(literal_column("'simple'"), c.c.search_terms)
            search = func.to_tsquery(literal_column("'simple'"), " | ".join("'" + token.replace("'", "") + "'" for token in query_terms))
            keyword = list(conn.execute(base.where(vector.op("@@")(search)).order_by(func.ts_rank(vector, search).desc(), e.c.recorded_at.desc()).limit(30)).mappings())
            scores, records = {}, {}
            for ranked in (exact, keyword):
                for rank, row in enumerate(ranked):
                    records[row["id"]] = row
                    scores[row["id"]] = scores.get(row["id"], 0) + 1 / (60 + rank + 1)
            # Structured facts match either their normalized statement or the original event
            # wording. The latter lets a user ask "你以后叫我什么" and still find a fact stored
            # as "用户希望被称为小风" when the source quote says "以后叫我小风".
            source_match = exists(
                select(mm.source_refs.c.id)
                .join(e, mm.source_refs.c.event_id == e.c.id)
                .where(
                    mm.source_refs.c.memory_id == mm.memory_items.c.id,
                    e.c.account_id == scope.account_id,
                    e.c.companion_id == scope.companion_id,
                    e.c.visibility == "visible",
                    or_(*[e.c.content["text"].astext.ilike("%" + token + "%") for token in query_terms]),
                )
            )
            structured = conn.execute(
                select(mm.memory_items)
                .where(
                    mm.memory_items.c.account_id == scope.account_id,
                    mm.memory_items.c.companion_id == scope.companion_id,
                    or_(mm.memory_items.c.user_id == scope.user_id, mm.memory_items.c.user_id.is_(None)),
                    mm.memory_items.c.status == "active",
                    or_(mm.memory_items.c.expires_at.is_(None), mm.memory_items.c.expires_at > reference),
                    mm.memory_items.c.epistemic_type != "fictional",
                    or_(*[mm.memory_items.c.content.ilike("%" + token + "%") for token in query_terms], source_match),
                )
                .order_by(mm.memory_items.c.revision.desc())
                .limit(30)
            ).mappings()
            hits = []
            for memory in structured:
                refs=list(conn.execute(select(mm.source_refs).join(m.events,mm.source_refs.c.event_id==m.events.c.id).where(mm.source_refs.c.memory_id==memory["id"],m.events.c.account_id==scope.account_id,m.events.c.companion_id==scope.companion_id,m.events.c.visibility=="visible")).mappings())
                if refs:
                    lifecycle = scored_hit_metadata(memory, reference)
                    hits.append(MemoryHit(memory_id=memory["id"],content=memory["content"],score=lifecycle["score"],epistemic_type=memory["epistemic_type"],source_refs=[SourceRef(**{key:ref[key] for key in ("event_id","json_pointer","start_offset","end_offset","quote_hash")}) for ref in refs]))
            historical=set(conn.scalars(select(mm.source_refs.c.event_id).join(mm.memory_items).where(mm.memory_items.c.companion_id==scope.companion_id,mm.memory_items.c.account_id==scope.account_id,mm.memory_items.c.status!="active")))
            for key in sorted(scores, key=scores.get, reverse=True):
                row = records[key]
                hits.append(MemoryHit(chunk_id=row["id"], content=row["content"], score=scores[key], epistemic_type="assistant_generated" if row["role"] == "assistant" else "user_asserted", source_refs=[SourceRef(event_id=row["source_id"], start_offset=row["start_offset"], end_offset=row["end_offset"], quote_hash=__import__('hashlib').sha256(row["content"].encode()).hexdigest())], time_range=(row["occurred_at"], row["occurred_at"]),status="historical" if row["source_id"] in historical else "active"))
            hits.sort(key=lambda hit: hit.score, reverse=True)
            return hits

    def stats(self, actor: Actor, companion_id: UUID):
        """Dashboard facts: status/kind/importance distribution and extraction backlog."""
        with self.db.engine.begin() as conn:
            authorize(conn, actor, companion_id)
            base = (mm.memory_items.c.account_id == actor.account_id, mm.memory_items.c.companion_id == companion_id)
            status_counts = {str(status): int(count) for status, count in conn.execute(select(mm.memory_items.c.status, func.count()).where(*base).group_by(mm.memory_items.c.status))}
            kind_counts = {str(kind): int(count) for kind, count in conn.execute(select(mm.memory_items.c.kind, func.count()).where(*base).group_by(mm.memory_items.c.kind))}
            buckets = [0] * 10
            for value in conn.scalars(select(mm.memory_items.c.importance).where(*base)):
                buckets[max(0, min(9, int(float(value or 0) * 10)))] += 1
            job_counts = {str(state): int(count) for state, count in conn.execute(select(m.outbox.c.state, func.count()).where(m.outbox.c.event_type == "memory.extract", m.outbox.c.aggregate_id == companion_id).group_by(m.outbox.c.state))}
            companion = conn.execute(select(m.companions).where(m.companions.c.id == companion_id)).mappings().one()
            visible_events = int(conn.scalar(select(func.count()).select_from(m.events).where(m.events.c.account_id == actor.account_id, m.events.c.companion_id == companion_id, m.events.c.visibility == "visible")) or 0)
            pending_query = select(func.count()).select_from(m.events).where(
                m.events.c.account_id == actor.account_id,
                m.events.c.companion_id == companion_id,
                m.events.c.visibility == "visible",
                or_(
                    and_(m.events.c.role == "user", m.events.c.kind == "user_input"),
                    and_(m.events.c.role == "assistant", m.events.c.kind == "assistant_output"),
                    and_(m.events.c.role == "system", m.events.c.kind == "vision_observation"),
                ),
            )
            cursor_at, cursor_id = companion["memory_extract_cursor_at"], companion["memory_extract_cursor_id"]
            if cursor_at is not None and cursor_id is not None:
                pending_query = pending_query.where(or_(m.events.c.recorded_at > cursor_at, and_(m.events.c.recorded_at == cursor_at, m.events.c.id > cursor_id)))
            pending_events = int(conn.scalar(pending_query) or 0)
            return {
                "counts": status_counts,
                "total": sum(status_counts.values()),
                "kinds": kind_counts,
                "importance_buckets": [{"range": f"{index * 10}-{(index + 1) * 10}%", "count": count} for index, count in enumerate(buckets)],
                "jobs": job_counts,
                "jobs_total": sum(job_counts.values()),
                "visible_events": visible_events,
                "pending_events": pending_events,
                "memory_revision": companion["memory_revision"],
                "cursor_at": cursor_at,
            }

    def list_items(self, actor: Actor, companion_id: UUID, kind=None, status=None, limit=50, query=None, sort="created"):
        """User-facing list; only authorized role rows, never private logs."""
        with self.db.engine.begin() as conn:
            authorize(conn, actor, companion_id)
            query_obj = select(mm.memory_items).where(mm.memory_items.c.account_id == actor.account_id, mm.memory_items.c.companion_id == companion_id)
            if kind: query_obj = query_obj.where(mm.memory_items.c.kind == kind)
            if status: query_obj = query_obj.where(mm.memory_items.c.status == status)
            if query:
                tokens = terms(query)[:20]
                if tokens:
                    source_match = exists(
                        select(mm.source_refs.c.id)
                        .join(m.events, mm.source_refs.c.event_id == m.events.c.id)
                        .where(
                            mm.source_refs.c.memory_id == mm.memory_items.c.id,
                            m.events.c.account_id == actor.account_id,
                            m.events.c.companion_id == companion_id,
                            m.events.c.visibility == "visible",
                            or_(*[m.events.c.content["text"].astext.ilike("%" + token + "%") for token in tokens]),
                        )
                    )
                    query_obj = query_obj.where(or_(*[mm.memory_items.c.content.ilike("%" + token + "%") for token in tokens], source_match))
            if sort == "importance":
                query_obj = query_obj.order_by(mm.memory_items.c.importance.desc(), mm.memory_items.c.created_at.desc())
            elif sort == "expiry":
                query_obj = query_obj.order_by(mm.memory_items.c.expires_at.asc().nullslast(), mm.memory_items.c.created_at.desc())
            else:
                query_obj = query_obj.order_by(mm.memory_items.c.created_at.desc())
            rows = [dict(row) for row in conn.execute(query_obj.limit(limit)).mappings()]
            for row in rows:
                row["source_count"] = conn.scalar(select(func.count()).select_from(mm.source_refs).where(mm.source_refs.c.memory_id == row["id"]))
                row["freshness"] = round(freshness(row), 4)
            return rows

    def update_item(self, actor: Actor, memory_id: UUID, status=None, importance=None):
        """Hide/restore or change importance of one memory item."""
        if status is not None and status not in ("active", "hidden"):
            raise AppError("INVALID_STATUS", "记忆状态只能是有效或隐藏", 422)
        if importance is not None and not 0 <= float(importance) <= 1:
            raise AppError("INVALID_IMPORTANCE", "重要度必须在 0 到 1 之间", 422)
        with self.db.engine.begin() as conn:
            memory = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id == memory_id).with_for_update()).mappings().first()
            if not memory:
                raise AppError("NOT_FOUND", "记忆不存在", 404)
            lock(conn, memory["companion_id"])
            companion = authorize(conn, actor, memory["companion_id"])
            if memory["account_id"] != actor.account_id:
                raise AppError("FORBIDDEN", "无权修改这条记忆", 403)
            if memory["status"] not in ("active", "hidden"):
                raise AppError("MEMORY_STATE", "已替代或已删除的记忆不能直接修改", 409)
            values = {}
            if status is not None and status != memory["status"]:
                values["status"] = status
            if importance is not None and abs(float(importance) - float(memory["importance"] or 0.5)) > 1e-6:
                value = float(importance)
                values["importance"] = value
                values["expires_at"] = now_utc() + timedelta(days=ttl_for(memory["kind"], value, int(memory["access_count"] or 0)))
            if values:
                conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == memory_id).values(**values))
                revision = companion["memory_revision"] + 1
                conn.execute(m.companions.update().where(m.companions.c.id == memory["companion_id"]).values(memory_revision=revision))
                from ..identity import models as identity
                conn.execute(identity.runtime_bindings.update().where(identity.runtime_bindings.c.companion_id == memory["companion_id"]).values(status="stale"))
                from ..delivery.invalidation import invalidate_active
                invalidate_active(conn, memory["companion_id"], "memory.updated")
            else:
                revision = companion["memory_revision"]
        return {"memory_id": memory_id, "status": values.get("status", memory["status"]), "importance": values.get("importance", memory["importance"]), "memory_revision": revision}

    def set_status(self, actor: Actor, memory_id: UUID, status: str):
        return self.update_item(actor, memory_id, status=status)

    def touch(self, conn, memory_ids, reference=None):
        """Access reinforcement inside the caller's context-build transaction."""
        reference = reference or now_utc()
        ids = [memory_id for memory_id in set(memory_ids or []) if memory_id is not None]
        if not ids:
            return 0
        rows = list(conn.execute(select(mm.memory_items).where(mm.memory_items.c.id.in_(ids))).mappings())
        for row in rows:
            access = int(row["access_count"] or 0) + 1
            conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == row["id"]).values(**extend_after_access(row, access, reference)))
        return len(rows)

    async def recall_debug(self, scope, query, limit=10):
        """Explainable recall for the memory page: score, route and lifecycle fields."""
        hits = await self.search(scope, query)
        memory_ids = [hit.memory_id for hit in hits if hit.memory_id]
        metadata = {}
        if memory_ids:
            with self.db.engine.connect() as conn:
                for row in conn.execute(select(mm.memory_items).where(mm.memory_items.c.id.in_(memory_ids))).mappings():
                    metadata[row["id"]] = dict(row)
        reference = now_utc()
        result = []
        for hit in hits[:limit]:
            item = hit.model_dump(mode="json")
            row = metadata.get(hit.memory_id)
            if row:
                item.update({
                    "kind": row["kind"], "status": row["status"], "importance": row["importance"],
                    "access_count": row["access_count"], "expires_at": row["expires_at"],
                    "decay_type": row["decay_type"], "freshness": freshness(row, reference),
                })
            item["routes"] = ["structured"] if hit.memory_id else ["keyword"]
            result.append(item)
        return result

    def get_item(self, actor: Actor, memory_id: UUID):
        with self.db.engine.begin() as conn:
            memory = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id == memory_id)).mappings().first()
            if not memory: return None
            authorize(conn, actor, memory["companion_id"])
            if memory["account_id"] != actor.account_id: return None
            refs = [dict(row) for row in conn.execute(select(mm.source_refs).where(mm.source_refs.c.memory_id == memory_id).order_by(mm.source_refs.c.event_id)).mappings()]
            events = {}
            if refs:
                for row in conn.execute(select(m.events).where(m.events.c.id.in_([ref["event_id"] for ref in refs]), m.events.c.visibility == "visible")).mappings():
                    events[row["id"]] = row
            for ref in refs:
                event = events.get(ref["event_id"])
                ref["available"] = event is not None
                if event:
                    text = event["content"].get("text", "")
                    ref["quote"] = text[ref["start_offset"]:ref["end_offset"]] if ref["start_offset"] is not None else text
                    ref["occurred_at"] = event["occurred_at"]
            detail = dict(memory)
            detail["source_refs"] = refs
            detail["freshness"] = round(freshness(memory), 4)
            return detail
