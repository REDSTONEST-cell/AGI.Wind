import json
from collections import Counter
from sqlalchemy import select
from .chunking import estimate_tokens
from .retrieval import MemoryService
from ..identity.service import IdentityService, authorize
from ..archive import models as m
from ..archive.repository import lock
from ..core.types import ContextBundle
from ..core.errors import AppError


def check_current_input(text, token_count=estimate_tokens, budget=12000):
    if token_count(text) > budget:
        raise AppError("INPUT_TOO_LONG", "当前输入超出模型预算，请分段发送", 413)


def select_memories(hits, token_count=estimate_tokens, budget=3000):
    selected, counts, used = [], Counter(), 0
    for hit in hits:
        ids = {ref.event_id for ref in hit.source_refs}
        cost = token_count(hit.content)
        if not ids or any(counts[key] >= 2 for key in ids) or used + cost > budget: continue
        selected.append(hit)
        counts.update(ids)
        used += cost
        if len(selected) >= 12: break
    return selected


class ContextBuilder:
    def __init__(self, db, token_count=estimate_tokens): self.db, self.token_count = db, token_count

    async def build(self, scope, current_input, model_ref, current_request_id=None):
        with self.db.engine.connect() as conn: initial_revision=authorize(conn,scope,scope.companion_id)["memory_revision"]
        check_current_input(current_input.text, self.token_count)
        identity = await IdentityService(self.db).snapshot(scope, scope.companion_id)
        identity_cost = self.token_count(identity.model_dump_json())
        current_cost = self.token_count(current_input.text)
        if identity_cost + current_cost > 12000:
            raise AppError("CONTEXT_TOO_LONG", "人格与当前输入超出预算，请调整后重试", 413)
        memory = MemoryService(self.db, self.token_count)
        await memory.index_events(scope)
        hits = await memory.search(scope, current_input.text)
        selected = select_memories(hits, self.token_count, min(3000, 12000-identity_cost-current_cost))
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
                cost = self.token_count(json.dumps(row["content"], ensure_ascii=False))
                if recent_cost + cost > 4000 or used + cost > 12000: break
                recent.append({"event_id": str(row["id"]), "role": row["role"], "content": row["content"]})
                used += cost
                recent_cost += cost
            if identity.version != companion["active_persona_version"] or initial_revision!=companion["memory_revision"]:
                raise AppError("STALE_CONTEXT", "人格已变化，请重试", 409)
            # Sources are rechecked while the same scope write lock is held.
            valid_ids = set(conn.scalars(select(m.events.c.id).where(m.events.c.account_id == scope.account_id, m.events.c.companion_id == scope.companion_id, m.events.c.visibility == "visible")))
            selected = [hit for hit in selected if all(ref.event_id in valid_ids for ref in hit.source_refs)]
            return ContextBundle(memory_revision=companion["memory_revision"], persona_snapshot=identity, recent_events=list(reversed(recent)), memories=selected, current_input=current_input, vision_observations=[], token_estimate=used, selected_source_ids=list({ref.event_id for hit in selected for ref in hit.source_refs}))
