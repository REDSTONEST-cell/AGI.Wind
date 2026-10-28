from datetime import datetime, timezone
from uuid import UUID
from sqlalchemy import select
from nagi.memory import models as mm


async def test_structured_pinned_memory_survives_clone_without_derived_vectors(db, actor, seeded_scope, archive, chat_request_factory, tmp_path):
    from nagi.archive.exports import ExportService
    from nagi.archive.imports import ImportService
    from nagi.archive.deletions import DeletionLedger
    accepted = await archive.accept(actor,chat_request_factory(text='合成移植测试：我喜欢咖啡',conversation_id=seeded_scope.conversation_id))
    await archive.append_output(accepted.generation_id,'合成测试已记录',final_state='complete')
    user_event = next(row for row in db.all('events') if row['role'] == 'user')
    valid = datetime.now(timezone.utc).replace(microsecond=0)
    with db.engine.begin() as conn:
        mid = conn.execute(mm.memory_items.insert().values(account_id=actor.account_id,
            companion_id=seeded_scope.companion_id,user_id=actor.user_id,kind='preference',content='合成咖啡偏好',
            epistemic_type='user_asserted',subject_key='user:self',predicate='favorite_drink',value='咖啡',
            valid_from=valid,is_pinned=True).returning(mm.memory_items.c.id)).scalar_one()
        conn.execute(mm.source_refs.insert().values(memory_id=mid,event_id=user_event['id'],json_pointer='/text'))
        sid = conn.execute(mm.embedding_spaces.insert().values(fingerprint='synthetic-portable-space',config={}).returning(mm.embedding_spaces.c.id)).scalar_one()
        conn.execute(mm.memory_vectors.insert().values(memory_id=mid,space_id=sid,content_hash='synthetic',vector=b'1234',revision=1))
    package = await ExportService(db,tmp_path/'assets').export(actor,seeded_scope.companion_id,tmp_path/'portable.zip')
    ledger = DeletionLedger(tmp_path/'governance'); ledger.initialize()
    restored = await ImportService(db,tmp_path/'assets').restore(package,mode='clone',latest_ledger=ledger)
    companion = UUID(str(restored['companion_id']))
    rows = db.all('memory_items',companion_id=companion)
    assert len(rows) == 1
    item = rows[0]
    assert (item['subject_key'],item['predicate'],item['value']) == ('user:self','favorite_drink','咖啡')
    assert item['valid_from'] == valid and item['is_pinned'] is True
    assert item['expires_at'] is None
    assert db.all('source_refs',memory_id=item['id'])
    with db.engine.connect() as conn:
        assert conn.execute(select(mm.memory_vectors).where(mm.memory_vectors.c.memory_id == item['id'])).first() is None
