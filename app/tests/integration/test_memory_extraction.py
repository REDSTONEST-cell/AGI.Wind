"""Phase-1 memory extraction: durable job, source proof, retry, delete race, recall."""
import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest

from agi_wind.core.settings import Settings
from agi_wind.core.types import ModelRef


class FakeProvider:
    def __init__(self, text="好的，我记住了。"):
        self.text, self.calls = text, 0

    async def stream(self, model, messages):
        self.calls += 1
        yield self.text


class FakeExtractor:
    """Deterministic extraction adapter used to test the job/writer, not model quality."""

    def __init__(self, candidates=None, on_extract=None):
        self.candidates, self.on_extract, self.calls = candidates or [], on_extract, 0

    async def extract(self, scope, events):
        self.calls += 1
        if self.on_extract:
            await self.on_extract(scope, events)
        return self.candidates, {"calls": 1, "input_chars": 1, "output_chars": 1}


async def _completed_turn(db, actor, seeded_scope, chat_request_factory, text="以后叫我小风，我不吃香菜"):
    from agi_wind.delivery.service import GenerationService
    service = GenerationService(db, FakeProvider(), model=ModelRef(provider_id="fake", model_id="test"))
    accepted = await service.submit(actor, chat_request_factory(text=text, conversation_id=seeded_scope.conversation_id))
    assert await service.run(accepted.generation_id)
    return accepted


def _events(db, role=None):
    rows = db.all("events", **({"role": role} if role else {}))
    return sorted(rows, key=lambda row: row["recorded_at"])


def _memory_job(db, worker, worker_id="extractor"):
    # Handler/writer tests deliberately advance past the automatic idle deadline;
    # the real scheduling contract is covered separately by test_memory_schedule.
    from agi_wind.archive import models as m
    with db.engine.begin() as conn:
        conn.execute(m.outbox.update().where(m.outbox.c.event_type == 'memory.extract',
            m.outbox.c.state == 'pending').values(available_at=m.now()-timedelta(seconds=1)))
    jobs = worker.claim(worker_id, batch_size=4)
    job = next((item for item in jobs if item["event_type"] == "memory.extract"), None)
    assert job is not None
    return job


def candidate(kind, content, event_id, quote, epistemic="user_asserted"):
    from agi_wind.memory.extraction import ExtractedCandidate, ExtractedSourceRef
    return ExtractedCandidate(kind=kind, content=content, epistemic_type=epistemic, source_refs=[ExtractedSourceRef(event_id=event_id, quote=quote)])


