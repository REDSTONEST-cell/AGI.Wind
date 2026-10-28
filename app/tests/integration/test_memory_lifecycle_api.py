from datetime import datetime, timedelta, timezone

from test_memory_extraction import FakeExtractor, _completed_turn, _events, _memory_job, candidate


async def test_extraction_sets_lifecycle_and_expired_memory_is_not_recalled(db, actor, seeded_scope, chat_request_factory):
    from nagi.memory.jobs import run_extraction
    from nagi.memory.retrieval import MemoryService
    from nagi.memory.lifecycle import maintain
    from nagi.jobs.worker import Worker
    from nagi.memory import models as mm

    await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    user_event = _events(db, "user")[0]
    extractor = FakeExtractor([candidate("preference", "用户希望被称为小风", user_event["id"], "以后叫我小风")])
    worker = Worker(db)
    job = _memory_job(db, worker)
    assert (await run_extraction(db, extractor, worker, job))["stored"] == 1
    memory = db.all("memory_items")[0]
    assert memory["importance"] > 0.5
    assert memory["ttl_days"] > 0
    assert memory["expires_at"] > datetime.now(timezone.utc)
    assert memory["decay_type"] == "exponential"
    assert memory["access_count"] == 0

    # Normal retrieval does not mutate access; context injection calls touch() explicitly.
    hits = await MemoryService(db).search(seeded_scope, "叫我什么")
    assert any(hit.memory_id == memory["id"] for hit in hits)
    assert db.all("memory_items")[0]["access_count"] == 0

    service = MemoryService(db)
    with db.engine.begin() as conn:
        assert service.touch(conn, [memory["id"]]) == 1
    touched = db.all("memory_items")[0]
    assert touched["access_count"] == 1 and touched["last_accessed_at"] is not None
    assert touched["expires_at"] >= memory["expires_at"]

    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == memory["id"]).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert all(hit.memory_id != memory["id"] for hit in await service.search(seeded_scope, "叫我什么"))
    result = maintain(db, seeded_scope.companion_id)
    assert result["expired"] >= 1
    assert db.all("memory_items")[0]["status"] == "expired"
    assert all(row["id"] != memory["id"] for row in service.list_items(actor, seeded_scope.companion_id, status="active"))
    assert any(row["id"] == memory["id"] for row in service.list_items(actor, seeded_scope.companion_id, status="expired"))


async def test_importance_update_sorting_and_recall_debug(db, actor, seeded_scope, chat_request_factory):
    from nagi.memory.jobs import run_extraction
    from nagi.memory.retrieval import MemoryService
    from nagi.jobs.worker import Worker

    await _completed_turn(db, actor, seeded_scope, chat_request_factory, text="我喜欢咖啡")
    coffee = _events(db, "user")[0]
    worker = Worker(db)
    job = _memory_job(db, worker)
    await run_extraction(db, FakeExtractor([candidate("preference", "用户喜欢咖啡", coffee["id"], "喜欢咖啡")]), worker, job)

    await _completed_turn(db, actor, seeded_scope, chat_request_factory, text="我们一起去了海边")
    beach = sorted(_events(db, "user"), key=lambda row: row["recorded_at"])[-1]
    worker2 = Worker(db)
    job2 = _memory_job(db, worker2)
    await run_extraction(db, FakeExtractor([candidate("episode", "用户去了海边", beach["id"], "一起去了海边")]), worker2, job2)

    service = MemoryService(db)
    coffee_memory = next(row for row in service.list_items(actor, seeded_scope.companion_id) if row["content"] == "用户喜欢咖啡")
    service.update_item(actor, coffee_memory["id"], importance=0.95)
    ordered = service.list_items(actor, seeded_scope.companion_id, sort="importance")
    assert ordered[0]["id"] == coffee_memory["id"]

    debug = await service.recall_debug(seeded_scope, "咖啡")
    coffee_hit = next(hit for hit in debug if hit["memory_id"] == str(coffee_memory["id"]))
    assert coffee_hit["routes"] == ["structured"]
    assert coffee_hit["importance"] > 0.9
    assert coffee_hit["score"] > 0.5
    assert coffee_hit["freshness"] > 0
