"""Durable, idempotent memory-extraction jobs built on the existing outbox/worker.

Flow per job:
1. short transaction A: verify the lease, read the companion cursor and a bounded event batch;
2. outside any transaction: call the extractor model (no locks held, no authoritative write);
3. short transaction B: verify the lease again, re-check cursor and memory_revision, re-check
   source visibility/content hashes, then store candidates + advance the cursor atomically.
"""
import logging
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from .extraction import MemoryWriter, source_owned_clause, owner_authored_clause
from .members import MemberMemoryWriter
from ..archive import models as m
from ..archive.repository import lock
from ..identity.service import authorize
from ..core.types import SceneScope, Scope
from ..core.errors import AppError

MEMORY_EXTRACT = "memory.extract"
MEMBER_EXTRACT = "memory.extract.member"
PIPELINE_VERSION = 1
MAX_EVENT_CHARS = 4000
MAX_BATCH_CHARS = 16000
logger = logging.getLogger("nagi.memory")
AUTO_EXTRACT_TURNS = 8
AUTO_EXTRACT_IDLE_SECONDS = 120


def _coalesce_automatic(conn, event_type, aggregate_id, generation_id, delay_seconds):
    """Sliding idle deadline; eight distinct arrivals make the same job due now.

    Scheduling metadata is durable in outbox, so restart does not lose the
    accumulated turns. Manual/continuation jobs and retry/budget holds keep
    their deadline. The pending-state CAS avoids touching a claimed job.
    """
    row = conn.execute(select(m.outbox).where(m.outbox.c.event_type == event_type,
        m.outbox.c.aggregate_id == aggregate_id, m.outbox.c.state == 'pending').limit(1)).mappings().first()
    if row is None:
        return False
    payload = dict(row['payload'] or {})
    if payload.get('manual') or payload.get('continuation') or payload.get('last_error_code'):
        return True
    turns = list(payload.get('automatic_turn_ids') or [str(payload.get('generation_id') or row['id'])])
    marker = str(generation_id or uuid4())
    if marker in turns or len(turns) >= AUTO_EXTRACT_TURNS:
        return True
    turns.append(marker)
    delay = 0 if len(turns) >= AUTO_EXTRACT_TURNS else max(0, delay_seconds)
    updated = conn.execute(m.outbox.update().where(m.outbox.c.id == row['id'],
        m.outbox.c.state == 'pending').values(payload={**payload, 'automatic_turn_ids': turns},
        available_at=m.now()+timedelta(seconds=delay)))
    return bool(updated.rowcount)


def enqueue_extraction(conn, scope, generation_id):
    """Coalesce completed turns until eight arrivals or 120 seconds of idle."""
    lock(conn, scope.companion_id)
    if _coalesce_automatic(conn, MEMORY_EXTRACT, scope.companion_id, generation_id, AUTO_EXTRACT_IDLE_SECONDS):
        return None
    return conn.execute(
        insert(m.outbox)
        .values(
            event_type=MEMORY_EXTRACT,
            aggregate_id=scope.companion_id,
            payload={
                "account_id": str(scope.account_id),
                "user_id": str(scope.user_id),
                "companion_id": str(scope.companion_id),
                "conversation_id": str(scope.conversation_id),
                "generation_id": str(generation_id),
                "pipeline_version": PIPELINE_VERSION,
                "automatic_turn_ids": [str(generation_id)],
            },
            dedupe_key=f"memory-extract:{scope.companion_id}:{generation_id}",
            available_at=m.now()+timedelta(seconds=AUTO_EXTRACT_IDLE_SECONDS),
        )
        .on_conflict_do_nothing()
        .returning(m.outbox.c.id)
    ).scalar()


def enqueue_manual_extraction(conn, scope):
    """Queue one extra batch now (used by the memory page while no new chat turn is pending)."""
    return conn.execute(
        insert(m.outbox)
        .values(
            event_type=MEMORY_EXTRACT,
            aggregate_id=scope.companion_id,
            payload={
                "account_id": str(scope.account_id),
                "user_id": str(scope.user_id),
                "companion_id": str(scope.companion_id),
                "conversation_id": str(scope.conversation_id),
                "manual": True,
                "pipeline_version": PIPELINE_VERSION,
            },
            dedupe_key=f"memory-extract-manual:{scope.companion_id}:{uuid4()}",
        )
        .returning(m.outbox.c.id)
    ).scalar()


