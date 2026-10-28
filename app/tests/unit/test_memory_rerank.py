"""External rerank: reorder, bounded payload and fail-open fallbacks.

Every case uses synthetic hits and httpx.MockTransport; no database or network.
"""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from nagi.core.secrets import SecretStore
from nagi.core.types import MemoryHit
from nagi.memory.rerank import (
    RERANK_CANDIDATES,
    RERANK_TEXT_CHARS,
    RerankClient,
    rerank_config,
    rerank_hits,
    rerank_status,
)

SECRET = "synthetic-external-rerank-key"


def make_settings(tmp_path, enabled=True, capability="rerank", **overrides):
    from nagi.core.settings import Settings
    value = {
        "data_dir": str(tmp_path / "data"),
        "providers": {"rr": {"base_url": "https://vendor.invalid/v1", "credential_ref": "RR_KEY",
                             "capabilities": [capability], "supported_parameters": []}},
        "models": {"rerank": {"provider_id": "rr", "model_id": "rerank-test"}},
        "memory_rerank_enabled": enabled,
    }
    value.update(overrides)
    return Settings.model_validate(value)


@pytest.fixture
def configured(tmp_path):
    SecretStore(tmp_path / "data").set("RR_KEY", SECRET)
    return make_settings(tmp_path)


def hits(count=3):
    return [MemoryHit(memory_id=uuid4(), content=f"候选{index}", score=1.0 - index * 0.1,
                      epistemic_type="user_asserted", source_refs=[]) for index in range(count)]


def rerank_transport(scores, seen=None, status=200, body=None):
    def handle(request):
        if seen is not None:
            seen.append(request)
        if status >= 400:
            return httpx.Response(status, json={"error": {"message": "denied"}})
        if body is not None:
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"results": [
            {"index": index, "relevance_score": score} for index, score in enumerate(scores)]})
    return httpx.MockTransport(handle)


def client(transport):
    return RerankClient(base_url="https://vendor.invalid/v1", model_id="rerank-test",
                        credential=SECRET, timeout_seconds=2.0, transport=transport)


async def test_rerank_success_reorders_and_sends_only_query_and_candidates(configured):
    seen = []
    db = SimpleNamespace(memory_settings=configured)
    db.memory_rerank_client = client(rerank_transport([0.1, 0.9, 0.5], seen=seen))
    original = hits()
    ordered, covered = await rerank_hits(db, "用户问题", original)
    assert covered == 3
    assert [hit.content for hit in ordered] == ["候选1", "候选2", "候选0"]
    # The caller's ordering is copied, not mutated in place.
    assert [hit.content for hit in original] == ["候选0", "候选1", "候选2"]
    request = seen[0]
    assert request.url.path == "/v1/rerank"
    assert request.headers["authorization"] == "Bearer " + SECRET
    payload = json.loads(request.content)
    assert payload == {"model": "rerank-test", "query": "用户问题",
                       "documents": ["候选0", "候选1", "候选2"], "top_n": 3}
    assert SECRET not in request.content.decode()
    assert db._memory_rerank_state == "external_rerank"


async def test_rerank_accepts_data_envelope_and_keeps_unscored_suffix(tmp_path):
    db = SimpleNamespace(memory_settings=make_settings(tmp_path))
    db.memory_rerank_client = client(rerank_transport([], body={"data": [
        {"index": 2, "score": 0.8}, {"index": 0, "score": 0.4}]}))
    original = hits(4)
    ordered, covered = await rerank_hits(db, "查询", original, limit=3)
    assert covered == 3
    assert [hit.content for hit in ordered] == ["候选2", "候选0", "候选1", "候选3"]


async def test_rerank_timeout_falls_back_without_raising(tmp_path):
    class TimeoutClient:
        timeout_seconds = 2.0

        async def rerank(self, query, documents):
            raise asyncio.TimeoutError

    db = SimpleNamespace(memory_settings=make_settings(tmp_path), memory_rerank_client=TimeoutClient())
    original = hits()
    ordered, covered = await rerank_hits(db, "查询", original)
    assert covered == 0
    assert ordered == original
    assert db._memory_rerank_state == "unavailable"
    assert db._memory_rerank_retry_at > 0


