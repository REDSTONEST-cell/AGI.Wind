import pytest

async def test_correction_preserves_original_and_invalidates_projection(db, archive, actor, seeded_scope, chat_request_factory):
    from agi_wind.memory.corrections import CorrectionService
    from agi_wind.memory.retrieval import MemoryService
    from agi_wind.archive import models as m
    from agi_wind.memory import models as mm
    from agi_wind.archive.repository import digest
    from uuid import uuid4
    await archive.accept(actor, chat_request_factory(text="以前喜欢咖啡", conversation_id=seeded_scope.conversation_id))
    event = db.all("events")[0]
    memory_id = uuid4()
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.insert().values(id=memory_id, account_id=actor.account_id, companion_id=seeded_scope.companion_id, user_id=actor.user_id, kind="preference", content="喜欢咖啡", epistemic_type="user_asserted"))
        conn.execute(mm.source_refs.insert().values(memory_id=memory_id, event_id=event["id"], json_pointer="/text"))
    result = await CorrectionService(db).apply_correction(actor, memory_id, "现在不喝咖啡，改喝茶", event["id"])
    assert result["memory_revision"] == 2
    assert db.all("events", id=event["id"])[0]["content"]["text"] == "以前喜欢咖啡"
    assert db.all("memory_items", id=memory_id)[0]["status"] == "superseded"
    await MemoryService(db).index_events(seeded_scope)
    hits = await MemoryService(db).search(seeded_scope, "咖啡")
    assert any("现在不喝" in h.content for h in hits)
    assert all(h.content != "喜欢咖啡" for h in hits)
