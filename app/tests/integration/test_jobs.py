from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from sqlalchemy import select
import pytest

def test_two_workers_claim_once_and_expired_lease_recovers(db, archive, actor, seeded_scope, chat_request_factory):
    import asyncio
    from nagi.jobs.worker import Worker
    from nagi.archive.models import outbox, now
    asyncio.run(archive.accept(actor, chat_request_factory(conversation_id=seeded_scope.conversation_id)))
    worker = Worker(db)
    with ThreadPoolExecutor(2) as pool:
        batches = list(pool.map(worker.claim, ["worker-a", "worker-b"]))
    assert sorted(map(len, batches)) == [0, 1]
    job = next(batch[0] for batch in batches if batch)
    with db.engine.begin() as conn:
        conn.execute(outbox.update().where(outbox.c.id==job["id"]).values(lease_until=now()-timedelta(seconds=1)))
    recovered = worker.claim("worker-c")
    assert recovered[0]["id"] == job["id"]
    assert recovered[0]["attempts"] == 2

async def test_retry_limit_and_owner_fencing(db, archive, actor, seeded_scope, chat_request_factory):
    from nagi.jobs.worker import Worker
    from nagi.archive.models import outbox, now
    from nagi.core.errors import AppError
    await archive.accept(actor, chat_request_factory(conversation_id=seeded_scope.conversation_id))
    worker = Worker(db, jitter=lambda:0)
    for attempt in range(5):
        job = worker.claim("worker-a")[0]
        with pytest.raises(AppError): worker.finish(job["id"], "wrong-worker")
        worker.fail(job["id"], "worker-a", "SYNTHETIC_FAILURE")
        with db.engine.begin() as conn: conn.execute(outbox.update().values(available_at=now()-timedelta(seconds=1)))
    assert worker.claim("worker-a") == []
    assert db.all("outbox")[0]["state"] == "failed"