def enqueue_member_extraction(conn, scope, generation_id=None, observed=False, delay_seconds=AUTO_EXTRACT_IDLE_SECONDS):
    """Queue member-pool extraction for one group room.

    Both replies and observed messages use a sliding idle deadline, or eight
    arrivals, per room. One pending job retains all members' source messages.
    """
    room_id = getattr(scope, "room_id", None)
    if room_id is None:
        return None
    lock(conn, scope.companion_id)
    if _coalesce_automatic(conn, MEMBER_EXTRACT, room_id, generation_id, delay_seconds):
        return None
    delay = max(0, int(delay_seconds))
    return conn.execute(
        insert(m.outbox)
        .values(
            event_type=MEMBER_EXTRACT,
            aggregate_id=room_id,
            payload={
                "account_id": str(scope.account_id),
                "user_id": str(scope.user_id),
                "companion_id": str(scope.companion_id),
                "conversation_id": str(scope.conversation_id),
                "binding_id": str(room_id),
                "generation_id": str(generation_id) if generation_id else None,
                "observed": bool(observed),
                "pipeline_version": PIPELINE_VERSION,
                "automatic_turn_ids": [str(generation_id or uuid4())],
            },
            dedupe_key=f"memory-extract-member:{room_id}:{generation_id or uuid4()}",
            available_at=m.now() + timedelta(seconds=delay),
        )
        .on_conflict_do_nothing()
        .returning(m.outbox.c.id)
    ).scalar()


def member_scope_from_payload(payload):
    try:
        account_id = UUID(str(payload["account_id"]))
        user_id = UUID(str(payload["user_id"]))
        companion_id = UUID(str(payload["companion_id"]))
        conversation_id = UUID(str(payload["conversation_id"]))
        binding_id = UUID(str(payload["binding_id"]))
    except (KeyError, ValueError, TypeError):
        raise AppError("INVALID_JOB", "群成员记忆任务缺少有效范围", 422) from None
    return SceneScope(account_id=account_id, user_id=user_id, companion_id=companion_id,
                      conversation_id=conversation_id, scene="group", room_id=binding_id)


def _member_batch_row(row):
    content = row["content"] if isinstance(row["content"], dict) else {}
    return {
        "id": row["id"], "content_sha256": row["content_sha256"],
        "role": row["role"], "kind": row["kind"], "recorded_at": row["recorded_at"],
        "content": content, "principal_id": row["principal_id"],
        "state": content.get("state"),
    }


def _member_model_event(row):
    name = row["principal_card_name"] or row["principal_display_name"] or row["principal_external_id"] or "群成员"
    text = row["content"].get("text", "") if isinstance(row["content"], dict) else ""
    return {
        "event_id": str(row["id"]), "speaker": name,
        "sender_external_id": row["principal_external_id"],
        "role": "user", "kind": row["kind"],
        "occurred_at": row["recorded_at"].isoformat() if row["recorded_at"] else None,
        "text": text[:MAX_EVENT_CHARS],
    }


def _load_member_batch(conn, binding, limit):
    query = (select(m.events)
        .add_columns(m.external_principals.c.id.label("principal_id"),
                     m.external_principals.c.display_name.label("principal_display_name"),
                     m.external_principals.c.card_name.label("principal_card_name"),
                     m.external_principals.c.external_user_id.label("principal_external_id"))
        .join(m.external_principals, m.events.c.sender_principal_id == m.external_principals.c.id)
        .where(m.events.c.account_id == binding["account_id"],
               m.events.c.companion_id == binding["companion_id"],
               m.events.c.conversation_id == binding["conversation_id"],
               m.events.c.visibility == "visible", m.events.c.role == "user",
               m.events.c.kind == "user_input",
               m.external_principals.c.is_owner.is_(False)))
    cursor_at, cursor_id = binding["member_extract_cursor_at"], binding["member_extract_cursor_id"]
    if cursor_at is not None and cursor_id is not None:
        query = query.where(or_(m.events.c.recorded_at > cursor_at,
                                and_(m.events.c.recorded_at == cursor_at, m.events.c.id > cursor_id)))
    rows = [dict(row) for row in conn.execute(query.order_by(m.events.c.recorded_at, m.events.c.id).limit(limit)).mappings()]
    selected, chars = [], 0
    for row in rows:
        text = row["content"].get("text", "") if isinstance(row["content"], dict) else ""
        cost = min(len(text), MAX_EVENT_CHARS)
        if selected and chars + cost > MAX_BATCH_CHARS:
            break
        selected.append(row)
        chars += cost
    return selected


