"""Independent memory worker: no HTTP server, no generation restart/cancellation side effects."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import time
from sqlalchemy import select
from .consolidation import MEMORY_CONSOLIDATE, enqueue_consolidation, run_consolidation, SUMMARY_ZONE
from .jobs import MEMBER_EXTRACT, MEMORY_EXTRACT, run_extraction, run_member_extraction
from .lifecycle import maintain
from ..archive import models as m
from ..core.settings import apply_persisted_settings
from ..core.types import Scope
from ..jobs.worker import Worker

logger = logging.getLogger('agi_wind.memory.worker')


def worker_status(settings):
    if settings.memory_worker_mode == 'gateway':
        return {'mode':'gateway','status':'gateway'}
    path = Path(settings.data_dir) / 'memory-worker.json'
    try:
        if path.stat().st_size > 16384:
            raise ValueError('invalid heartbeat')
        state = json.loads(path.read_text(encoding='utf8'))
        age = time.time() - float(state['updated_at'])
        return {'mode':'separate','status':state.get('status','unknown') if 0 <= age < 120 else 'stale',
                'heartbeat_age_seconds':round(max(0, age),1), 'processed':state.get('processed',0)}
    except (OSError, ValueError, KeyError, TypeError):
        return {'mode':'separate','status':'not_running'}


def _scopes(db, actor):
    with db.engine.connect() as conn:
        rows = conn.execute(select(m.conversations).where(m.conversations.c.account_id == actor.account_id,
            m.conversations.c.user_id == actor.user_id, m.conversations.c.state == 'active')
            .order_by(m.conversations.c.created_at.desc())).mappings()
        seen, scopes = set(), []
        for row in rows:
            if row['companion_id'] not in seen:
                scopes.append(Scope(**actor.model_dump(), companion_id=row['companion_id'], conversation_id=row['id']))
                seen.add(row['companion_id'])
        return scopes


async def background_tick(db, settings, actor, state):
    scopes = _scopes(db, actor)
    try:
        from .retrieval import MemoryService
        from .semantic import index_semantic
        for scope in scopes:
            await MemoryService(db).index_events(scope,limit=64)
            if settings.memory_semantic_enabled:
                await index_semantic(db,scope,limit=64)
    except Exception as error:
        logger.warning('memory indexing deferred (%s)',type(error).__name__)
    clock = time.monotonic()
    if settings.memory_maintenance_enabled and (state.get('maintenance') is None or clock - state['maintenance'] >= settings.memory_maintenance_hours * 3600):
        await asyncio.to_thread(maintain,db,retention_days=settings.memory_retention_days)
        state['maintenance'] = clock
    if settings.memory_consolidation_enabled and (state.get('summary') is None or clock - state['summary'] >= 3600):
        reference = datetime.now(SUMMARY_ZONE)
        with db.engine.begin() as conn:
            for scope in scopes:
                enqueue_consolidation(conn,scope,'daily',reference - timedelta(days=1))
                enqueue_consolidation(conn,scope,'weekly',reference - timedelta(days=reference.weekday()+1))
        state['summary'] = clock


async def run_worker(root, once=False):
    os.environ['AGI_CONFIG'] = str(Path(root) / 'config/app.local.yaml')
    from ..api.app import from_env
    app = from_env()  # Do not enter Gateway lifespan: it owns live generations.
    db, service, settings, actor = app.state.db, app.state.service, app.state.settings, app.state.actor
    worker = Worker(db)
    worker_id = 'memory-' + str(os.getpid())
    state_path = Path(settings.data_dir) / 'memory-worker.json'
    processed, stamp, background_state = 0, None, {}

    def heartbeat(status):
        temporary = state_path.with_suffix('.' + str(os.getpid()) + '.tmp')
        payload = {'pid':os.getpid(),'updated_at':time.time(),'status':status,'processed':processed}
        try:
            temporary.write_text(json.dumps(payload),encoding='utf8')
            temporary.replace(state_path)
        except OSError:
            pass

    try:
        while True:
            path = Path(settings.data_dir) / 'settings.json'
            current_stamp = path.stat().st_mtime_ns if path.exists() else 0
            if current_stamp != stamp:
                loaded = apply_persisted_settings(settings)
                for name in type(settings).model_fields:
                    setattr(settings,name,getattr(loaded,name))
                app.state.apply_settings()
                stamp = current_stamp
            if settings.memory_worker_mode != 'separate':
                heartbeat('standby')
                if once: return
                await asyncio.sleep(2)
                continue
            heartbeat('running')
            await background_tick(db,settings,actor,background_state)
            kinds = []
            if settings.memory_extraction_enabled:
                kinds.append(MEMORY_EXTRACT)
                kinds.append(MEMBER_EXTRACT)
            if settings.memory_consolidation_enabled: kinds.append(MEMORY_CONSOLIDATE)
            jobs = worker.claim(worker_id,batch_size=1,event_types=kinds)
            for job in jobs:
                heartbeat('busy')
                async def handle(item):
                    if item['event_type'] == MEMORY_EXTRACT:
                        return await run_extraction(db,getattr(service,'memory_extractor',None),worker,item,
                            batch_events=settings.memory_extract_batch_events,settings=settings)
                    if item['event_type'] == MEMBER_EXTRACT:
                        return await run_member_extraction(db,getattr(service,'memory_extractor',None),worker,item,
                            batch_events=settings.memory_extract_batch_events,settings=settings)
                    return await run_consolidation(db,getattr(service,'memory_summary_extractor',None),worker,item,settings=settings)
                await worker.run_job(job['id'],worker_id,handle)
                processed += 1
            heartbeat('running')
            if once: return
            await asyncio.sleep(1 if jobs else 3)
    finally:
        heartbeat('stopped')
        registry = getattr(service,'registry',None)
        if registry: await registry.aclose()
        clients = {id(provider):provider for provider in (service.provider,
            getattr(getattr(service,'memory_extractor',None),'provider',None),
            getattr(getattr(service,'memory_summary_extractor',None),'provider',None)) if provider is not None}
        for provider in clients.values():
            close = getattr(provider,'aclose',None)
            if close: await close()
        db.engine.dispose()
