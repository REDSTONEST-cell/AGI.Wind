"""Durable, idempotent memory-extraction jobs built on the existing outbox/worker.

Flow per job:
1. short transaction A: verify the lease, read the companion cursor and a bounded event batch;
2. outside any transaction: call the extractor model (no locks held, no authoritative write);
3. short transaction B: verify the lease again, re-check cursor and memory_revision, re-check
   source visibility/content hashes, then store candidates + advance the cursor atomically.
"""
import logging
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from .extraction import MemoryWriter, source_owned_clause
from ..archive import models as m
from ..archive.repository import lock
from ..identity.service import authorize
from ..core.types import Scope
from ..core.errors import AppError

MEMORY_EXTRACT = "memory.extract"
PIPELINE_VERSION = 1
MAX_EVENT_CHARS = 4000
MAX_BATCH_CHARS = 16000
logger = logging.getLogger("agi_wind.memory")


def enqueue_extraction(conn, scope, generation_id):
    """Register one idempotent extraction job for a completed generation."""
    lock(conn, scope.companion_id)
    if conn.scalar(select(m.outbox.c.id).where(m.outbox.c.event_type == MEMORY_EXTRACT,
        m.outbox.c.aggregate_id == scope.companion_id, m.outbox.c.state == 'pending').limit(1)):
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
            },
            dedupe_key=f"memory-extract:{scope.companion_id}:{generation_id}",
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