def _member_result_payload(job, summary, rows, candidates_count, usage):
    return {**(job.get("payload") or {}), "usage": usage,
            "result": {"stored": summary["stored"], "reinforced": summary["reinforced"],
                       "quarantined": summary["quarantined"], "ignored": summary["ignored"],
                       "events": len(rows), "candidates": candidates_count,
                       "pool_kind": "member"}}


async def run_member_extraction(db, extractor, worker, job, batch_events=8, settings=None):
    """Handler for one claimed ``memory.extract.member`` outbox row."""
    scope = member_scope_from_payload(job.get("payload") or {})
    binding_id = scope.room_id
    job_id, worker_id = job["id"], job["worker_id"]
    if extractor is None:
        raise AppError("MEMORY_EXTRACTOR_UNCONFIGURED", "记忆提炼模型尚未配置", 503)
    with db.engine.begin() as conn:
        worker.owned(conn, job_id, worker_id)
        lock(conn, scope.companion_id)
        companion = authorize(conn, scope, scope.companion_id)
        binding = conn.execute(select(m.channel_bindings).where(
            m.channel_bindings.c.id == binding_id,
            m.channel_bindings.c.account_id == scope.account_id,
            m.channel_bindings.c.companion_id == scope.companion_id,
            m.channel_bindings.c.conversation_id == scope.conversation_id).with_for_update()).mappings().first()
        if not binding:
            payload = {**(job.get("payload") or {}), "result": {"status": "noop", "reason": "binding_missing"}}
            conn.execute(m.outbox.update().where(m.outbox.c.id == job_id).values(payload=payload))
            return {"status": "noop", "reason": "binding_missing"}
        rows = _load_member_batch(conn, binding, batch_events)
        expected_revision = companion["memory_revision"]
        cursor_at, cursor_id = binding["member_extract_cursor_at"], binding["member_extract_cursor_id"]
    if not rows:
        with db.engine.begin() as conn:
            worker.owned(conn, job_id, worker_id)
            payload = {**(job.get("payload") or {}), "result": {"status": "noop", "events": 0, "pool_kind": "member"}}
            conn.execute(m.outbox.update().where(m.outbox.c.id == job_id).values(payload=payload))
        return {"status": "noop", "stored": 0, "reinforced": 0, "quarantined": 0}
    batch = [_member_batch_row(row) for row in rows]
    offered = [_member_model_event(row) for row in rows]
    from .budget import budgeted_extract
    candidates, usage = await budgeted_extract(db, settings, scope, job, extractor, offered)
    with db.engine.begin() as conn:
        worker.owned(conn, job_id, worker_id)
        lock(conn, scope.companion_id)
        companion = authorize(conn, scope, scope.companion_id)
        binding = conn.execute(select(m.channel_bindings).where(
            m.channel_bindings.c.id == binding_id,
            m.channel_bindings.c.conversation_id == scope.conversation_id).with_for_update()).mappings().one()
        if binding["member_extract_cursor_at"] != cursor_at or binding["member_extract_cursor_id"] != cursor_id:
            raise AppError("EXTRACTION_OUTPACED", "该群成员批次已被其他任务推进", 409)
        if companion["memory_revision"] != expected_revision:
            raise AppError("STALE_CONTEXT", "提炼任务的记忆版本已失效", 409)
        summary = MemberMemoryWriter(db, retention_days=settings.memory_retention_days if settings else None).commit_candidates(
            conn, scope, binding, candidates, batch, expected_revision)
        last = rows[-1]
        conn.execute(m.channel_bindings.update().where(m.channel_bindings.c.id == binding_id).values(
            member_extract_cursor_at=last["recorded_at"], member_extract_cursor_id=last["id"]))
        payload = _member_result_payload(job, summary, rows, len(candidates), usage)
        conn.execute(m.outbox.update().where(m.outbox.c.id == job_id).values(payload=payload))
        next_binding = {**dict(binding), "member_extract_cursor_at": last["recorded_at"],
                        "member_extract_cursor_id": last["id"]}
        if _load_member_batch(conn, next_binding, 1):
            pending = conn.scalar(select(m.outbox.c.id).where(
                m.outbox.c.event_type == MEMBER_EXTRACT, m.outbox.c.aggregate_id == binding_id,
                m.outbox.c.state == "pending").limit(1))
            if pending is None:
                conn.execute(insert(m.outbox).values(
                    event_type=MEMBER_EXTRACT, aggregate_id=binding_id,
                    payload={**(job.get("payload") or {}), "continuation": True},
                    dedupe_key=f"memory-member-continue:{binding_id}:{last['id']}:{expected_revision}",
                    available_at=m.now()).on_conflict_do_nothing())
    logger.info("member memory extracted room=%s events=%s candidates=%s stored=%s reinforced=%s quarantined=%s ignored=%s calls=%s",
                binding_id, len(rows), len(candidates), summary["stored"], summary["reinforced"],
                summary["quarantined"], summary["ignored"], usage.get("calls"))
    return {"status": "committed", "events": len(rows), "candidates": len(candidates), **summary,
            "usage": usage, "pipeline_version": PIPELINE_VERSION}


