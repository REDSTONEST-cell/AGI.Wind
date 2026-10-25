import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from agi_wind.core.errors import AppError


async def test_duplicate_request_reuses_generation(archive, db, actor, seeded_scope, chat_request_factory):
    request = chat_request_factory(conversation_id=seeded_scope.conversation_id)
    first = await archive.accept(actor, request)
    second = await archive.accept(actor, request)
    assert first.generation_id == second.generation_id
    assert db.count("events") == 1
    assert db.count("outbox") == 1


async def test_concurrent_devices_do_not_duplicate(archive, db, actor, seeded_scope, chat_request_factory):
    request = chat_request_factory(conversation_id=seeded_scope.conversation_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(asyncio.run, archive.accept(actor, request)) for _ in range(2)]
        accepted = [future.result() for future in futures]
    assert accepted[0].generation_id == accepted[1].generation_id
    assert db.count("events") == db.count("outbox") == 1


async def test_conflicting_request_and_scope_are_rejected(archive, db, actor, seeded_scope, chat_request_factory):
    from agi_wind.core.types import Actor
    first = chat_request_factory(conversation_id=seeded_scope.conversation_id)
    await archive.accept(actor, first)
    with pytest.raises(AppError) as error:
        await archive.accept(actor, first.model_copy(update={"input": first.input.model_copy(update={"text": "不同内容"})}))
    assert error.value.status == 409
    with pytest.raises(AppError) as error:
        await archive.accept(Actor(account_id=uuid4(), user_id=uuid4()), first)
    assert error.value.status == 403
    assert db.count("events") == 1


async def test_failure_rolls_back_all_accept_records(archive, db, actor, seeded_scope, chat_request_factory):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE outbox ADD CONSTRAINT fail_insert CHECK (false)"))
    with pytest.raises(IntegrityError):
        await archive.accept(actor, chat_request_factory(conversation_id=seeded_scope.conversation_id))
    assert db.count("requests") == db.count("events") == db.count("generations") == 0


async def test_output_persists_partial_before_stream(archive, db, actor, seeded_scope, chat_request_factory):
    accepted = await archive.accept(actor, chat_request_factory(conversation_id=seeded_scope.conversation_id))
    event = await archive.append_output(accepted.generation_id, "你好")
    assert event["content"]["text"] == "你好"
    assert event["content"]["state"] == "partial"
    await archive.append_output(accepted.generation_id, "，世界", final_state="complete")
    outputs = db.all("events", role="assistant")
    assert len(outputs) == 1
    assert outputs[0]["content"] == {"text": "你好，世界", "state": "complete"}
