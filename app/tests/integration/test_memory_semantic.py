"""Synthetic facts in real temporary PostgreSQL schemas; no real model by default."""
import hashlib
import json
import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from agi_wind.archive import models as m
from agi_wind.archive.repository import digest
from agi_wind.memory import models as mm
from agi_wind.memory.lifecycle import now_utc
from agi_wind.memory.retrieval import MemoryService


def source(db, scope, text):
    with db.engine.begin() as conn:
        return conn.execute(m.events.insert().values(account_id=scope.account_id,
            companion_id=scope.companion_id, conversation_id=scope.conversation_id,
            role="user", kind="message", content={"text": text},
            content_sha256=digest({"text": text})).returning(m.events.c.id)).scalar_one()


def fact(db, scope, text, **kw):
    event_id = source(db, scope, text)
    with db.engine.begin() as conn:
        memory_id = conn.execute(mm.memory_items.insert().values(account_id=scope.account_id,
            companion_id=scope.companion_id, user_id=scope.user_id, kind="preference",
            content=text, epistemic_type="user_asserted", **kw).returning(mm.memory_items.c.id)).scalar_one()
        conn.execute(mm.source_refs.insert().values(memory_id=memory_id, event_id=event_id,
            json_pointer="/text", start_offset=0, end_offset=len(text),
            quote_hash=hashlib.sha256(text.encode()).hexdigest()))
    return memory_id, event_id


class FakeEncoder:
    def __init__(self):
        from agi_wind.memory.embedding import EmbeddingSpace
        self.space = EmbeddingSpace(model_name="synthetic-zh", revision="test", weights_hash="a"*64,
            tokenizer_hash="b"*64, dimension=512)
        self.calls = []
        self.before_return = None

    async def embed(self, texts, query=False):
        self.calls.append((list(texts), query))
        if self.before_return:
            self.before_return()
        return [[1.0 if index == (0 if any(word in text for word in ("咖啡", "提神", "饮料")) else 1) else 0.0
                 for index in range(512)] for text in texts]