def scope_from_payload(payload):
    try:
        return Scope(
            account_id=UUID(str(payload["account_id"])),
            user_id=UUID(str(payload["user_id"])),
            companion_id=UUID(str(payload["companion_id"])),
            conversation_id=UUID(str(payload["conversation_id"])),
        )
    except (KeyError, ValueError, TypeError):
        raise AppError("INVALID_JOB", "记忆提炼任务缺少有效范围", 422) from None


def _batch_row(row):
    content = row["content"] if isinstance(row["content"], dict) else {}
    return {
        "id": row["id"],
        "content_sha256": row["content_sha256"],
        "role": row["role"],
        "kind": row["kind"],
        "recorded_at": row["recorded_at"],
        "state": content.get("state"),
    }


def _model_event(row):
    text = row["content"].get("text", "") if isinstance(row["content"], dict) else ""
    return {
        "event_id": str(row["id"]),
        "role": row["role"],
        "kind": row["kind"],
        "occurred_at": row["recorded_at"].isoformat() if row["recorded_at"] else None,
        "state": row["content"].get("state") if isinstance(row["content"], dict) else None,
        "text": text[:MAX_EVENT_CHARS],
    }


def _load_batch(conn, scope, companion, limit, exclude_id=None):
    query = select(m.events).where(
        m.events.c.account_id == scope.account_id,
        m.events.c.companion_id == scope.companion_id,
        m.events.c.visibility == "visible",
        source_owned_clause(scope),
        owner_authored_clause(),
        or_(
            and_(m.events.c.role == "user", m.events.c.kind == "user_input"),
            and_(m.events.c.role == "assistant", m.events.c.kind == "assistant_output"),
            and_(m.events.c.role == "system", m.events.c.kind == "vision_observation"),
        ),
    )
    cursor_at, cursor_id = companion["memory_extract_cursor_at"], companion["memory_extract_cursor_id"]
    if cursor_at is not None and cursor_id is not None:
        query = query.where(or_(m.events.c.recorded_at > cursor_at, and_(m.events.c.recorded_at == cursor_at, m.events.c.id > cursor_id)))
    if exclude_id is not None:
        query = query.where(m.events.c.id != exclude_id)
    rows = [dict(row) for row in conn.execute(query.order_by(m.events.c.recorded_at, m.events.c.id).limit(limit)).mappings()]
    selected, chars = [], 0
    for row in rows:
        text = row["content"].get("text", "") if isinstance(row["content"], dict) else ""
        cost = min(len(text), MAX_EVENT_CHARS)
        if selected and chars + cost > MAX_BATCH_CHARS:
            break
        selected.append(row)
        chars += cost
    return selected


def _load_models(conn, scope, limit):
    """Read the cursor and batch in one short transaction."""
    companion = conn.execute(select(m.companions).where(m.companions.c.id == scope.companion_id).with_for_update()).mappings().one()
    if companion["account_id"] != scope.account_id:
        raise AppError("FORBIDDEN", "无权访问角色", 403)
    rows = _load_batch(conn, scope, companion, limit)
    return companion, rows


