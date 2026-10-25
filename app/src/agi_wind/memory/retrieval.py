from uuid import UUID
from sqlalchemy import select, or_, func, literal_column,exists
from sqlalchemy.dialects.postgresql import insert
from . import models as mm
from .keyword import terms
from .chunking import chunk_text, estimate_tokens
from ..archive import models as m
from ..archive.repository import digest, lock
from ..identity.service import authorize
from ..core.types import MemoryHit, SourceRef


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
            structured=conn.execute(select(mm.memory_items).where(mm.memory_items.c.account_id==scope.account_id,mm.memory_items.c.companion_id==scope.companion_id,or_(mm.memory_items.c.user_id==scope.user_id,mm.memory_items.c.user_id.is_(None)),mm.memory_items.c.status=="active",mm.memory_items.c.epistemic_type!="fictional",or_(*[mm.memory_items.c.content.ilike("%"+token+"%") for token in query_terms])).order_by(mm.memory_items.c.revision.desc()).limit(30)).mappings()
            hits = []
            for memory in structured:
                refs=list(conn.execute(select(mm.source_refs).join(m.events,mm.source_refs.c.event_id==m.events.c.id).where(mm.source_refs.c.memory_id==memory["id"],m.events.c.account_id==scope.account_id,m.events.c.companion_id==scope.companion_id,m.events.c.visibility=="visible")).mappings())
                if refs: hits.append(MemoryHit(memory_id=memory["id"],content=memory["content"],score=1.0,epistemic_type=memory["epistemic_type"],source_refs=[SourceRef(**{key:ref[key] for key in ("event_id","json_pointer","start_offset","end_offset","quote_hash")}) for ref in refs]))
            historical=set(conn.scalars(select(mm.source_refs.c.event_id).join(mm.memory_items).where(mm.memory_items.c.companion_id==scope.companion_id,mm.memory_items.c.status!="active")))
            for key in sorted(scores, key=scores.get, reverse=True):
                row = records[key]
                hits.append(MemoryHit(chunk_id=row["id"], content=row["content"], score=scores[key], epistemic_type="assistant_generated" if row["role"] == "assistant" else "user_asserted", source_refs=[SourceRef(event_id=row["source_id"], start_offset=row["start_offset"], end_offset=row["end_offset"], quote_hash=__import__('hashlib').sha256(row["content"].encode()).hexdigest())], time_range=(row["occurred_at"], row["occurred_at"]),status="historical" if row["source_id"] in historical else "active"))
            return hits
