"""10k source-backed synthetic facts in a disposable PostgreSQL schema; fake encoder."""
import hashlib
import time
from uuid import uuid4
from sqlalchemy import select
from test_memory_semantic import encoder
from agi_wind.archive import models as m
from agi_wind.archive.repository import digest
from agi_wind.memory import models as mm


async def test_ten_thousand_fact_recall_with_real_source_validation(db, seeded_scope, encoder):
    from agi_wind.memory.semantic import pack_vector
    from agi_wind.memory.retrieval import MemoryService
    scope = seeded_scope
    vector_other = pack_vector([0.0,1.0] + [0.0]*510)
    vector_target = pack_vector([1.0] + [0.0]*511)
    events, memories, refs, vectors = [], [], [], []
    with db.engine.begin() as conn:
        sid = conn.execute(mm.embedding_spaces.insert().values(fingerprint=encoder.space.fingerprint,
            config=encoder.space.model_dump(),status='ready').returning(mm.embedding_spaces.c.id)).scalar_one()
        target = None
        for index in range(10000):
            mid, eid = uuid4(), uuid4()
            text = '晨间咖啡偏好' if index == 0 else '合成主题' + str(index) + '生活记录'
            if index == 0: target = mid
            events.append({'id':eid,'account_id':scope.account_id,'companion_id':scope.companion_id,
                'conversation_id':scope.conversation_id,'role':'user','kind':'user_input',
                'content':{'text':text},'content_sha256':digest({'text':text})})
            memories.append({'id':mid,'account_id':scope.account_id,'companion_id':scope.companion_id,
                'user_id':scope.user_id,'kind':'fact','content':text,'epistemic_type':'user_asserted'})
            refs.append({'memory_id':mid,'event_id':eid,'json_pointer':'/text'})
            vectors.append({'memory_id':mid,'space_id':sid,'content_hash':hashlib.sha256(text.encode()).hexdigest(),
                'vector':vector_target if index == 0 else vector_other,'revision':1})
        conn.execute(m.events.insert(),events)
        conn.execute(mm.memory_items.insert(),memories)
        conn.execute(mm.source_refs.insert(),refs)
        conn.execute(mm.memory_vectors.insert(),vectors)
    started = time.perf_counter()
    hits = await MemoryService(db).search(scope,'最爱的提神饮料')
    milliseconds = (time.perf_counter() - started) * 1000
    assert hits and hits[0].memory_id == target
    assert milliseconds < 30000, '10k recall exceeded a bounded local acceptance window'
    print(f'10k PostgreSQL + provenance + deterministic vector ranking: {milliseconds:.2f} ms (fake query encoder)')
