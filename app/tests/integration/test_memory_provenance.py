from uuid import uuid4
from sqlalchemy import select
import pytest

async def test_hallucinated_birthday_and_assistant_user_fact_quarantined(db, archive, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.extraction import MemoryWriter, MemoryCandidate
    from agi_wind.core.types import SourceRef
    accepted = await archive.accept(actor, chat_request_factory(text="今天看海",conversation_id=seeded_scope.conversation_id))
    event = db.all("events")[0]
    await archive.append_output(accepted.generation_id,"我小时候住海边")
    assistant = db.all("events",role="assistant")[0]
    writer = MemoryWriter(db)
    bad = MemoryCandidate(kind="fact", content="用户生日是今天",epistemic_type="user_asserted", source_refs=[SourceRef(event_id=event["id"])])
    result = await writer.validate_and_store(seeded_scope, bad, expected_revision=1)
    assert result["status"] == "quarantine"
    bad = MemoryCandidate(kind="user_profile", content="我小时候住海边",epistemic_type="user_asserted", source_refs=[SourceRef(event_id=assistant["id"])])
    assert (await writer.validate_and_store(seeded_scope,bad,1))["status"] == "quarantine"
    assert db.count("memory_items") == 0

async def test_verifiable_quote_stores_and_revision_change_blocks_stale_job(db, archive, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.extraction import MemoryWriter, MemoryCandidate
    from agi_wind.core.types import SourceRef
    from agi_wind.archive.models import companions
    await archive.accept(actor, chat_request_factory(text="故事设定：我住在月球",conversation_id=seeded_scope.conversation_id))
    event = db.all("events")[0]
    candidate = MemoryCandidate(kind="episode",content="我住在月球",epistemic_type="fictional",source_refs=[SourceRef(event_id=event["id"],start_offset=5,end_offset=10)])
    assert (await MemoryWriter(db).validate_and_store(seeded_scope,candidate,1))["status"] == "active"
    with db.engine.begin() as conn: conn.execute(companions.update().values(memory_revision=2))
    with pytest.raises(Exception) as error: await MemoryWriter(db).validate_and_store(seeded_scope,candidate,1)
    assert getattr(error.value,"code",None) == "STALE_CONTEXT"