@pytest.fixture
def encoder(db, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config/memory.local.json").write_text("{}")
    (tmp_path / "data/memory-token.txt").write_text("synthetic-token")
    db.memory_settings = SimpleNamespace(data_dir=tmp_path / "data", memory_semantic_enabled=True,
                                         memory_retention_days={"preference": 10})
    db.memory_semantic_client = FakeEncoder()
    return db.memory_semantic_client


async def test_paraphrase_and_fingerprint_isolation(db, seeded_scope, encoder):
    from agi_wind.memory.semantic import index_semantic, semantic_status
    coffee, _ = fact(db, seeded_scope, "我喜欢咖啡")
    fact(db, seeded_scope, "我喜欢海浪")
    await MemoryService(db).index_events(seeded_scope)
    result = await index_semantic(db, seeded_scope)
    assert result["indexed"] == 4
    assert semantic_status(db) == "semantic_local"
    service = MemoryService(db)
    hits = await service.search(seeded_scope, "最爱的提神饮料是什么")
    assert hits[0].memory_id == coffee
    debug = await service.recall_debug(seeded_scope, "最爱的提神饮料是什么")
    assert "semantic_local" in debug[0]["routes"]
    encoder.space = encoder.space.model_copy(update={"weights_hash": "c"*64})
    assert not any(h.memory_id == coffee for h in await service.search(seeded_scope, "提神饮料"))
    await index_semantic(db, seeded_scope)
    assert db.count("embedding_spaces") == 2
    assert any(h.memory_id == coffee for h in await service.search(seeded_scope, "提神饮料"))


async def test_unavailable_disabled_and_unconfigured_fall_back(db, seeded_scope, encoder):
    from agi_wind.memory.semantic import semantic_status
    coffee, _ = fact(db, seeded_scope, "我喜欢咖啡")
    db.memory_settings.memory_semantic_enabled = False
    assert any(h.memory_id == coffee for h in await MemoryService(db).search(seeded_scope, "咖啡"))
    assert encoder.calls == [] and semantic_status(db) == "keyword_only"
    db.memory_settings.memory_semantic_enabled = True
    async def unavailable(*args, **kwargs):
        raise TimeoutError()
    encoder.embed = unavailable
    assert any(h.memory_id == coffee for h in await MemoryService(db).search(seeded_scope, "咖啡"))
    assert semantic_status(db) == "local_unavailable"
    del db.memory_settings
    assert semantic_status(db) == "keyword_only"


async def test_incremental_index_is_bounded_skips_empty_and_replaces_edited(db, seeded_scope):
    service = MemoryService(db)
    source(db, seeded_scope, " ")
    source(db, seeded_scope, "\n\t")
    source(db, seeded_scope, "")
    first = source(db, seeded_scope, "合成甲")
    source(db, seeded_scope, "合成乙")
    assert await service.index_events(seeded_scope, limit=1) == 1
    assert db.count("retrieval_chunks") == 1
    assert await service.index_events(seeded_scope, limit=1) == 1
    assert await service.index_events(seeded_scope, limit=1) == 0
    with db.engine.begin() as conn:
        conn.execute(m.events.update().where(m.events.c.id == first).values(
            content={"text": "合成丙"}, content_sha256=digest({"text": "合成丙"})))
    assert await service.index_events(seeded_scope, limit=1) == 1
    assert {r["content"] for r in db.all("retrieval_chunks")} == {"合成丙", "合成乙"}


@pytest.mark.parametrize("change", ["hidden", "disputed", "superseded", "expired", "visibility", "source_edit", "valid_to", "fictional"])
async def test_inactive_sources_and_shared_summary_never_resurrect(db, seeded_scope, encoder, change):
    from agi_wind.memory.semantic import index_semantic
    mid, eid = fact(db, seeded_scope, "我喜欢咖啡")
    with db.engine.begin() as conn:
        summary = conn.execute(mm.memory_items.insert().values(account_id=seeded_scope.account_id,
            companion_id=seeded_scope.companion_id, user_id=seeded_scope.user_id, kind="episode",
            content="咖啡偏好日总结", epistemic_type="observed", summary_level="daily").returning(mm.memory_items.c.id)).scalar_one()
        ref = dict(conn.execute(select(mm.source_refs).where(mm.source_refs.c.memory_id == mid)).mappings().one())
        ref.update(id=uuid4(), memory_id=summary)
        conn.execute(mm.source_refs.insert().values(**ref))
    service = MemoryService(db)
    await service.index_events(seeded_scope)
    await index_semantic(db, seeded_scope)
    with db.engine.begin() as conn:
        if change == "visibility":
            conn.execute(m.events.update().where(m.events.c.id == eid).values(visibility="hidden"))
        elif change == "source_edit":
            conn.execute(m.events.update().where(m.events.c.id == eid).values(content={"text": "我喜欢茶"}, content_sha256=digest({"text": "我喜欢茶"})))
        else:
            values = ({"expires_at": now_utc()-timedelta(seconds=1)} if change == "expired" else
                      {"valid_to": now_utc()-timedelta(seconds=1)} if change == "valid_to" else
                      {"epistemic_type": "fictional"} if change == "fictional" else {"status": change})
            conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == mid).values(**values))
    assert await service.search(seeded_scope, "咖啡") == []


async def test_index_rechecks_sources_and_revision_after_model_work(db, seeded_scope, encoder):
    from agi_wind.memory.semantic import index_semantic
    mid, eid = fact(db, seeded_scope, "我喜欢咖啡")
    def change():
        with db.engine.begin() as conn:
            conn.execute(m.events.update().where(m.events.c.id == eid).values(visibility="hidden"))
    encoder.before_return = change
    result = await index_semantic(db, seeded_scope)
    assert result["indexed"] == 0 and db.count("memory_vectors") == 0


async def test_pins_are_included_and_expiry_preserved_on_importance_edit(db, actor, seeded_scope):
    mid, _ = fact(db, seeded_scope, "我喜欢咖啡")
    service = MemoryService(db)
    assert service.update_item(actor, mid, is_pinned=True)["is_pinned"] is True
    service.update_item(actor, mid, importance=0.95, ttl_days=2)
    row = service.get_item(actor, mid)
    assert row["expires_at"] is None and row["ttl_days"] == 2
    assert (await service.search(seeded_scope, "你好"))[0].memory_id == mid
    assert service.update_item(actor, mid, is_pinned=False)["expires_at"] > now_utc()


async def test_foreign_user_and_companion_vectors_and_fk_delete(db, actor, seeded_scope, encoder):
    from agi_wind.core.types import Actor
    from agi_wind.memory.semantic import index_semantic
    other = db.seed(Actor(account_id=actor.account_id, user_id=uuid4()))
    foreign, _ = fact(db, other, "我喜欢咖啡")
    local, _ = fact(db, seeded_scope, "我喜欢咖啡")
    await index_semantic(db, other)
    await index_semantic(db, seeded_scope)
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == local).values(user_id=other.user_id))
    assert await MemoryService(db).search(seeded_scope, "提神饮料") == []
    with db.engine.begin() as conn:
        conn.execute(mm.source_refs.delete().where(mm.source_refs.c.memory_id == foreign))
        conn.execute(mm.memory_items.delete().where(mm.memory_items.c.id == foreign))
    assert not db.all("memory_vectors", memory_id=foreign)


