from sqlalchemy import select

from nagi.archive import models as m
from nagi.archive.repository import digest
from nagi.memory import models as mm
from nagi.memory.extraction import ExtractedCandidate, MemoryWriter


def test_known_predicate_alias_reinforces_historical_spelling_and_preserves_suppression(db, seeded_scope):
    text = '我喜欢咖啡'
    with db.engine.begin() as conn:
        event = conn.execute(m.events.insert().values(account_id=seeded_scope.account_id,
            companion_id=seeded_scope.companion_id, conversation_id=seeded_scope.conversation_id,
            role='user', kind='message', content={'text': text},
            content_sha256=digest({'text': text})).returning(m.events)).mappings().one()
        old = conn.execute(mm.memory_items.insert().values(account_id=seeded_scope.account_id,
            companion_id=seeded_scope.companion_id, user_id=seeded_scope.user_id,
            kind='preference', content='原有的不同措辞', epistemic_type='user_asserted',
            subject_key='user:self', predicate='favorite_drink', value='咖啡').returning(mm.memory_items.c.id)).scalar_one()
    candidate = ExtractedCandidate(kind='preference', content='用户偏爱咖啡',
        subject_key='user:self', predicate='喜爱的饮料', value='咖啡', epistemic_type='user_asserted',
        source_refs=[{'event_id': event['id'], 'quote': text}])
    writer = MemoryWriter(db)
    with db.engine.begin() as conn:
        revision = conn.scalar(select(m.companions.c.memory_revision).where(m.companions.c.id == seeded_scope.companion_id))
        result = writer.commit_candidates(conn, seeded_scope, [candidate], [event], revision)
    assert result['reinforced'] == 1 and result['stored'] == 0
    assert db.count('memory_items') == 1
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == old).values(status='hidden'))
        result = writer.commit_candidates(conn, seeded_scope, [candidate], [event], result['memory_revision'])
    assert result['ignored'] == 1 and db.count('memory_items') == 1
