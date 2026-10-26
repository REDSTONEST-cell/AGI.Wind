import json
from collections import Counter
from sqlalchemy import select
from .chunking import estimate_tokens
from .retrieval import MemoryService
from .semantic import active_clause, item_scope, verified_refs, chunk_query, chunk_valid
from . import models as mm
from ..identity.service import IdentityService, authorize
from ..archive import models as m
from ..archive.repository import lock
from ..core.types import ContextBundle, SourceRef
from ..core.errors import AppError


DEFAULT_TOTAL_BUDGET = 48000
DEFAULT_INPUT_BUDGET = 12000


def check_current_input(text, token_count=estimate_tokens, budget=DEFAULT_INPUT_BUDGET):
    if token_count(text) > budget:
        raise AppError("INPUT_TOO_LONG", "当前输入超出模型预算，请分段发送", 413)


def select_memories(hits, token_count=estimate_tokens, budget=3000):
    selected, counts, used = [], Counter(), 0
    for hit in hits:
        if hit.status != "active": continue
        ids = {ref.event_id for ref in hit.source_refs}
        cost = token_count(hit.content)
        if not ids or any(counts[key] >= 2 for key in ids) or used + cost > budget: continue
        selected.append(hit)
        counts.update(ids)
        used += cost
        if len(selected) >= 12: break
    return selected


class ContextBuilder:
    def __init__(self, db, token_count=estimate_tokens, total_budget=DEFAULT_TOTAL_BUDGET, input_budget=DEFAULT_INPUT_BUDGET):
        self.db, self.token_count = db, token_count
        self.total_budget, self.input_budget = total_budget, input_budget

    async def build(self, scope, current_input, model_ref, current_request_id=None):
        with self.db.engine.connect() as conn: initial_revision=authorize(conn,scope,scope.companion_id)["memory_revision"]
        check_current_input(current_input.text, self.token_count, self.input_budget)
        identity = await IdentityService(self.db).snapshot(scope, scope.companion_id)
        identity_cost = self.token_count(identity.model_dump_json())
        current_cost = self.token_count(current_input.text)
        if identity_cost + current_cost > self.total_budget:
            raise AppError("CONTEXT_TOO_LONG", "人格与当前输入超出预算，请精简人格档案后重试", 413)
        memory = MemoryService(self.db, self.token_count)
        await memory.index_events(scope)
        hits = await memory.search(scope, current_input.text)
        selected = select_memories(hits, self.token_count, min(3000, self.total_budget-identity_cost-current_cost))
        used = identity_cost + current_cost + sum(self.token_count(h.content) for h in selected)
        recent = []
        with self.db.engine.begin() as conn:
            lock(conn, scope.companion_id)
            companion = authorize(conn, scope, scope.companion_id)
            query = select(m.events).where(m.events.c.companion_id == scope.companion_id, m.events.c.account_id == scope.account_id, m.events.c.conversation_id == scope.conversation_id, m.events.c.visibility == "visible")
            if current_request_id: query = query.where(m.events.c.request_id != current_request_id)
            rows = conn.execute(query.order_by(m.events.c.recorded_at.desc()).limit(40)).mappings()
            recent_cost = 0
            for row in rows:
                if row['role'] not in ('user', 'assistant'): continue
                content = {'text':row['content'].get('text', '')}
                if row['content'].get('attachments'): content['attachments'] = row['content']['attachments']
                cost = self.token_count(json.dumps(content, ensure_ascii=False))
                if recent_cost + cost > 4000 or used + cost > self.total_budget: break
                recent.append({"event_id": str(row["id"]), "role": row["role"], "content": content})
                used += cost
                recent_cost += cost
            if identity.version != companion["active_persona_version"] or initial_revision!=companion["memory_revision"]:
                raise AppError("STALE_CONTEXT", "人格已变化，请重试", 409)
            # Sources are rechecked while the same scope write lock is held.
            selected_memory_ids = [hit.memory_id for hit in selected if hit.memory_id]
            current_memories = list(conn.execute(select(mm.memory_items).where(item_scope(scope), active_clause(),
                mm.memory_items.c.id.in_(selected_memory_ids))).mappings())
            current_refs = verified_refs(conn, scope, current_memories)
            current_contents = {row["id"]:row["content"] for row in current_memories if row["id"] in current_refs}
            current_chunks = {row["id"]:row for row in conn.execute(chunk_query(scope).where(
                mm.retrieval_chunks.c.id.in_([hit.chunk_id for hit in selected if hit.chunk_id]))).mappings() if chunk_valid(row)}
            selected = [hit for hit in selected if hit.status == "active" and
                        (current_contents.get(hit.memory_id) == hit.content if hit.memory_id else
                         hit.chunk_id in current_chunks and current_chunks[hit.chunk_id]["content"] == hit.content)]
            # Refresh attribution from the same locked snapshot. A memory may have
            # gained/lost references since ranking without changing its wording.
            for hit in selected:
                if hit.memory_id:
                    hit.source_refs = [SourceRef(**{key:ref[key] for key in
                        ("event_id","json_pointer","start_offset","end_offset","quote_hash")})
                        for ref in current_refs[hit.memory_id]]
                else:
                    row = current_chunks[hit.chunk_id]
                    import hashlib
                    hit.source_refs = [SourceRef(event_id=row["source_id"],start_offset=row["start_offset"],
                        end_offset=row["end_offset"],quote_hash=hashlib.sha256(row["content"].encode()).hexdigest())]
            memory.touch(conn, [hit.memory_id for hit in selected if hit.memory_id])
            return ContextBundle(memory_revision=companion["memory_revision"], persona_snapshot=identity, recent_events=list(reversed(recent)), memories=selected, current_input=current_input, vision_observations=[], token_estimate=used, selected_source_ids=list({ref.event_id for hit in selected for ref in hit.source_refs}))
