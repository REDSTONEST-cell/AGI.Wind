from datetime import datetime, timezone
from uuid import uuid4
import httpx
from sqlalchemy import select
from agi_wind.archive import models as m


def test_claim_filter_and_automatic_job_coalescing(db, seeded_scope):
    from agi_wind.memory.jobs import enqueue_extraction
    from agi_wind.jobs.worker import Worker
    with db.engine.begin() as conn:
        assert enqueue_extraction(conn,seeded_scope,uuid4()) is not None
        assert enqueue_extraction(conn,seeded_scope,uuid4()) is None
        conn.execute(m.outbox.insert().values(event_type='generation.requested',aggregate_id=uuid4(),payload={},dedupe_key='synthetic-generation'))
    worker = Worker(db)
    assert worker.claim('memory',event_types=['memory.extract']) == []
    with db.engine.begin() as conn:
        conn.execute(m.outbox.update().where(m.outbox.c.event_type=='memory.extract').values(available_at=m.now()))
    memory = worker.claim('memory',event_types=['memory.extract'])
    chat = worker.claim('gateway',event_types=['generation.requested'])
    assert len(memory) == len(chat) == 1
    assert memory[0]['event_type'] == 'memory.extract' and chat[0]['event_type'] == 'generation.requested'


async def test_exhausted_budget_defers_without_burning_retry_attempts(db, seeded_scope):
    from agi_wind.jobs.worker import Worker
    from agi_wind.core.errors import AppError
    from agi_wind.memory.jobs import enqueue_extraction
    with db.engine.begin() as conn:
        enqueue_extraction(conn,seeded_scope,uuid4())
        conn.execute(m.outbox.update().where(m.outbox.c.event_type=='memory.extract').values(available_at=m.now()))
    worker = Worker(db)
    job = worker.claim('memory',event_types=['memory.extract'])[0]
    async def handler(item):
        raise AppError('MEMORY_BUDGET_EXCEEDED','预算已用尽',429)
    result = await worker.run_job(job['id'],'memory',handler)
    assert result['state'] == 'deferred'
    saved = db.all('outbox',id=job['id'])[0]
    assert saved['state'] == 'pending' and saved['attempts'] == 0
    assert saved['available_at'].date() > datetime.now(timezone.utc).date()
    assert worker.claim('memory',event_types=['memory.extract']) == []


async def test_memory_settings_validate_atomically_and_show_worker_budget(db, actor, seeded_scope, tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.core.settings import Settings
    from agi_wind.delivery.service import GenerationService
    settings = Settings(data_dir=tmp_path,access_token='synthetic-memory-token')
    app = create_app(settings,db,actor,GenerationService(db,None,model=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
        headers={'Authorization':'Bearer synthetic-memory-token'}) as client:
        saved = await client.patch('/api/v1/memory/settings',json={'daily_budget_usd':0.02,
            'daily_token_limit':200000,'retention_days':{'preference':120},'worker_mode':'separate'})
        assert saved.status_code == 200
        body = saved.json()
        assert body['retention_days'] == {'preference':120}
        assert body['worker']['status'] == 'not_running'
        assert body['budget']['currency'] == 'USD' and body['budget']['cost_is_estimate']
        invalid = await client.patch('/api/v1/memory/settings',json={'retention_days':{'fictional':0},'daily_budget_usd':0.01})
        assert invalid.status_code == 422
        current = (await client.get('/api/v1/memory/settings')).json()
        assert current['daily_budget_usd'] == 0.02 and current['retention_days'] == {'preference':120}


async def test_pin_unpin_and_ttl_only_patches_reach_memory_policy(db, actor, seeded_scope, tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.core.settings import Settings
    from agi_wind.delivery.service import GenerationService
    from agi_wind.memory import models as mm
    with db.engine.begin() as conn:
        mid=conn.execute(mm.memory_items.insert().values(account_id=actor.account_id,
            companion_id=seeded_scope.companion_id,user_id=actor.user_id,kind='fact',content='合成固定验证',
            epistemic_type='user_asserted').returning(mm.memory_items.c.id)).scalar_one()
    app=create_app(Settings(data_dir=tmp_path,access_token='synthetic-memory-token'),db,actor,GenerationService(db,None,model=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'Authorization':'Bearer synthetic-memory-token'}) as client:
        path='/api/v1/memory/items/'+str(mid)
        pinned=await client.patch(path,json={'is_pinned':True})
        assert pinned.status_code==200
        assert db.all('memory_items',id=mid)[0]['expires_at'] is None
        unpinned=await client.patch(path,json={'is_pinned':False})
        assert unpinned.status_code==200 and db.all('memory_items',id=mid)[0]['expires_at'] is not None
        ttl=await client.patch(path,json={'ttl_days':7})
        assert ttl.status_code==200 and db.all('memory_items',id=mid)[0]['ttl_days']==7
