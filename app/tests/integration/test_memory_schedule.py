from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from nagi.archive import models as m
from nagi.memory.jobs import enqueue_extraction, enqueue_manual_extraction
from nagi.jobs.worker import Worker


def test_auto_extraction_waits_for_idle_and_eighth_turn_runs_now(db, seeded_scope):
    ids = [uuid4() for _ in range(8)]
    with db.engine.begin() as conn:
        enqueue_extraction(conn, seeded_scope, ids[0])
    assert Worker(db).claim('early', event_types=['memory.extract']) == []
    first = db.all('outbox', event_type='memory.extract')[0]
    assert first['available_at'] >= m.now() + timedelta(seconds=110)
    with db.engine.begin() as conn:
        for gid in ids[1:]:
            enqueue_extraction(conn, seeded_scope, gid)
    assert db.count('outbox') == 1
    assert len(Worker(db).claim('due', event_types=['memory.extract'])) == 1


def test_new_turn_resets_idle_but_duplicate_does_not(db, seeded_scope):
    gid = uuid4()
    with db.engine.begin() as conn:
        enqueue_extraction(conn, seeded_scope, gid)
        row = conn.execute(select(m.outbox)).mappings().one()
        old = m.now() + timedelta(seconds=20)
        conn.execute(m.outbox.update().where(m.outbox.c.id == row['id']).values(available_at=old))
        enqueue_extraction(conn, seeded_scope, gid)
        assert conn.scalar(select(m.outbox.c.available_at)) == old
        enqueue_extraction(conn, seeded_scope, uuid4())
        assert conn.scalar(select(m.outbox.c.available_at)) > old + timedelta(seconds=80)


def test_manual_and_budget_deferral_are_not_delayed_or_bypassed(db, seeded_scope):
    with db.engine.begin() as conn:
        enqueue_manual_extraction(conn, seeded_scope)
        before = conn.scalar(select(m.outbox.c.available_at))
        enqueue_extraction(conn, seeded_scope, uuid4())
        assert conn.scalar(select(m.outbox.c.available_at)) == before
        row = conn.execute(select(m.outbox)).mappings().one()
        tomorrow = m.now() + timedelta(days=1)
        conn.execute(m.outbox.update().where(m.outbox.c.id == row['id']).values(
            available_at=tomorrow, payload={**row['payload'], 'manual': False,
                                         'last_error_code': 'MEMORY_BUDGET_EXCEEDED'}))
        for _ in range(10):
            enqueue_extraction(conn, seeded_scope, uuid4())
        assert conn.scalar(select(m.outbox.c.available_at)) == tomorrow


def test_member_replies_are_also_batched(db, actor, seeded_scope):
    from test_memory_members import setup_room
    from nagi.core.types import SceneScope
    from nagi.memory.jobs import enqueue_member_extraction, MEMBER_EXTRACT
    binding, *_ = setup_room(db, actor, seeded_scope)
    scope = SceneScope(account_id=actor.account_id, user_id=actor.user_id,
        companion_id=seeded_scope.companion_id, conversation_id=binding['conversation_id'],
        scene='group', room_id=binding['id'])
    with db.engine.begin() as conn:
        enqueue_member_extraction(conn, scope, uuid4())
    assert Worker(db).claim('early', event_types=[MEMBER_EXTRACT]) == []
    with db.engine.begin() as conn:
        for _ in range(7):
            enqueue_member_extraction(conn, scope, uuid4())
    assert len(Worker(db).claim('due', event_types=[MEMBER_EXTRACT])) == 1
