"""Bounded daily/weekly derived summaries, always committed through the provenance gate."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import UUID
from zoneinfo import ZoneInfo
from sqlalchemy import select, or_, and_, exists, func
from sqlalchemy.dialects.postgresql import insert
from . import models as mm
from .budget import budgeted_extract
from .extraction import MemoryWriter, _SYSTEM_PROMPT, source_owned_clause
from .jobs import scope_from_payload, _batch_row, _model_event, MAX_BATCH_CHARS
from ..archive import models as m
from ..archive.repository import lock
from ..identity.service import authorize
from ..core.errors import AppError

MEMORY_CONSOLIDATE = 'memory.consolidate'
SUMMARY_ZONE = ZoneInfo('Asia/Shanghai')
SUMMARY_PROMPT = _SYSTEM_PROMPT + (
    '\n本次为来源片段的阶段摘要，不是人格或事实更新。压缩日常经历、进展和待办为1到2条episode，'
    'epistemic_type必须为inferred。每条仍须引用支持其内容的事件原文片段；最多500字、8个来源。'
    '无法由来源支持的细节不要写；不要把故事、猜测或助手承诺变成用户事实。'
    '只总结提供的片段，不声称涵盖一整天/周的全部消息。'
)


def _range(level, reference=None):
    if level not in ('daily', 'weekly'):
        raise AppError('INVALID_SUMMARY_LEVEL', '摘要类型只能是 daily 或 weekly', 422)
    date = (reference or datetime.now(SUMMARY_ZONE)).astimezone(SUMMARY_ZONE).date()
    if level == 'weekly':
        date -= timedelta(days=date.weekday())
    start = datetime.combine(date, datetime.min.time(), SUMMARY_ZONE)
    return start.astimezone(timezone.utc), (start + timedelta(days=1 if level == 'daily' else 7)).astimezone(timezone.utc)


def _rows(conn, scope, start, end):
    blocked = exists(select(mm.source_refs.c.id).join(mm.memory_items).where(
        mm.source_refs.c.event_id == m.events.c.id, mm.memory_items.c.account_id == scope.account_id,
        mm.memory_items.c.companion_id == scope.companion_id,
        or_(mm.memory_items.c.status != 'active', mm.memory_items.c.valid_to.is_not(None),
            and_(mm.memory_items.c.is_pinned.is_(False), mm.memory_items.c.expires_at <= m.now()))))
    query = select(m.events).where(m.events.c.account_id == scope.account_id,
        m.events.c.companion_id == scope.companion_id, m.events.c.visibility == 'visible',
        source_owned_clause(scope),
        m.events.c.recorded_at >= start, m.events.c.recorded_at < end, ~blocked,
        func.coalesce(m.events.c.content['text'].astext, '') != '',
        or_(and_(m.events.c.role == 'user', m.events.c.kind == 'user_input'),
            and_(m.events.c.role == 'assistant', m.events.c.kind == 'assistant_output',
                 m.events.c.content['state'].astext == 'complete')))
    # A summary explicitly covers a bounded source window; authoritative events remain
    # intact and ordinary extraction consumes the complete cursor backlog independently.
    rows = list(conn.execute(query.order_by(m.events.c.recorded_at.desc(), m.events.c.id.desc()).limit(32)).mappings())
    selected, chars = [], 0
    for row in rows:
        cost = min(4000, len(row['content'].get('text', '')))
        if selected and chars + cost > MAX_BATCH_CHARS:
            break
        selected.append(dict(row))
        chars += cost
    return list(reversed(selected))


def enqueue_consolidation(conn, scope, level='daily', reference=None):
    lock(conn, scope.companion_id)
    authorize(conn, scope, scope.companion_id)
    start, end = _range(level, reference)
    rows = _rows(conn, scope, start, end)
    if not rows:
        return None
    sources = {str(row['id']): row['content_sha256'] for row in rows}
    fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    return conn.execute(insert(m.outbox).values(event_type=MEMORY_CONSOLIDATE,
        aggregate_id=scope.companion_id,
        payload={**scope.model_dump(mode='json'), 'level':level, 'start':start.isoformat(),
                 'end':end.isoformat(), 'sources':sources},
        dedupe_key=f'memory-summary:{scope.companion_id}:{level}:{start.date()}:{fingerprint}')
        .on_conflict_do_nothing().returning(m.outbox.c.id)).scalar()


async def run_consolidation(db, extractor, worker, job, settings=None):
    if extractor is None:
        raise AppError('MEMORY_EXTRACTOR_UNCONFIGURED', '摘要模型尚未配置', 503)
    scope = scope_from_payload(job['payload'])
    level = job['payload']['level']
    start, end = datetime.fromisoformat(job['payload']['start']), datetime.fromisoformat(job['payload']['end'])
    sources = job['payload']['sources']
    with db.engine.begin() as conn:
        worker.owned(conn, job['id'], job['worker_id'])
        lock(conn, scope.companion_id)
        companion = authorize(conn, scope, scope.companion_id)
        rows = [row for row in _rows(conn, scope, start, end)
                if sources.get(str(row['id'])) == row['content_sha256']]
        revision = companion['memory_revision']
    if not rows:
        return {'status':'noop', 'stored':0}
    candidates, usage = await budgeted_extract(db, settings, scope, job, extractor, [_model_event(row) for row in rows])
    # Summary output is a derived projection regardless of the vendor's chosen labels.
    candidates = [candidate.model_copy(update={'kind':'episode','epistemic_type':'inferred',
        'subject_key':'summary:' + level + ':' + start.date().isoformat(), 'predicate':'summary', 'value':''})
        for candidate in candidates[:2]]
    with db.engine.begin() as conn:
        worker.owned(conn, job['id'], job['worker_id'])
        lock(conn, scope.companion_id)
        result = MemoryWriter(db, retention_days=settings.memory_retention_days if settings else None).commit_candidates(
            conn, scope, candidates, [_batch_row(row) for row in rows], revision, summary_level=level)
        conn.execute(m.outbox.update().where(m.outbox.c.id == job['id']).values(payload={
            **job['payload'], 'usage':usage, 'result':{**result,'source_count':len(rows),'bounded_window':True}}))
    return result
