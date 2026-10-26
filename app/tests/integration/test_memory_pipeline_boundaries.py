from uuid import uuid4
from sqlalchemy import select
from agi_wind.archive import models as m
from agi_wind.archive.repository import digest


def add_event(conn, scope, text, conversation_id=None):
    return dict(conn.execute(m.events.insert().values(account_id=scope.account_id,
        companion_id=scope.companion_id,conversation_id=conversation_id or scope.conversation_id,
        role='user',kind='user_input',content={'text':text},content_sha256=digest({'text':text}))
        .returning(m.events)).mappings().one())


async def test_character_limited_batch_leaves_a_durable_continuation(db, seeded_scope):
    from agi_wind.memory.jobs import enqueue_extraction,run_extraction
    from agi_wind.jobs.worker import Worker
    with db.engine.begin() as conn:
        for _ in range(8): add_event(conn,seeded_scope,'合'*4000)
        enqueue_extraction(conn,seeded_scope,uuid4())
    class Empty:
        async def extract(self,scope,events): return [],{'calls':1}
    worker=Worker(db)
    job=worker.claim('memory',event_types=['memory.extract'])[0]
    result=await run_extraction(db,Empty(),worker,job,batch_events=8)
    worker.finish(job['id'],'memory')
    assert result['events'] == 4
    assert len(worker.claim('memory',event_types=['memory.extract'])) == 1


async def test_other_user_sources_are_not_offered_or_accepted(db, seeded_scope):
    from agi_wind.memory.jobs import enqueue_extraction,run_extraction
    from agi_wind.memory.consolidation import enqueue_consolidation
    from agi_wind.memory.extraction import ExtractedCandidate,ExtractedSourceRef,MemoryWriter
    from agi_wind.jobs.worker import Worker
    scope=seeded_scope
    with db.engine.begin() as conn:
        other=uuid4();conn.execute(m.users.insert().values(id=other,account_id=scope.account_id))
        conv=conn.execute(m.conversations.insert().values(account_id=scope.account_id,companion_id=scope.companion_id,user_id=other,state='archived').returning(m.conversations.c.id)).scalar_one()
        foreign=add_event(conn,scope,'合成他人私有来源',conv)
        own=add_event(conn,scope,'合成当前用户来源')
        enqueue_extraction(conn,scope,uuid4())
    class Capture:
        offered=[]
        async def extract(self,scope,events):
            self.offered=events;return [],{'calls':1}
    worker=Worker(db);job=worker.claim('memory',event_types=['memory.extract'])[0]
    extractor=Capture();await run_extraction(db,extractor,worker,job)
    assert [item['event_id'] for item in extractor.offered] == [str(own['id'])]
    with db.engine.begin() as conn:
        revision=conn.scalar(select(m.companions.c.memory_revision).where(m.companions.c.id==scope.companion_id))
        candidate=ExtractedCandidate(kind='fact',content='合成他人私有来源',epistemic_type='user_asserted',source_refs=[ExtractedSourceRef(event_id=foreign['id'],quote='合成他人私有来源')])
        result=MemoryWriter(db).commit_candidates(conn,scope,[candidate],[foreign],revision)
        assert result['stored']==0 and result['quarantined']==1
