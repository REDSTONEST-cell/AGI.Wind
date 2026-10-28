import json
from collections import Counter
from sqlalchemy import select, func
from .chunking import estimate_tokens
from .retrieval import MemoryService
from .semantic import active_clause, item_scope, verified_refs, chunk_query, chunk_valid
from . import models as mm
from .access import scene_of, speaker_label
from ..identity.service import IdentityService, authorize
from ..archive import models as m
from ..archive.repository import lock
from ..core.types import ContextBundle, SceneScope, SourceRef
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
        if any(counts[key] >= 2 for key in ids) or used + cost > budget: continue
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
        identity = await IdentityService(self.db).snapshot_scene(scope)
        group_scene = scene_of(scope) == "group"
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
            query = (select(m.events)
                .add_columns(m.external_principals.c.card_name.label("sender_card"),
                             m.external_principals.c.display_name.label("sender_name"),
                             m.external_principals.c.external_user_id.label("sender_external_id"),
                             m.external_principals.c.is_owner.label("sender_is_owner"))
                .outerjoin(m.external_principals, m.events.c.sender_principal_id == m.external_principals.c.id)
                .where(m.events.c.companion_id == scope.companion_id, m.events.c.account_id == scope.account_id, m.events.c.conversation_id == scope.conversation_id, m.events.c.visibility == "visible"))
            if current_request_id: query = query.where(m.events.c.request_id != current_request_id)
            rows = conn.execute(query.order_by(m.events.c.recorded_at.desc()).limit(40)).mappings()
            recent_cost = 0
            for row in rows:
                if row['role'] not in ('user', 'assistant'): continue
                content = {'text':row['content'].get('text', '')}
                if row['content'].get('attachments'): content['attachments'] = row['content']['attachments']
                cost = self.token_count(json.dumps(content, ensure_ascii=False))
                if recent_cost + cost > 4000 or used + cost > self.total_budget: break
                entry = {"event_id": str(row["id"]), "role": row["role"], "content": content, "recorded_at": row["recorded_at"].isoformat()}
                if group_scene and row["role"] == "user":
                    entry["speaker"] = speaker_label(None, {"card_name": row["sender_card"], "display_name": row["sender_name"],
                                                            "external_user_id": row["sender_external_id"]})
                recent.append(entry)
                used += cost
                recent_cost += cost
            current_speaker = None
            if group_scene and current_request_id:
                speaker = conn.execute(select(m.external_principals.c.card_name, m.external_principals.c.display_name,
                    m.external_principals.c.external_user_id).select_from(m.events.outerjoin(
                    m.external_principals, m.events.c.sender_principal_id == m.external_principals.c.id)).where(
                    m.events.c.request_id == current_request_id, m.events.c.role == "user")).mappings().first()
                if speaker:
                    current_speaker = speaker_label(None, speaker)
            if identity.version != companion["active_persona_version"] or initial_revision!=companion["memory_revision"]:
                raise AppError("STALE_CONTEXT", "人格已变化，请重试", 409)
            # Sources are rechecked while the same scope write lock is held.
            selected_memory_ids = [hit.memory_id for hit in selected if hit.memory_id]
            current_memories = list(conn.execute(select(mm.memory_items).where(item_scope(scope), active_clause(),
                mm.memory_items.c.id.in_(selected_memory_ids))).mappings())
            current_refs = verified_refs(conn, scope, current_memories)
            source_counts = dict(conn.execute(select(mm.source_refs.c.memory_id, func.count()).where(
                mm.source_refs.c.memory_id.in_(selected_memory_ids)).group_by(mm.source_refs.c.memory_id)).all())
            current_contents = {row["id"]:row["content"] for row in current_memories}
            current_chunks = {row["id"]:row for row in conn.execute(chunk_query(scope).where(
                mm.retrieval_chunks.c.id.in_([hit.chunk_id for hit in selected if hit.chunk_id]))).mappings() if chunk_valid(row)}
            def memory_valid(hit):
                if not hit.memory_id: return True
                if source_counts.get(hit.memory_id, 0) == 0: return True
                return hit.memory_id in current_refs
            selected = [hit for hit in selected if hit.status == "active" and memory_valid(hit) and
                        (current_contents.get(hit.memory_id) == hit.content if hit.memory_id else
                         hit.chunk_id in current_chunks and current_chunks[hit.chunk_id]["content"] == hit.content)]
            # Refresh attribution from the same locked snapshot. A memory may have
            # gained/lost references since ranking without changing its wording.
            for hit in selected:
                if hit.memory_id:
                    hit.source_refs = [SourceRef(**{key:ref[key] for key in
                        ("event_id","json_pointer","start_offset","end_offset","quote_hash")})
                        for ref in current_refs.get(hit.memory_id, [])]
                else:
                    row = current_chunks[hit.chunk_id]
                    import hashlib
                    hit.source_refs = [SourceRef(event_id=row["source_id"],start_offset=row["start_offset"],
                        end_offset=row["end_offset"],quote_hash=hashlib.sha256(row["content"].encode()).hexdigest())]
            memory.touch(conn, [hit.memory_id for hit in selected if hit.memory_id])
            return ContextBundle(memory_revision=companion["memory_revision"], persona_snapshot=identity, recent_events=list(reversed(recent)), memories=selected, current_input=current_input, vision_observations=[], token_estimate=used, selected_source_ids=list({ref.event_id for hit in selected for ref in hit.source_refs}), current_speaker=current_speaker)
