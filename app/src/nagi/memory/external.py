"""Optional external embedding provider (OpenAI-compatible ``POST /v1/embeddings``).

The local BGE projection remains the default and is preferred whenever its
deployment files exist. This module is only used when no local deployment is
configured and an operator explicitly enabled an external provider in settings.

Only the bounded retrieval query / candidate text leaves the machine. Stored
memory history, provenance, logs and the API key are never part of a payload or
of a status view. Every failure raises and lets the caller keep deterministic
keyword/local ordering; retrieval is never blocked on this provider.
"""
import hashlib
import math

import httpx

from .embedding import EmbeddingSpace
from ..core.secrets import SecretStore

EXTERNAL_REVISION = "external-v1"
DEFAULT_DIMENSION = 512


def _provider(settings, role="embedding"):
    """Return (model, provider_config, credential_ref) for a configured role or None."""
    if settings is None:
        return None
    models = getattr(settings, "models", None)
    model = getattr(models, role, None) if models is not None else None
    if model is None or getattr(model, "provider_id", None) in (None, "local"):
        return None
    providers = getattr(settings, "providers", None) or {}
    provider = providers.get(model.provider_id)
    capabilities = getattr(provider, "capabilities", None) if provider is not None else None
    if provider is None or (capabilities is not None and role not in capabilities):
        return None
    return model, provider


def external_embedding_config(settings):
    """Connection config for the external embedding role, or None when not usable.

    Missing settings (disabled flag, no role, provider without the embedding
    capability, absent credential) all degrade to None; retrieval then reports
    keyword-only and never attempts a network call.
    """
    if settings is None or not getattr(settings, "memory_external_embedding_enabled", False):
        return None
    # The semantic master switch also governs this provider; disabling semantic
    # retrieval must not leave vector work running in the worker.
    if not getattr(settings, "memory_semantic_enabled", True):
        return None
    configured = _provider(settings, "embedding")
    if configured is None:
        return None
    model, provider = configured
    try:
        credential = SecretStore(getattr(settings, "data_dir")).get(provider.credential_ref)
    except (OSError, ValueError, TypeError):
        return None
    if not credential:
        return None
    dimension = int(getattr(settings, "memory_external_embedding_dimension", DEFAULT_DIMENSION) or DEFAULT_DIMENSION)
    return {
        "base_url": provider.base_url,
        "model_id": model.model_id,
        "credential": credential,
        "dimension": dimension,
        "timeout_seconds": min(float(provider.timeout_seconds), 30.0),
        "parameters": dict(getattr(model, "parameters", None) or {}),
    }


def external_embedding_status(settings):
    """Config-only status view; never includes the credential and never calls the provider."""
    status = {
        "enabled": bool(getattr(settings, "memory_external_embedding_enabled", False)),
        "provider_id": None,
        "model_id": None,
        "dimension": int(getattr(settings, "memory_external_embedding_dimension", DEFAULT_DIMENSION) or DEFAULT_DIMENSION),
        "credential_configured": False,
        "status": "disabled",
    }
    if not status["enabled"]:
        return status
    if not getattr(settings, "memory_semantic_enabled", True):
        status["status"] = "semantic_disabled"
        return status
    configured = _provider(settings, "embedding")
    if configured is None:
        status["status"] = "unconfigured"
        return status
    model, provider = configured
    status["provider_id"] = model.provider_id
    status["model_id"] = model.model_id
    try:
        configured_secret = SecretStore(getattr(settings, "data_dir")).configured(provider.credential_ref)
    except (OSError, ValueError, TypeError):
        configured_secret = False
    status["credential_configured"] = configured_secret
    status["status"] = "ready" if configured_secret else "credential_missing"
    return status


def _space_identity(base_url, model_id, dimension, parameters):
    material = "\n".join((base_url.rstrip("/"), model_id, str(dimension),
                          repr(sorted((parameters or {}).items()))))
    return hashlib.sha256(material.encode()).hexdigest()


class ExternalEmbeddingClient:
    """OpenAI-compatible embedding client with a stable projection identity.

    ``space`` describes the projection that stored vectors belong to; the
    configured dimension is intentional, so a provider returning a different
    size is a mismatch (and a fallback) rather than a silent re-projection.
    """

    def __init__(self, base_url, model_id, credential, dimension=DEFAULT_DIMENSION,
                 timeout_seconds=8.0, parameters=None, transport=None):
        self.base_url = str(base_url).rstrip("/")
        self.model_id = str(model_id)
        self._credential = credential
        self._transport = transport
        self.timeout_seconds = min(float(timeout_seconds or 8.0), 60.0)
        self.parameters = dict(parameters or {})
        identity = _space_identity(self.base_url, self.model_id, int(dimension), self.parameters)
        self.space = EmbeddingSpace(
            model_name=self.model_id, revision=EXTERNAL_REVISION,
            weights_hash=identity, tokenizer_hash=identity,
            dimension=int(dimension), normalize=True, preprocessing="external-embedding-v1")

    async def embed(self, texts, query=False):
        texts = list(texts)
        if not texts:
            return []
        payload = {"model": self.model_id, "input": texts}
        # Only forward dimensions when explicitly configured; many providers do
        # not accept the parameter, and the configured space dimension already
        # defines the projection identity above.
        if "dimensions" in self.parameters:
            payload["dimensions"] = int(self.parameters["dimensions"])
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False,
                                     transport=self._transport) as client:
            reply = await client.post(
                self.base_url + "/embeddings",
                headers={"Authorization": "Bearer " + self._credential},
                json=payload)
            reply.raise_for_status()
            result = reply.json()
        rows = result.get("data") if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise ValueError("invalid external embeddings response")
        # OpenAI returns an index for each row; sort defensively when present.
        if all(isinstance(row, dict) and isinstance(row.get("index"), int) for row in rows):
            rows = sorted(rows, key=lambda row: row["index"])
        vectors = []
        for row in rows:
            embedding = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(embedding, list) or len(embedding) != self.space.dimension:
                raise ValueError("external embedding dimension mismatch")
            vector = [float(value) for value in embedding]
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("invalid external embedding values")
            vectors.append(vector)
        return vectors