async def test_rerank_401_falls_back_without_leaking_secret(tmp_path):
    db = SimpleNamespace(memory_settings=make_settings(tmp_path))
    db.memory_rerank_client = client(rerank_transport([], status=401))
    original = hits()
    ordered, covered = await rerank_hits(db, "查询", original)
    assert covered == 0
    assert ordered == original
    assert SECRET not in repr(db.__dict__)


async def test_rerank_malformed_response_falls_back(tmp_path):
    db = SimpleNamespace(memory_settings=make_settings(tmp_path))
    db.memory_rerank_client = client(rerank_transport([], body={"results": [{"index": 0}]}))
    original = hits()
    ordered, covered = await rerank_hits(db, "查询", original)
    assert covered == 0
    assert ordered == original
    # Out-of-range index is malformed too.
    db.memory_rerank_client = client(rerank_transport([], body={"results": [
        {"index": 9, "relevance_score": 0.5}, {"index": 1, "relevance_score": 0.7}]}))
    ordered, covered = await rerank_hits(db, "查询", original)
    assert covered == 0
    assert ordered == original


async def test_rerank_unconfigured_returns_original_order_without_network(tmp_path):
    db = SimpleNamespace(memory_settings=make_settings(tmp_path, enabled=False))
    original = hits()
    ordered, covered = await rerank_hits(db, "查询", original)
    assert covered == 0
    assert ordered == original
    assert not hasattr(db, "_memory_rerank_retry_at")


async def test_rerank_truncates_candidates_and_documents(tmp_path):
    seen = []
    db = SimpleNamespace(memory_settings=make_settings(tmp_path))
    db.memory_rerank_client = client(rerank_transport([0.5] * RERANK_CANDIDATES, seen=seen))
    original = [MemoryHit(memory_id=uuid4(), content="长" * 1000, score=1.0 - index * 0.01,
                          epistemic_type="user_asserted", source_refs=[]) for index in range(40)]
    ordered, covered = await rerank_hits(db, "查询", original)
    payload = json.loads(seen[0].content)
    assert covered == RERANK_CANDIDATES
    assert len(payload["documents"]) == RERANK_CANDIDATES
    assert all(len(document) == RERANK_TEXT_CHARS for document in payload["documents"])
    assert [hit.content for hit in ordered[-8:]] == [hit.content for hit in original[-8:]]


async def test_rerank_retry_breaker_skips_repeated_failures(tmp_path):
    class FailingClient:
        timeout_seconds = 2.0

        def __init__(self):
            self.calls = 0

        async def rerank(self, query, documents):
            self.calls += 1
            raise RuntimeError("synthetic failure")

    injected = FailingClient()
    db = SimpleNamespace(memory_settings=make_settings(tmp_path), memory_rerank_client=injected)
    original = hits()
    await rerank_hits(db, "查询", original)
    assert injected.calls == 1
    await rerank_hits(db, "查询", original)
    assert injected.calls == 1  # circuit breaker avoids another timeout in chat


def test_rerank_config_and_status_never_expose_secret(configured):
    config = rerank_config(configured)
    assert config["model_id"] == "rerank-test"
    assert config["credential"] == SECRET
    status = rerank_status(configured)
    assert status == {"enabled": True, "provider_id": "rr", "model_id": "rerank-test",
                      "credential_configured": True, "status": "ready"}
    assert SECRET not in json.dumps(status, ensure_ascii=False)
    assert "credential_ref" not in status and "credential_last4" not in status
    root = configured.data_dir.parent
    assert rerank_config(make_settings(root, enabled=False)) is None
    assert rerank_config(make_settings(root, capability="text")) is None
    assert rerank_config(make_settings(root, data_dir=str(root / "empty-data"))) is None
    unset = make_settings(root, models={})
    assert rerank_config(unset) is None and rerank_status(unset)["status"] == "unconfigured"