async def run_extraction(db, extractor, worker, job, batch_events=8, settings=None):
    """Handler for one claimed ``memory.extract`` outbox row."""
    scope = scope_from_payload(job.get("payload") or {})
    job_id, worker_id = job["id"], job["worker_id"]
    if extractor is None:
        raise AppError("MEMORY_EXTRACTOR_UNCONFIGURED", "记忆提炼模型尚未配置", 503)
    with db.engine.begin() as conn:
        worker.owned(conn, job_id, worker_id)
        lock(conn, scope.companion_id)
        authorize(conn, scope, scope.companion_id)
        companion, rows = _load_models(conn, scope, batch_events)
        expected_revision = companion["memory_revision"]
        cursor_at, cursor_id = companion["memory_extract_cursor_at"], companion["memory_extract_cursor_id"]
    if not rows:
        with db.engine.begin() as conn:
            worker.owned(conn, job_id, worker_id)
            payload = {**(job.get("payload") or {}), "result": {"status": "noop", "events": 0}}
            conn.execute(m.outbox.update().where(m.outbox.c.id == job_id).values(payload=payload))
        return {"status": "noop", "stored": 0, "reinforced": 0, "quarantined": 0}
    batch = [_batch_row(row) for row in rows]
    offered = [_model_event(row) for row in rows]
    from .budget import budgeted_extract
    candidates, usage = await budgeted_extract(db, settings, scope, job, extractor, offered)
    with db.engine.begin() as conn:
        worker.owned(conn, job_id, worker_id)
        lock(conn, scope.companion_id)
        companion = authorize(conn, scope, scope.companion_id)
        if companion["memory_extract_cursor_at"] != cursor_at or companion["memory_extract_cursor_id"] != cursor_id:
            raise AppError("EXTRACTION_OUTPACED", "该批次已被其他任务推进", 409)
        if companion["memory_revision"] != expected_revision:
            raise AppError("STALE_CONTEXT", "提炼任务的记忆版本已失效", 409)
        summary = MemoryWriter(db, retention_days=settings.memory_retention_days if settings else None).commit_candidates(conn, scope, candidates, batch, expected_revision)
        last = rows[-1]
        conn.execute(m.companions.update().where(m.companions.c.id == scope.companion_id).values(memory_extract_cursor_at=last["recorded_at"], memory_extract_cursor_id=last["id"]))
        result = {"status": "committed", "events": len(rows), "candidates": len(candidates), **summary, "usage": usage, "pipeline_version": PIPELINE_VERSION}
        payload = {**(job.get("payload") or {}), "usage": usage, "result": {"stored": summary["stored"], "reinforced": summary["reinforced"], "quarantined": summary["quarantined"], "ignored": summary["ignored"], "events": len(rows)}}
        conn.execute(m.outbox.update().where(m.outbox.c.id == job_id).values(payload=payload))
        # One coalesced job can have more than one bounded batch to consume. Leave a
        # durable continuation so old backlog does not require another chat message.
        next_cursor={**companion,'memory_extract_cursor_at':last['recorded_at'],'memory_extract_cursor_id':last['id']}
        if _load_batch(conn,scope,next_cursor,1):
            pending = conn.scalar(select(m.outbox.c.id).where(m.outbox.c.event_type == MEMORY_EXTRACT,
                m.outbox.c.aggregate_id == scope.companion_id, m.outbox.c.state == 'pending').limit(1))
            if pending is None:
                conn.execute(insert(m.outbox).values(event_type=MEMORY_EXTRACT,
                    aggregate_id=scope.companion_id,
                    payload={**(job.get('payload') or {}), 'continuation':True},
                    dedupe_key=f'memory-continue:{scope.companion_id}:{last["id"]}:{expected_revision}')
                    .on_conflict_do_nothing())
    logger.info("memory extracted companion=%s events=%s candidates=%s stored=%s reinforced=%s quarantined=%s ignored=%s calls=%s", scope.companion_id, len(rows), len(candidates), summary["stored"], summary["reinforced"], summary["quarantined"], summary["ignored"], usage.get("calls"))
    return result
