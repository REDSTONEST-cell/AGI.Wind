async def test_reindex_preserves_authoritative_events_and_persona(db,actor,seeded_scope,archive,chat_request_factory):
    from agi_wind.memory.retrieval import MemoryService
    from agi_wind.archive.backup import IndexService
    from agi_wind.memory.models import retrieval_chunks
    await archive.accept(actor,chat_request_factory(text="2026年一起看了海",conversation_id=seeded_scope.conversation_id))
    original=[row["content_sha256"] for row in db.all("events")]
    persona=db.all("persona_versions")[0]
    await MemoryService(db).index_events(seeded_scope)
    with db.engine.begin() as conn: conn.execute(retrieval_chunks.delete())
    await IndexService(db).rebuild_keywords(seeded_scope)
    assert [row["content_sha256"] for row in db.all("events")]==original
    assert db.all("persona_versions")[0]==persona
    assert await MemoryService(db).search(seeded_scope,"看了海")
