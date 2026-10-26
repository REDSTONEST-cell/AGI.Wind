"""Member-pool memory writer: server-verified member facts, isolated per person and room.

The model only proposes candidate text and a quote. The fact subject is always the
verified sender principal of the quoted event, the room is always the binding the job
was created for, and no candidate can move a fact into the owner pool or another group.
"""
import hashlib
import re
from uuid import uuid4

from sqlalchemy import and_, or_, select

from . import models as mm
from .extraction import _semantic_text
from .lifecycle import extend_after_access, lifecycle_values, now_utc
from ..archive import models as m
from ..core.errors import AppError
from ..identity.service import authorize


class MemberMemoryWriter:
    def __init__(self, db, retention_days=None):
        self.db, self.retention_days = db, retention_days

    def _source_rows(self, conn, scope, binding, batch_ids):
        if not batch_ids:
            return {}
        rows = conn.execute(
            select(m.events)
            .add_columns(m.external_principals.c.id.label("principal_id"),
                         m.external_principals.c.display_name.label("principal_display_name"),
                         m.external_principals.c.card_name.label("principal_card_name"),
                         m.external_principals.c.external_user_id.label("principal_external_id"),
                         m.external_principals.c.is_owner.label("principal_is_owner"))
            .join(m.external_principals, m.events.c.sender_principal_id == m.external_principals.c.id)
            .where(m.events.c.id.in_(list(batch_ids)),
                   m.events.c.account_id == scope.account_id,
                   m.events.c.companion_id == scope.companion_id,
                   m.events.c.conversation_id == binding["conversation_id"],
                   m.events.c.visibility == "visible",
                   m.external_principals.c.is_owner.is_(False))).mappings()
        return {row["id"]: row for row in rows}

    def commit_candidates(self, conn, scope, binding, candidates, batch, expected_revision):
        authorize(conn, scope, scope.companion_id)
        companion = conn.execute(select(m.companions).where(
            m.companions.c.id == scope.companion_id)).mappings().one()
        if companion["memory_revision"] != expected_revision:
            raise AppError("STALE_CONTEXT", "提炼任务的记忆版本已失效", 409)
        room_id = binding["id"]
        by_id = {row["id"]: row for row in batch}
        current_rows = self._source_rows(conn, scope, binding, by_id)
        stored = reinforced = quarantined = ignored = 0
        provenance_changed = False
        reference = now_utc()
        for candidate in candidates:
            reason, refs, principals, fictional = None, [], set(), False
            for ref in candidate.source_refs:
                snapshot = by_id.get(ref.event_id)
                current = current_rows.get(ref.event_id)
                if snapshot is None or current is None:
                    reason = "UNVERIFIABLE_SOURCE"
                    break
                if current["content_sha256"] != snapshot["content_sha256"]:
                    reason = "SOURCE_CHANGED"
                    break
                if current["principal_is_owner"]:
                    reason = "OWNER_FACT_NOT_MEMBER_POOL"
                    break
                text = current["content"].get("text", "")
                if ref.quote not in text:
                    reason = "QUOTE_NOT_FOUND"
                    break
                if text.count(ref.quote) != 1:
                    reason = "AMBIGUOUS_QUOTE"
                    break
                if candidate.epistemic_type != "user_asserted":
                    reason = "MEMBER_STATEMENT_REQUIRED"
                    break
                if current["role"] != "user" or current["kind"] != "user_input":
                    reason = "NOT_A_MEMBER_MESSAGE"
                    break
                principals.add(current["principal_id"])
                fictional = fictional or bool(re.search("故事设定|虚构|假设", text))
                start = text.index(ref.quote)
                refs.append({"event_id": ref.event_id, "json_pointer": "/text",
                             "start_offset": start, "end_offset": start + len(ref.quote),
                             "quote_hash": hashlib.sha256(ref.quote.encode()).hexdigest()})
            if not reason and len(principals) != 1:
                reason = "AMBIGUOUS_MEMBER_SUBJECT"
            if reason:
                conn.execute(mm.quarantine.insert().values(
                    account_id=scope.account_id, companion_id=scope.companion_id,
                    candidate=candidate.model_dump(mode="json"), reason=reason))
                quarantined += 1
                continue
            principal_id = next(iter(principals))
            epistemic = "fictional" if fictional else candidate.epistemic_type
            predicate, value = map(_semantic_text, (candidate.predicate, candidate.value))
            semantic = bool(predicate and value)
            scoped = [mm.memory_items.c.account_id == scope.account_id,
                      mm.memory_items.c.companion_id == scope.companion_id,
                      mm.memory_items.c.pool_kind == "member",
                      mm.memory_items.c.subject_principal_id == principal_id,
                      mm.memory_items.c.visibility_room_id == room_id,
                      mm.memory_items.c.summary_level == ""]
            exact = and_(mm.memory_items.c.content == candidate.content,
                         mm.memory_items.c.kind == candidate.kind,
                         mm.memory_items.c.epistemic_type == epistemic)
            same_value = and_(mm.memory_items.c.predicate == predicate,
                              mm.memory_items.c.value == value,
                              mm.memory_items.c.epistemic_type == epistemic)
            match = or_(exact, same_value) if semantic else exact
            active = and_(mm.memory_items.c.status == "active",
                          or_(mm.memory_items.c.is_pinned.is_(True), mm.memory_items.c.expires_at.is_(None),
                              mm.memory_items.c.expires_at > reference))
            suppressed = conn.execute(select(mm.memory_items.c.id).where(*scoped, match, ~active)).first()
            if suppressed:
                ignored += 1
                continue
            existing = conn.execute(select(mm.memory_items).where(*scoped, match, active)
                                    .order_by(mm.memory_items.c.revision.desc())).mappings().first()
            if existing:
                semantic_updates = {name: val for name, val in
                                    (("predicate", predicate), ("value", value)) if val and not existing[name]}
                if semantic_updates:
                    provenance_changed = True
                for ref in refs:
                    duplicate = conn.execute(select(mm.source_refs.c.id).where(
                        mm.source_refs.c.memory_id == existing["id"], mm.source_refs.c.event_id == ref["event_id"],
                        mm.source_refs.c.start_offset == ref["start_offset"],
                        mm.source_refs.c.end_offset == ref["end_offset"])).first()
                    if not duplicate:
                        conn.execute(mm.source_refs.insert().values(memory_id=existing["id"], **ref))
                        provenance_changed = True
                reinforced += 1
                conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == existing["id"]).values(
                    importance=min(1.0, float(existing["importance"] or 0.5) + 0.03),
                    last_reinforced_at=m.now(),
                    expires_at=extend_after_access(existing, int(existing["access_count"] or 0),
                                                   retention_days=self.retention_days).get("expires_at"),
                    **semantic_updates))
            else:
                memory_id = uuid4()
                valid_from = max(current_rows[ref.event_id]["recorded_at"] for ref in candidate.source_refs)
                occupied = []
                if semantic:
                    occupied = list(conn.execute(select(mm.memory_items).where(
                        *scoped, active,
                        mm.memory_items.c.predicate == predicate,
                        mm.memory_items.c.value != "", mm.memory_items.c.value != value,
                        mm.memory_items.c.epistemic_type == "user_asserted")).mappings())
                status = "disputed" if occupied else "active"
                conn.execute(mm.memory_items.insert().values(
                    id=memory_id, account_id=scope.account_id, companion_id=scope.companion_id,
                    user_id=scope.user_id, kind=candidate.kind, content=candidate.content,
                    epistemic_type=epistemic, confidence=candidate.confidence, status=status,
                    revision=expected_revision + 1, subject_key=str(principal_id),
                    predicate=predicate, value=value, valid_from=valid_from, summary_level="",
                    pool_kind="member", subject_principal_id=principal_id,
                    origin_room_id=room_id, visibility="group_public", visibility_room_id=room_id,
                    **lifecycle_values(candidate.kind, candidate.confidence, retention_days=self.retention_days)))
                for ref in refs:
                    conn.execute(mm.source_refs.insert().values(memory_id=memory_id, **ref))
                for old in occupied:
                    conn.execute(mm.memory_relations.insert().values(
                        from_memory_id=memory_id, to_memory_id=old["id"], relation_type="contradicts"))
                stored += 1
        revision = expected_revision
        if stored or provenance_changed:
            revision = expected_revision + 1
            conn.execute(m.companions.update().where(m.companions.c.id == scope.companion_id)
                         .values(memory_revision=revision))
            from ..identity import models as identity
            conn.execute(identity.runtime_bindings.update().where(
                identity.runtime_bindings.c.companion_id == scope.companion_id).values(status="stale"))
            from ..delivery.invalidation import invalidate_active
            invalidate_active(conn, scope.companion_id, "memory.changed")
        return {"stored": stored, "reinforced": reinforced, "quarantined": quarantined,
                "ignored": ignored, "memory_revision": revision}
