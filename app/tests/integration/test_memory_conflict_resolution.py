from sqlalchemy import select
from agi_wind.archive import models as m
from agi_wind.memory import models as mm
from agi_wind.archive.repository import digest


async def test_confirming_disputed_fact_closes_linked_old_truth_and_keeps_pin(db, actor, seeded_scope):
    from agi_wind.memory.corrections import CorrectionService
    scope = seeded_scope
    with db.engine.begin() as conn:
        eid = conn.execute(m.events.insert().values(account_id=scope.account_id,companion_id=scope.companion_id,
            conversation_id=scope.conversation_id,role='user',kind='user_input',content={'text':'我住杭州'},
            content_sha256=digest({'text':'我住杭州'})).returning(m.events.c.id)).scalar_one()
        ids = []
        for value, status, pinned in [('北京','active',True),('杭州','disputed',False)]:
            mid = conn.execute(mm.memory_items.insert().values(account_id=scope.account_id,
                companion_id=scope.companion_id,user_id=scope.user_id,kind='fact',content='用户居住'+value,
                epistemic_type='user_asserted',status=status,is_pinned=pinned,
                subject_key='user:self',predicate='city',value=value).returning(mm.memory_items.c.id)).scalar_one()
            conn.execute(mm.source_refs.insert().values(memory_id=mid,event_id=eid,json_pointer='/text'))
            ids.append(mid)
        conn.execute(mm.memory_relations.insert().values(from_memory_id=ids[1],to_memory_id=ids[0],relation_type='contradicts'))
    result = await CorrectionService(db).apply_correction(actor,ids[1],'用户现在居住杭州',eid)
    with db.engine.connect() as conn:
        old = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id.in_(ids))).mappings().all()
        assert all(row['status'] == 'superseded' and row['valid_to'] for row in old)
        current = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id == result['memory_id'])).mappings().one()
        assert current['status'] == 'active' and current['is_pinned'] is True and current['expires_at'] is None
        assert conn.execute(select(mm.source_refs.c.event_id).where(mm.source_refs.c.memory_id == result['memory_id'])).scalar_one() != eid
