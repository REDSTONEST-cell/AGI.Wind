"""Synthetic projection only; no production text or encoder calls."""
from uuid import UUID, uuid4

from nagi.archive import models as m
from nagi.archive.repository import digest
from nagi.memory import models as mm
from nagi.memory import semantic


def test_full_projection_above_old_cap_and_source_delete_recheck(db, seeded_scope):
    count = 10001
    text = 'x' * (count - 1) + 'y'
    x = semantic.pack_vector([1.] + [0.] * 511)
    y = semantic.pack_vector([0., 1.] + [0.] * 510)
    fingerprint = 'synthetic-scale'
    with db.engine.begin() as conn:
        eid = conn.execute(m.events.insert().values(account_id=seeded_scope.account_id,
            companion_id=seeded_scope.companion_id, conversation_id=seeded_scope.conversation_id,
            role='user', kind='message', content={'text': text},
            content_sha256=digest({'text': text})).returning(m.events.c.id)).scalar_one()
        sid = conn.execute(mm.embedding_spaces.insert().values(fingerprint=fingerprint,
            config={}, checkpoint={}).returning(mm.embedding_spaces.c.id)).scalar_one()
        chunks = [dict(id=UUID(int=i+1), account_id=seeded_scope.account_id,
            companion_id=seeded_scope.companion_id, source_id=eid, content=char,
            content_hash=digest(char), source_hash=digest({'text': text}),
            start_offset=i, end_offset=i+1, token_count=1, search_terms=char, revision=1)
            for i, char in enumerate(text)]
        conn.execute(mm.retrieval_chunks.insert(), chunks)
        conn.execute(mm.chunk_vectors.insert(), [dict(id=uuid4(), chunk_id=row['id'], space_id=sid,
            content_hash=row['content_hash'], vector=y if row['content']=='y' else x, revision=1)
            for row in chunks])
    _, ranked = semantic._ann_candidates(db, seeded_scope, fingerprint, [0., 1.] + [0.] * 510)
    assert ranked[0][0] == chunks[-1]['id']
    indexes = next(iter(db._memory_ann_cache.values()))[1]
    assert indexes[1].size == count
    with db.engine.begin() as conn:
        conn.execute(m.events.update().where(m.events.c.id == eid).values(visibility='hidden'))
    assert semantic._ann_candidates(db, seeded_scope, fingerprint, [0., 1.] + [0.] * 510) == ([], [])
