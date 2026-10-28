"""External embedding and rerank against synthetic facts in a real temporary PostgreSQL schema.

No real provider is contacted: the encoder and reranker are injected into the database object.
"""
import asyncio
import hashlib
import json

import httpx
import pytest

from agi_wind.archive import models as m
from agi_wind.archive.repository import digest
from agi_wind.core.secrets import SecretStore
from agi_wind.memory import models as mm
from agi_wind.memory.retrieval import MemoryService

SECRET = "synthetic-external-memory-key"


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


def external_settings(tmp_path, enabled=True, *, with_secret=True):
    from agi_wind.core.settings import Settings
    data_dir = tmp_path / "data"
    if with_secret:
        SecretStore(data_dir).set("EXT_KEY", SECRET)
    return Settings.model_validate({
        "data_dir": str(data_dir),
        "access_token": "synthetic-memory-token",
        "providers": {"ext": {"base_url": "https://vendor.invalid/v1", "credential_ref": "EXT_KEY",
                              "capabilities": ["embedding"], "supported_parameters": []}},
        "models": {"embedding": {"provider_id": "ext", "model_id": "text-embedding-test"}},
        "memory_external_embedding_enabled": enabled,
    })


class FakeExternalEncoder:
    """One-hot synthetic vectors, same shape as the local-model fake in test_memory_semantic."""

    def __init__(self):
        from agi_wind.memory.embedding import EmbeddingSpace
        self.space = EmbeddingSpace(model_name="synthetic-external", revision="test",
            weights_hash="c" * 64, tokenizer_hash="d" * 64, dimension=512)
        self.calls = []

    async def embed(self, texts, query=False):
        self.calls.append((list(texts), query))
        return [[1.0 if index == (0 if any(word in text for word in ("咖啡", "提神", "饮料")) else 1) else 0.0
                 for index in range(512)] for text in texts]


@pytest.fixture
def external(db, tmp_path):
    db.memory_settings = external_settings(tmp_path)
    db.memory_external_embedding_client = FakeExternalEncoder()
    return db


