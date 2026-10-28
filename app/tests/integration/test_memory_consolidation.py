from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select
from nagi.archive import models as m
from nagi.memory import models as mm
from nagi.archive.repository import digest


async def test_summary_is_derived_source_backed_and_idempotent(db, seeded_scope):
    from nagi.memory.consolidation import enqueue_consolidation, run_consolidation
    from nagi.memory.extraction import ExtractedCandidate, ExtractedSourceRef
    from nagi.jobs.worker import Worker
    scope = seeded_scope
    text = '今天完成了合成测试计划'
    with db.engine.begin() as conn:
        event_id = conn.execute(m.events.insert().values(account_id=scope.account_id,
            companion_id=scope.companion_id, conversation_id=scope.conversation_id,
            role='user',kind='user_input',content={'text':text}, content_sha256=digest({'text':text})).returning(m.events.c.id)).scalar_one()
        first = enqueue_consolidation(conn, scope, 'daily')
        assert first is not None
        assert enqueue_consolidation(conn, scope, 'daily') is None
    class Fake:
        async def extract(self, scope, events):
            return [ExtractedCandidate(kind='fact',content='完成了合成测试计划',epistemic_type='user_asserted',
                source_refs=[ExtractedSourceRef(event_id=event_id,quote=text)])], {'calls':1}
    worker = Worker(db)
    job = worker.claim('summary',event_types=['memory.consolidate'])[0]
    result = await run_consolidation(db, Fake(), worker, job)
    assert result['stored'] == 1
    item = db.all('memory_items')[0]
    assert item['epistemic_type'] == 'inferred' and item['kind'] == 'episode'
    assert item['summary_level'] == 'daily'
    assert db.all('source_refs')[0]['event_id'] == event_id


async def test_hidden_fact_sources_are_not_offered_for_new_summaries(db, seeded_scope):
    from nagi.memory.consolidation import enqueue_consolidation
    scope = seeded_scope
    with db.engine.begin() as conn:
        eid = conn.execute(m.events.insert().values(account_id=scope.account_id,companion_id=scope.companion_id,
            conversation_id=scope.conversation_id,role='user',kind='user_input',content={'text':'不应复活的合成记忆'},
            content_sha256=digest({'text':'不应复活的合成记忆'})).returning(m.events.c.id)).scalar_one()
        mid = conn.execute(mm.memory_items.insert().values(account_id=scope.account_id,companion_id=scope.companion_id,
            user_id=scope.user_id,kind='fact',content='合成隐藏记忆',epistemic_type='user_asserted',status='hidden').returning(mm.memory_items.c.id)).scalar_one()
        conn.execute(mm.source_refs.insert().values(memory_id=mid,event_id=eid,json_pointer='/text'))
        assert enqueue_consolidation(conn, scope, 'daily') is None