def test_stats_counts_pins_summaries_and_disputes_in_scope(db, actor, seeded_scope):
    fact(db, seeded_scope, "我喜欢咖啡", is_pinned=True)
    fact(db, seeded_scope, "我喜欢茶", status="disputed")
    fact(db, seeded_scope, "合成日总结", summary_level="daily")
    stats = MemoryService(db).stats(actor, seeded_scope.companion_id)
    assert stats["pinned_count"] == stats["disputed_count"] == stats["summary_count"] == 1


def test_detail_exposes_scoped_relation_history(db, actor, seeded_scope):
    old, _ = fact(db, seeded_scope, "我喜欢咖啡", status="superseded")
    current, _ = fact(db, seeded_scope, "我喜欢茶")
    with db.engine.begin() as conn:
        conn.execute(mm.memory_relations.insert().values(from_memory_id=current,to_memory_id=old,relation_type="supersedes"))
    item = MemoryService(db).get_item(actor,current)
    assert item["related_items"][0]["id"] == old
    assert item["related_items"][0]["status"] == "superseded"
    assert item["memory_relations"][0]["relation_type"] == "supersedes"


async def test_chunk_vector_fk_delete_and_changed_content_reindex(db, seeded_scope, encoder):
    from agi_wind.memory.semantic import index_semantic
    mid, _ = fact(db,seeded_scope,"我喜欢咖啡")
    service = MemoryService(db)
    await service.index_events(seeded_scope)
    assert (await index_semantic(db,seeded_scope))["indexed"] == 2
    assert (await index_semantic(db,seeded_scope))["indexed"] == 0
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.update().where(mm.memory_items.c.id == mid).values(content="用户偏爱咖啡"))
    assert (await index_semantic(db,seeded_scope))["indexed"] == 1
    with db.engine.begin() as conn:
        conn.execute(mm.retrieval_chunks.delete())
    assert db.count("chunk_vectors") == 0


async def test_memory_source_changes_during_encode_are_discarded(db,seeded_scope,encoder):
    from agi_wind.memory.semantic import index_semantic
    _, eid = fact(db,seeded_scope,"我喜欢咖啡")
    def change():
        with db.engine.begin() as conn:
            text = "我喜欢咖啡和海浪"
            conn.execute(m.events.update().where(m.events.c.id == eid).values(content={"text":text},content_sha256=digest({"text":text})))
    encoder.before_return = change
    assert (await index_semantic(db,seeded_scope))["indexed"] == 0
    assert db.count("memory_vectors") == 0


async def test_ineligible_old_fact_cannot_starve_incremental_index(db,seeded_scope,encoder):
    from agi_wind.memory.semantic import index_semantic
    _, old_source = fact(db,seeded_scope,"我喜欢海浪")
    current,_ = fact(db,seeded_scope,"我喜欢咖啡")
    with db.engine.begin() as conn:
        conn.execute(m.events.update().where(m.events.c.id == old_source).values(visibility="hidden"))
    result = await index_semantic(db,seeded_scope,limit=1)
    assert result["indexed"] == 1
    assert db.all("memory_vectors")[0]["memory_id"] == current


async def test_semantic_candidate_loading_keeps_gateway_event_loop_responsive(db,seeded_scope,encoder,monkeypatch):
    from agi_wind.memory import semantic
    fact(db,seeded_scope,"我喜欢咖啡")
    await semantic.index_semantic(db,seeded_scope)
    original = semantic.verified_refs
    def slow_vector_verification(conn,scope,memories):
        if memories and "vector" in memories[0]:
            time.sleep(0.15)  # Simulate a large candidate/provenance read.
        return original(conn,scope,memories)
    monkeypatch.setattr(semantic,"verified_refs",slow_vector_verification)
    task = asyncio.create_task(semantic.semantic_candidates(db,seeded_scope,"提神饮料"))
    try:
        await asyncio.sleep(0.03)
        assert not task.done(), "candidate DB verification blocked the event loop until completion"
        memories,_ = await task
        assert memories
    finally:
        await task
