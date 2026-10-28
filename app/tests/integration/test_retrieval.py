from uuid import uuid4
import pytest

async def test_chinese_dates_models_and_scope_without_vectors(db, archive, actor, seeded_scope, chat_request_factory):
    from nagi.memory.retrieval import MemoryService
    from nagi.core.types import Actor
    for text in ["去年小王不喝咖啡，今年小王改喝茶。", "2026-10-25购买RTX4060，准备训练声音。", "故事设定：另一个小王住在月球上。"]:
        await archive.accept(actor, chat_request_factory(text=text, conversation_id=seeded_scope.conversation_id))
    memory = MemoryService(db)
    await memory.index_events(seeded_scope)
    found = await memory.search(seeded_scope, "RTX4060")
    assert any("2026-10-25" in hit.content for hit in found)
    assert all(hit.source_refs for hit in found)
    other = Actor(account_id=uuid4(), user_id=uuid4())
    other_scope = db.seed(other)
    assert await memory.search(other_scope, "RTX4060") == []
    # A forged account on the original companion is denied before retrieval.
    with pytest.raises(Exception) as error:
        await memory.search(seeded_scope.model_copy(update={"account_id": other.account_id}), "RTX4060")
    assert getattr(error.value, "status", None) == 403