class FakeReranker:
    """Ascending scores reverse the candidate order; used to observe that rerank applied."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.timeout_seconds = 2.0

    async def rerank(self, query, documents):
        self.calls.append((query, list(documents)))
        if self.error is not None:
            raise self.error
        return [(index, float(index)) for index in range(len(documents))]


def hit_ids(hits):
    return [hit.memory_id or hit.chunk_id for hit in hits]


async def test_external_embedding_indexes_and_retrieves_without_local_deployment(db, seeded_scope, external):
    from agi_wind.memory.semantic import index_semantic, semantic_status
    coffee, _ = fact(db, seeded_scope, "我喜欢咖啡")
    fact(db, seeded_scope, "我喜欢海浪")
    await MemoryService(db).index_events(seeded_scope)
    result = await index_semantic(db, seeded_scope)
    assert result["status"] == "semantic_external" and result["indexed"] > 0
    assert semantic_status(db) == "semantic_external"
    service = MemoryService(db)
    hits = await service.search(seeded_scope, "最爱的提神饮料是什么")
    assert any(hit.memory_id == coffee for hit in hits)
    debug = await service.recall_debug(seeded_scope, "最爱的提神饮料是什么")
    assert any("semantic_external" in item["routes"] for item in debug)
    # The encoder only ever receives query and memory texts, never stored history payloads.
    embedded = [text for texts, _ in external.memory_external_embedding_client.calls for text in texts]
    assert "最爱的提神饮料是什么" in embedded


async def test_external_embedding_failures_fall_back_to_keyword(db, seeded_scope, external):
    from agi_wind.memory.semantic import index_semantic, semantic_status
    coffee, _ = fact(db, seeded_scope, "我喜欢咖啡")

    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("synthetic connect failure")

    external.memory_external_embedding_client.embed = unavailable
    assert await index_semantic(db, seeded_scope) == {"indexed": 0, "status": "external_unavailable"}
    assert semantic_status(db) == "external_unavailable"
    service = MemoryService(db)
    assert any(hit.memory_id == coffee for hit in await service.search(seeded_scope, "咖啡"))
    debug = await service.recall_debug(seeded_scope, "咖啡")
    assert debug and not any("semantic_" in route for item in debug for route in item["routes"])


async def test_external_embedding_dimension_mismatch_falls_back(db, seeded_scope, external):
    from agi_wind.memory.semantic import index_semantic, semantic_status
    coffee, _ = fact(db, seeded_scope, "我喜欢咖啡")

    async def wrong_dimension(texts, query=False):
        return [[1.0, 0.0] for _ in texts]

    external.memory_external_embedding_client.embed = wrong_dimension
    assert await index_semantic(db, seeded_scope) == {"indexed": 0, "status": "external_unavailable"}
    assert semantic_status(db) == "external_unavailable"
    assert any(hit.memory_id == coffee for hit in await MemoryService(db).search(seeded_scope, "咖啡"))


async def test_unconfigured_external_embedding_keeps_keyword_only_behavior(db, seeded_scope, tmp_path):
    from agi_wind.core.settings import Settings
    from agi_wind.memory.semantic import index_semantic, semantic_status
    coffee, _ = fact(db, seeded_scope, "我喜欢咖啡")
    db.memory_settings = Settings(data_dir=tmp_path / "empty" / "data",
                                  access_token="synthetic-memory-token")
    assert semantic_status(db) == "keyword_only"
    assert await index_semantic(db, seeded_scope) == {"indexed": 0, "status": "keyword_only"}
    assert semantic_status(db) == "keyword_only"
    assert any(hit.memory_id == coffee for hit in await MemoryService(db).search(seeded_scope, "咖啡"))


async def test_semantic_master_switch_disables_external_embedding(db, seeded_scope, external):
    from agi_wind.memory.semantic import index_semantic, semantic_status
    coffee, _ = fact(db, seeded_scope, "我喜欢咖啡")
    external.memory_settings.memory_semantic_enabled = False
    assert semantic_status(db) == "keyword_only"
    assert await index_semantic(db, seeded_scope) == {"indexed": 0, "status": "keyword_only"}
    assert any(hit.memory_id == coffee for hit in await MemoryService(db).search(seeded_scope, "咖啡"))


async def test_rerank_reorders_search_hits_and_only_sends_query_and_candidates(db, seeded_scope, external):
    fact(db, seeded_scope, "合成检索甲")
    fact(db, seeded_scope, "合成检索乙")
    await MemoryService(db).index_events(seeded_scope)
    service = MemoryService(db)
    baseline = await service.search(seeded_scope, "合成检索")
    assert len(baseline) >= 2
    reranker = FakeReranker()
    db.memory_rerank_client = reranker
    db._memory_rerank_retry_at = 0
    reranked = await service.search(seeded_scope, "合成检索")
    assert hit_ids(reranked[:len(baseline)]) == list(reversed(hit_ids(baseline[:len(baseline)])))
    query, documents = reranker.calls[0]
    assert query == "合成检索"
    assert documents == [str(hit.content)[:800] for hit in baseline]
    debug = await service.recall_debug(seeded_scope, "合成检索")
    assert any("external_rerank" in item["routes"] for item in debug)


async def test_rerank_timeout_keeps_baseline_order_and_breaker(db, seeded_scope, external):
    fact(db, seeded_scope, "合成检索甲")
    fact(db, seeded_scope, "合成检索乙")
    await MemoryService(db).index_events(seeded_scope)
    service = MemoryService(db)
    baseline = await service.search(seeded_scope, "合成检索")
    reranker = FakeReranker(error=asyncio.TimeoutError())
    db.memory_rerank_client = reranker
    db._memory_rerank_retry_at = 0
    assert await service.search(seeded_scope, "合成检索") == baseline
    # The breaker prevents a second timeout inside the same chat turn window.
    assert await service.search(seeded_scope, "合成检索") == baseline
    assert len(reranker.calls) == 1


async def test_memory_settings_api_masks_secret_and_toggles_external_features(db, actor, tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.delivery.service import GenerationService
    settings = external_settings(tmp_path)
    app = create_app(settings, db, actor, GenerationService(db, None, model=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": "Bearer synthetic-memory-token"}) as client:
        view = await client.get("/api/v1/memory/settings")
        assert view.status_code == 200
        body = view.json()
        assert body["external_embedding"]["status"] == "ready"
        assert body["external_embedding"]["enabled"] is True
        assert body["external_embedding"]["credential_configured"] is True
        assert SECRET not in view.text
        providers = await client.get("/api/v1/settings/providers")
        assert providers.status_code == 200 and SECRET not in providers.text
        patched = await client.patch("/api/v1/memory/settings", json={
            "external_embedding_enabled": False, "rerank_enabled": False})
        assert patched.status_code == 200
        assert patched.json()["external_embedding"]["enabled"] is False
        assert patched.json()["rerank"]["enabled"] is False
        saved = (tmp_path / "data" / "settings.json").read_text(encoding="utf-8")
        assert SECRET not in saved
        persisted = json.loads(saved)["memory"]
        assert persisted["memory_external_embedding_enabled"] is False
        assert persisted["memory_rerank_enabled"] is False
        current = (await client.get("/api/v1/memory/settings")).json()
        assert current["external_embedding"]["enabled"] is False
        assert current["rerank"]["enabled"] is False


async def test_provider_probe_routes_gate_capability_and_credential(db, actor, tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.delivery.service import GenerationService
    from agi_wind.core.settings import Settings
    settings = Settings.model_validate({
        "data_dir": str(tmp_path / "probe-data"),
        "access_token": "synthetic-memory-token",
        "providers": {
            "chat": {"base_url": "https://vendor.invalid/v1", "credential_ref": "CHAT_KEY",
                     "capabilities": ["text"], "supported_parameters": []},
            "nokey": {"base_url": "https://vendor.invalid/v1", "credential_ref": "MISSING_KEY",
                      "capabilities": ["embedding", "rerank"], "supported_parameters": []},
        },
    })
    app = create_app(settings, db, actor, GenerationService(db, None, model=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"Authorization": "Bearer synthetic-memory-token"}) as client:
        assert (await client.post("/api/v1/settings/providers/chat/test-embedding",
                                  json={"model_id": "m"})).status_code == 422
        assert (await client.post("/api/v1/settings/providers/chat/test-rerank",
                                  json={"model_id": "m"})).status_code == 422
        assert (await client.post("/api/v1/settings/providers/nokey/test-embedding",
                                  json={"model_id": "m"})).status_code == 409
        assert (await client.post("/api/v1/settings/providers/nokey/test-rerank",
                                  json={"model_id": "m"})).status_code == 409
        assert (await client.post("/api/v1/settings/providers/missing/test-embedding",
                                  json={"model_id": "m"})).status_code == 404
