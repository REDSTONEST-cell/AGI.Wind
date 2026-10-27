from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from agi_wind.core.types import ModelRef

class FakeProvider:
    def __init__(self, fail=False): self.calls, self.fail = 0, fail
    async def stream(self, model, messages):
        self.calls += 1
        yield "你好"
        if self.fail: raise RuntimeError("synthetic-private-upstream-error")
        yield "，世界。"

async def test_duplicate_submit_and_reconnect_never_regenerate(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.delivery.service import GenerationService
    provider = FakeProvider()
    service = GenerationService(db, provider, model=ModelRef(provider_id="fake", model_id="synthetic"))
    request = chat_request_factory(conversation_id=seeded_scope.conversation_id)
    first = await service.submit(actor, request)
    assert (await service.submit(actor, request)).generation_id == first.generation_id
    assert await service.run(first.generation_id)
    await service.run(first.generation_id)
    assert provider.calls == 1
    events = service.replay(actor, first.generation_id, 0)
    assert events[-1].type == "generation.completed"
    assert "".join(e.payload["text"] for e in events if e.type == "text.delta") == "你好，世界。"
    context_event = next(e for e in events if e.type == "memory.context")
    assert context_event.payload["memory_count"] >= len(context_event.payload["memory_ids"])
    assert context_event.payload["token_estimate"] > 0
    assert context_event.payload["model"] == {"provider_id": "fake", "model_id": "synthetic"}
    cutoff = events[1].sequence
    replay = service.replay(actor, first.generation_id, cutoff)
    assert all(event.sequence > cutoff for event in replay)
    assert db.all("events", role="assistant")[0]["content"]["state"] == "complete"

async def test_db_failure_never_calls_provider(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.delivery.service import GenerationService
    provider = FakeProvider()
    service = GenerationService(db, provider, model=ModelRef(provider_id="fake",model_id="test"))
    with db.engine.begin() as conn: conn.execute(text("ALTER TABLE outbox ADD CONSTRAINT fail_write CHECK(false)"))
    with pytest.raises(IntegrityError):
        await service.submit(actor, chat_request_factory(conversation_id=seeded_scope.conversation_id))
    assert provider.calls == 0

async def test_stream_failure_preserves_partial_and_never_exposes_raw_error(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.delivery.service import GenerationService
    service = GenerationService(db, FakeProvider(fail=True), model=ModelRef(provider_id="fake",model_id="test"))
    accepted = await service.submit(actor, chat_request_factory(conversation_id=seeded_scope.conversation_id))
    await service.run(accepted.generation_id)
    output = db.all("events", role="assistant")[0]
    assert output["content"] == {"text":"你好", "state":"partial"}
    assert db.all("generations")[0]["state"] == "failed"
    assert "private" not in "".join(e.model_dump_json() for e in service.replay(actor, accepted.generation_id, 0))

async def test_streamed_reasoning_is_persisted_in_batches_without_loss(db, actor, seeded_scope, chat_request_factory):
    from agi_wind.delivery.service import GenerationService
    from agi_wind.providers.protocol import ProviderChunk
    class Thinking:
        async def stream(self, model, messages):
            for index in range(300):
                yield ProviderChunk('reasoning', '想' + str(index % 10))
            yield ProviderChunk('text', '好的。')
    service = GenerationService(db, Thinking(), model=ModelRef(provider_id="fake", model_id="synthetic"))
    accepted = await service.submit(actor, chat_request_factory(conversation_id=seeded_scope.conversation_id))
    assert await service.run(accepted.generation_id)
    events = service.replay(actor, accepted.generation_id, 0)
    deltas = [e for e in events if e.type == "reasoning.delta"]
    expected = "".join('想' + str(index % 10) for index in range(300))
    # One stream/database write per token used to rewrite the whole event 300 times.
    assert 0 < len(deltas) <= 4
    assert "".join(e.payload["text"] for e in deltas) == expected
    assert [e.type for e in events].index("reasoning.delta") < [e.type for e in events].index("text.delta")
    output = db.all("events", role="assistant")[0]["content"]
    assert output["reasoning"] == expected
    assert [step["text"] for step in output["trace"] if step["type"] == "reasoning"] == [expected]
