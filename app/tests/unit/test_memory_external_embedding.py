"""External embedding provider: HTTP shape, dimension validation and secret hygiene.

No database and no real provider: every request is served by httpx.MockTransport.
"""
import json

import httpx
import pytest

from nagi.core.secrets import SecretStore
from nagi.memory.external import (
    DEFAULT_DIMENSION,
    ExternalEmbeddingClient,
    external_embedding_config,
    external_embedding_status,
)

SECRET = "synthetic-external-embedding-key"


def make_settings(tmp_path, **overrides):
    from nagi.core.settings import Settings
    value = {
        "data_dir": str(tmp_path / "data"),
        "providers": {"ext": {"base_url": "https://vendor.invalid/v1", "credential_ref": "EXT_KEY",
                              "capabilities": ["embedding"], "supported_parameters": ["dimensions"]}},
        "models": {"embedding": {"provider_id": "ext", "model_id": "text-embedding-test"}},
        "memory_external_embedding_enabled": True,
    }
    value.update(overrides)
    return Settings.model_validate(value)


@pytest.fixture
def configured(tmp_path):
    SecretStore(tmp_path / "data").set("EXT_KEY", SECRET)
    return make_settings(tmp_path)


def transport(dimension=4, status=200, body=None, seen=None, count=2):
    def handle(request):
        if seen is not None:
            seen.append(request)
        if status >= 400:
            return httpx.Response(status, json={"error": {"message": "denied"}})
        if body is not None:
            return httpx.Response(200, json=body)
        return httpx.Response(200, json={"data": [
            {"index": index, "embedding": [float(index + step) for step in range(dimension)]}
            for index in range(count)]})
    return httpx.MockTransport(handle)


async def test_embedding_client_success_uses_openai_embeddings_shape():
    seen = []
    client = ExternalEmbeddingClient(base_url="https://vendor.invalid/v1", model_id="text-embedding-test",
                                     credential=SECRET, dimension=4, transport=transport(seen=seen))
    vectors = await client.embed(["alpha", "beta"])
    assert [len(vector) for vector in vectors] == [4, 4]
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/embeddings"
    assert request.headers["authorization"] == "Bearer " + SECRET
    payload = json.loads(request.content)
    assert payload == {"model": "text-embedding-test", "input": ["alpha", "beta"]}
    assert client.space.dimension == 4
    assert client.space.preprocessing == "external-embedding-v1"
    assert SECRET not in client.space.model_dump_json()


async def test_embedding_client_reports_dimension_mismatch_and_malformed_payload():
    client = ExternalEmbeddingClient(base_url="https://vendor.invalid/v1", model_id="m",
                                     credential=SECRET, dimension=4,
                                     transport=transport(dimension=3))
    with pytest.raises(ValueError, match="dimension"):
        await client.embed(["alpha", "beta"])
    malformed = ExternalEmbeddingClient(base_url="https://vendor.invalid/v1", model_id="m",
                                        credential=SECRET, dimension=4,
                                        transport=transport(body={"data": []}))
    with pytest.raises(ValueError, match="invalid"):
        await malformed.embed(["alpha", "beta"])


async def test_embedding_client_http_error_does_not_leak_credential():
    client = ExternalEmbeddingClient(base_url="https://vendor.invalid/v1", model_id="m",
                                     credential=SECRET, dimension=4,
                                     transport=transport(status=401))
    with pytest.raises(httpx.HTTPStatusError) as error:
        await client.embed(["alpha", "beta"])
    assert "401" in str(error.value)
    assert SECRET not in str(error.value)


async def test_embedding_client_forwards_configured_dimensions_parameter():
    seen = []
    client = ExternalEmbeddingClient(base_url="https://vendor.invalid/v1", model_id="m",
                                     credential=SECRET, dimension=4, parameters={"dimensions": 4},
                                     transport=transport(seen=seen))
    await client.embed(["alpha", "beta"])
    assert json.loads(seen[0].content)["dimensions"] == 4


def test_external_embedding_config_requires_flag_role_and_credential(configured):
    assert external_embedding_config(configured)["model_id"] == "text-embedding-test"
    assert external_embedding_config(configured)["credential"] == SECRET
    assert external_embedding_config(configured)["dimension"] == DEFAULT_DIMENSION
    assert external_embedding_config(None) is None
    root = configured.data_dir.parent
    assert external_embedding_config(make_settings(root, memory_external_embedding_enabled=False)) is None
    # Credential missing: configuration drops to keyword-only rather than reporting a partial provider.
    assert external_embedding_config(make_settings(root, data_dir=str(root / "empty-data"))) is None
    # Provider without the embedding capability is not usable.
    no_capability = make_settings(root, providers={"ext": {
        "base_url": "https://vendor.invalid/v1", "credential_ref": "EXT_KEY",
        "capabilities": ["text"], "supported_parameters": []}})
    assert external_embedding_config(no_capability) is None
    # The existing "local" role stays with the built-in BGE deployment.
    local = make_settings(root, providers={},
                          models={"embedding": {"provider_id": "local", "model_id": "BAAI/bge-small-zh-v1.5"}})
    assert external_embedding_config(local) is None


def test_external_embedding_respects_semantic_master_switch(configured):
    root = configured.data_dir.parent
    disabled = make_settings(root, memory_semantic_enabled=False)
    assert external_embedding_config(disabled) is None
    assert external_embedding_status(disabled)["status"] == "semantic_disabled"


def test_external_embedding_status_never_includes_secret(configured):
    status = external_embedding_status(configured)
    assert status["status"] == "ready"
    assert status["credential_configured"] is True
    assert status["provider_id"] == "ext"
    assert status["model_id"] == "text-embedding-test"
    assert status["dimension"] == DEFAULT_DIMENSION
    assert SECRET not in json.dumps(status, ensure_ascii=False)
    assert "credential_ref" not in status and "credential_last4" not in status
    root = configured.data_dir.parent
    disabled = external_embedding_status(make_settings(root, memory_external_embedding_enabled=False))
    assert disabled["status"] == "disabled"
    assert disabled["credential_configured"] is False
    missing = external_embedding_status(make_settings(root, data_dir=str(root / "empty-data")))
    assert missing["status"] == "credential_missing"
    assert missing["credential_configured"] is False