async def test_completed_turn_enqueues_extraction_and_cross_session_recall(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.jobs import MEMORY_EXTRACT, enqueue_extraction, run_extraction
    from agi_wind.memory.retrieval import MemoryService
    from agi_wind.jobs.worker import Worker

    accepted = await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    user_event = _events(db, "user")[0]
    outbox = db.all("outbox")
    memory_jobs = [row for row in outbox if row["event_type"] == MEMORY_EXTRACT]
    assert len(memory_jobs) == 1 and memory_jobs[0]["aggregate_id"] == seeded_scope.companion_id
    # Same generation cannot enqueue the same extraction twice.
    with db.engine.begin() as conn:
        assert enqueue_extraction(conn, seeded_scope, accepted.generation_id) is None
    assert [row["event_type"] for row in db.all("outbox")].count(MEMORY_EXTRACT) == 1

    extractor = FakeExtractor([candidate("preference", "用户希望被称为小风", user_event["id"], "以后叫我小风")])
    worker = Worker(db)
    job = _memory_job(db, worker)
    result = await run_extraction(db, extractor, worker, job)
    worker.finish(job["id"], "extractor")
    assert result["stored"] == 1 and result["quarantined"] == 0
    assert db.all("outbox", id=job["id"])[0]["state"] == "completed"
    assert db.count("memory_items") == 1
    assert db.count("source_refs") == 1
    memory = db.all("memory_items")[0]
    assert memory["content"] == "用户希望被称为小风"
    assert memory["status"] == "active" and memory["epistemic_type"] == "user_asserted"
    assert db.all("companions")[0]["memory_revision"] == 2
    assert db.all("companions")[0]["memory_extract_cursor_id"] == _events(db)[-1]["id"]

    hits = await MemoryService(db).search(seeded_scope, "你还记得以后要叫我什么吗")
    recall = [hit for hit in hits if hit.memory_id == memory["id"]]
    assert recall and any(ref.event_id == user_event["id"] for ref in recall[0].source_refs)
    # Scope isolation: another account must not see this memory or source.
    from agi_wind.core.types import Actor
    other = Actor(account_id=uuid4(), user_id=uuid4())
    other_scope = db.seed(other)
    assert await MemoryService(db).search(other_scope, "叫我什么") == []


async def test_rerun_after_crash_is_idempotent_and_never_calls_model_again(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.jobs import run_extraction
    from agi_wind.jobs.worker import Worker

    await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    user_event = _events(db, "user")[0]
    extractor = FakeExtractor([candidate("preference", "用户希望被称为小风", user_event["id"], "以后叫我小风")])
    worker = Worker(db)
    job = _memory_job(db, worker)
    first = await run_extraction(db, extractor, worker, job)
    # Simulate a crash after the data commit but before worker.finish: the retry sees the
    # advanced cursor and performs a no-op instead of duplicating the memory.
    second = await run_extraction(db, extractor, worker, job)
    assert first["stored"] == 1 and second["status"] == "noop"
    assert extractor.calls == 1
    assert db.count("memory_items") == 1 and db.count("source_refs") == 1


async def test_invalid_extraction_json_retries_without_writing(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.jobs import run_extraction
    from agi_wind.jobs.worker import Worker

    await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    calls = []

    async def complete(model, messages):
        calls.append(messages)
        return "这不是 JSON"

    from agi_wind.memory.extraction import Extractor
    worker = Worker(db, jitter=lambda: 0)
    job = _memory_job(db, worker)
    result = await worker.run_job(job["id"], "extractor", lambda item: run_extraction(db, Extractor(complete, ModelRef(provider_id="fake", model_id="m")), worker, item))
    assert result == {"state": "retry_or_failed", "code": "EXTRACTION_INVALID"}
    assert len(calls) == 2  # original + one bounded repair, never a third guess
    assert db.count("memory_items") == 0 and db.count("quarantine") == 0
    row = db.all("outbox", id=job["id"])[0]
    assert row["state"] == "pending" and row["attempts"] == 1
    assert db.all("companions")[0]["memory_extract_cursor_at"] is None


async def test_forged_and_assistant_user_fact_candidates_are_quarantined(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.jobs import run_extraction
    from agi_wind.jobs.worker import Worker

    await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    user_event, assistant_event = _events(db, "user")[0], _events(db, "assistant")[0]
    extractor = FakeExtractor([
        candidate("fact", "用户生日是今天", uuid4(), "今天"),
        candidate("fact", "用户说记住了", assistant_event["id"], "记住了"),
    ])
    worker = Worker(db)
    job = _memory_job(db, worker)
    result = await run_extraction(db, extractor, worker, job)
    assert result["quarantined"] == 2 and result["stored"] == 0
    assert db.count("memory_items") == 0
    reasons = {row["reason"] for row in db.all("quarantine")}
    assert "UNVERIFIABLE_SOURCE" in reasons and "ASSISTANT_IS_NOT_USER_FACT" in reasons


async def test_source_deleted_during_model_call_cannot_be_committed(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from agi_wind.memory.jobs import run_extraction
    from agi_wind.jobs.worker import Worker
    from agi_wind.archive import models as m

    await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    user_event = _events(db, "user")[0]

    async def delete_source(scope, events):
        from agi_wind.memory import models as mm
        with db.engine.begin() as conn:
            # Mirror the deletion service's derived-first order without creating a ledger here.
            conn.execute(mm.retrieval_chunks.delete().where(mm.retrieval_chunks.c.source_id == user_event["id"]))
            conn.execute(m.events.delete().where(m.events.c.id == user_event["id"]))

    extractor = FakeExtractor([candidate("preference", "用户希望被称为小风", user_event["id"], "以后叫我小风")], on_extract=delete_source)
    worker = Worker(db)
    job = _memory_job(db, worker)
    result = await run_extraction(db, extractor, worker, job)
    assert result["stored"] == 0 and result["quarantined"] == 1
    assert db.count("memory_items") == 0
    assert db.all("quarantine")[0]["reason"] == "UNVERIFIABLE_SOURCE"


async def test_lost_lease_never_writes_memory(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.jobs import run_extraction
    from agi_wind.jobs.worker import Worker
    from agi_wind.archive import models as m
    from agi_wind.core.errors import AppError

    await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    user_event = _events(db, "user")[0]
    extractor = FakeExtractor([candidate("preference", "用户希望被称为小风", user_event["id"], "以后叫我小风")])
    worker = Worker(db)
    job = _memory_job(db, worker)
    with db.engine.begin() as conn:
        conn.execute(m.outbox.update().where(m.outbox.c.id == job["id"]).values(lease_until=m.now() - timedelta(seconds=1)))
    with pytest.raises(AppError) as error:
        await run_extraction(db, extractor, worker, job)
    assert error.value.code == "LEASE_LOST"
    assert extractor.calls == 0
    assert db.count("memory_items") == 0


async def test_correction_replaces_structured_current_fact(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.jobs import run_extraction
    from agi_wind.jobs.worker import Worker
    from agi_wind.memory.retrieval import MemoryService
    from agi_wind.memory.corrections import CorrectionService

    await _completed_turn(db, actor, seeded_scope, chat_request_factory, text="我喜欢咖啡")
    user_event = _events(db, "user")[0]
    extractor = FakeExtractor([candidate("preference", "用户喜欢咖啡", user_event["id"], "喜欢咖啡")])
    worker = Worker(db)
    job = _memory_job(db, worker)
    stored = await run_extraction(db, extractor, worker, job)
    assert stored["stored"] == 1
    old_memory = db.all("memory_items")[0]
    result = await CorrectionService(db).apply_correction(actor, old_memory["id"], "现在不喝咖啡，改喝茶", user_event["id"])
    hits = await MemoryService(db).search(seeded_scope, "咖啡")
    structured = [hit for hit in hits if hit.memory_id]
    assert any(hit.memory_id == result["memory_id"] and "现在不喝" in hit.content for hit in structured)
    assert all(hit.memory_id != old_memory["id"] for hit in structured)


async def test_gateway_worker_runs_extraction_after_real_chat_loop(db, actor, seeded_scope, tmp_path, monkeypatch):
    """End-to-end through create_app's outbox loop: chat -> archive -> extract -> memory."""
    from agi_wind.api.app import create_app
    from agi_wind.delivery.service import GenerationService
    # This test covers the real worker, not a two-minute wall-clock wait.
    monkeypatch.setattr('agi_wind.memory.jobs.AUTO_EXTRACT_IDLE_SECONDS', 0)

    class DynamicExtractor:
        async def extract(self, scope, events):
            user = next(event for event in events if event["role"] == "user")
            quote = "以后叫我小风"
            if quote not in user["text"]:
                quote = user["text"][:4]
            return [candidate("preference", "用户希望被称为小风", UUID(user["event_id"]), quote)], {"calls": 1}

    service = GenerationService(db, FakeProvider(), model=ModelRef(provider_id="fake", model_id="test"))
    service.memory_extractor = DynamicExtractor()
    from agi_wind.archive.deletions import DeletionLedger
    DeletionLedger(tmp_path / "governance").initialize()
    app = create_app(Settings(access_token="synthetic", data_dir=tmp_path), db, actor, service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/chat/requests",
                headers={"Authorization": "Bearer synthetic"},
                json={"request_id": str(uuid4()), "conversation_id": str(seeded_scope.conversation_id), "input": {"text": "以后叫我小风"}, "device_id": "test"},
            )
            assert response.status_code == 202
            for _ in range(100):
                if db.count("memory_items") >= 1:
                    break
                await asyncio.sleep(0.05)
    assert len(db.all("events", role="assistant")) == 1
    memory = db.all("memory_items")[0]
    assert memory["content"] == "用户希望被称为小风"
    jobs = [row for row in db.all("outbox") if row["event_type"] == "memory.extract"]
    assert jobs and jobs[0]["state"] == "completed"


async def test_hide_restore_query_filter_and_extraction_does_not_resurrect(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.jobs import run_extraction
    from agi_wind.memory.retrieval import MemoryService
    from agi_wind.jobs.worker import Worker
    from agi_wind.core.types import Actor
    from agi_wind.core.errors import AppError

    await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    user_event = _events(db, "user")[0]
    extractor = FakeExtractor([candidate("preference", "用户希望被称为小风", user_event["id"], "以后叫我小风")])
    worker = Worker(db)
    job = _memory_job(db, worker)
    await run_extraction(db, extractor, worker, job)
    worker.finish(job["id"], "extractor")
    memory = db.all("memory_items")[0]
    service = MemoryService(db)
    assert [row["id"] for row in service.list_items(actor, seeded_scope.companion_id, query="小风")] == [memory["id"]]
    assert service.list_items(actor, seeded_scope.companion_id, query="完全不存在") == []

    service.set_status(actor, memory["id"], "hidden")
    assert db.all("memory_items")[0]["status"] == "hidden"
    assert [row["id"] for row in service.list_items(actor, seeded_scope.companion_id, status="hidden")] == [memory["id"]]
    hits = await service.search(seeded_scope, "你还记得以后要叫我什么吗")
    assert all(hit.memory_id != memory["id"] for hit in hits)
    other = Actor(account_id=uuid4(), user_id=uuid4())
    db.seed(other)
    with pytest.raises(AppError) as error:
        service.set_status(other, memory["id"], "active")
    assert error.value.status == 403

    service.set_status(actor, memory["id"], "active")
    assert db.all("memory_items")[0]["status"] == "active"
    hits = await service.search(seeded_scope, "你还记得以后要叫我什么吗")
    assert any(hit.memory_id == memory["id"] for hit in hits)

    # A hidden fact must not come back as a new active memory when the same source wording is
    # extracted again in a later turn.
    service.set_status(actor, memory["id"], "hidden")
    await _completed_turn(db, actor, seeded_scope, chat_request_factory, text="以后叫我小风")
    second_user = sorted(_events(db, "user"), key=lambda row: row["recorded_at"])[-1]
    second_extractor = FakeExtractor([candidate("preference", "用户希望被称为小风", second_user["id"], "以后叫我小风")])
    worker2 = Worker(db)
    second_job = _memory_job(db, worker2)
    result = await run_extraction(db, second_extractor, worker2, second_job)
    assert result["stored"] == 0 and result["ignored"] == 1
    assert db.count("memory_items") == 1
    assert db.all("memory_items")[0]["status"] == "hidden"


async def test_memory_list_detail_and_job_status_api(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from agi_wind.memory.jobs import run_extraction
    from agi_wind.jobs.worker import Worker
    from agi_wind.api.app import create_app

    await _completed_turn(db, actor, seeded_scope, chat_request_factory)
    user_event = _events(db, "user")[0]
    extractor = FakeExtractor([candidate("preference", "用户希望被称为小风", user_event["id"], "以后叫我小风")])
    worker = Worker(db)
    job = _memory_job(db, worker)
    await run_extraction(db, extractor, worker, job)
    worker.finish(job["id"], "extractor")
    memory = db.all("memory_items")[0]
    app = create_app(Settings(access_token="synthetic", data_dir=tmp_path), db, actor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer synthetic"}
        listing = await client.get(f"/api/v1/memory/items?companion_id={seeded_scope.companion_id}", headers=headers)
        assert listing.status_code == 200
        item = listing.json()[0]
        assert item["content"] == "用户希望被称为小风" and item["source_count"] == 1
        filtered = await client.get(f"/api/v1/memory/items?companion_id={seeded_scope.companion_id}&q=小风", headers=headers)
        assert filtered.status_code == 200 and len(filtered.json()) == 1
        hidden = await client.patch(f"/api/v1/memory/items/{memory['id']}", headers=headers, json={"status": "hidden"})
        assert hidden.status_code == 200 and hidden.json()["status"] == "hidden"
        hidden_list = await client.get(f"/api/v1/memory/items?companion_id={seeded_scope.companion_id}&status=hidden", headers=headers)
        assert hidden_list.status_code == 200 and hidden_list.json()[0]["id"] == str(memory["id"])
        restored = await client.patch(f"/api/v1/memory/items/{memory['id']}", headers=headers, json={"status": "active"})
        assert restored.status_code == 200 and restored.json()["status"] == "active"
        detail = await client.get(f"/api/v1/memory/items/{memory['id']}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["source_refs"][0]["quote"] == "以后叫我小风"
        assert detail.json()["importance"] > 0 and detail.json()["expires_at"]
        important = await client.patch(f"/api/v1/memory/items/{memory['id']}", headers=headers, json={"importance": 0.95})
        assert important.status_code == 200 and important.json()["importance"] == 0.95
        recalled = await client.post("/api/v1/memory/recall-test", headers=headers, json={"companion_id": str(seeded_scope.companion_id), "query": "叫我什么"})
        assert recalled.status_code == 200
        assert any(hit.get("memory_id") == str(memory["id"]) and "structured" in hit["routes"] and hit["importance"] > 0.9 for hit in recalled.json())
        maintained = await client.post("/api/v1/memory/maintenance", headers=headers, json={"companion_id": str(seeded_scope.companion_id)})
        assert maintained.status_code == 200 and maintained.json()["expired"] == 0
        stats = await client.get(f"/api/v1/memory/stats?companion_id={seeded_scope.companion_id}", headers=headers)
        assert stats.status_code == 200
        facts = stats.json()
        assert facts["counts"]["active"] == 1 and facts["kinds"]["preference"] == 1
        assert len(facts["importance_buckets"]) == 10
        assert facts["jobs_total"] >= 1 and facts["visible_events"] >= 2
        assert facts["settings"]["extractor"] == "unconfigured"
        tasks = await client.get(f"/api/v1/memory/extractions?companion_id={seeded_scope.companion_id}", headers=headers)
        assert tasks.status_code == 200 and tasks.json()[0]["result"]["stored"] == 1
        queued = await client.post("/api/v1/memory/extractions", headers=headers, json={"companion_id": str(seeded_scope.companion_id)})
        assert queued.status_code == 201 and queued.json()["queued"] is True
        queued_jobs = (await client.get(f"/api/v1/memory/extractions?companion_id={seeded_scope.companion_id}", headers=headers)).json()
        assert any(job["state"] == "pending" and job["result"] is None for job in queued_jobs)
        denied = await client.get(f"/api/v1/memory/items/{uuid4()}", headers=headers)
        assert denied.status_code == 404
