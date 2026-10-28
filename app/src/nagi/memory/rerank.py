"""Optional external rerank provider (common ``POST /v1/rerank`` shape).

Rerank runs only after keyword/vector candidate fusion, over a bounded prefix of
already-ordered hits. Only the user query and the candidate text (truncated) are
sent: provenance, source events, stored history and logs never leave the
machine. Any missing config, timeout, HTTP error, malformed payload or
dimension-shaped mismatch is swallowed and the existing ordering is preserved,
so a chat turn is never blocked by this provider.
"""
import asyncio
import logging
import math
import time

import httpx

from ..core.secrets import SecretStore

logger = logging.getLogger(__name__)

RERANK_CANDIDATES = 32
RERANK_TEXT_CHARS = 800
RERANK_TIMEOUT_SECONDS = 2.0
RERANK_RETRY_SECONDS = 15.0


def rerank_config(settings):
    """Connection config for the external rerank role, or None when not usable."""
    if settings is None or not getattr(settings, "memory_rerank_enabled", True):
        return None
    models = getattr(settings, "models", None)
    model = getattr(models, "rerank", None) if models is not None else None
    if model is None or getattr(model, "provider_id", None) in (None, "local"):
        return None
    providers = getattr(settings, "providers", None) or {}
    provider = providers.get(model.provider_id)
    if provider is None:
        return None
    capabilities = getattr(provider, "capabilities", None)
    if capabilities is not None and "rerank" not in capabilities:
        return None
    try:
        credential = SecretStore(getattr(settings, "data_dir")).get(provider.credential_ref)
    except (OSError, ValueError, TypeError):
        return None
    if not credential:
        return None
    return {
        "base_url": provider.base_url,
        "model_id": model.model_id,
        "credential": credential,
        "timeout_seconds": min(float(provider.timeout_seconds), 10.0),
    }


def rerank_status(settings):
    """Config-only status view; never includes the credential and never calls the provider."""
    status = {
        "enabled": bool(getattr(settings, "memory_rerank_enabled", True)),
        "provider_id": None,
        "model_id": None,
        "credential_configured": False,
        "status": "disabled",
    }
    if not status["enabled"]:
        return status
    models = getattr(settings, "models", None)
    model = getattr(models, "rerank", None) if models is not None else None
    if model is None or getattr(model, "provider_id", None) in (None, "local"):
        status["status"] = "unconfigured"
        return status
    status["provider_id"] = model.provider_id
    status["model_id"] = model.model_id
    providers = getattr(settings, "providers", None) or {}
    provider = providers.get(model.provider_id)
    capabilities = getattr(provider, "capabilities", None) if provider is not None else None
    if provider is None or (capabilities is not None and "rerank" not in capabilities):
        status["status"] = "unconfigured"
        return status
    try:
        configured = SecretStore(getattr(settings, "data_dir")).configured(provider.credential_ref)
    except (OSError, ValueError, TypeError):
        configured = False
    status["credential_configured"] = configured
    status["status"] = "ready" if configured else "credential_missing"
    return status


def _scores(payload):
    """Normalize common rerank response envelopes into [(index, score)]."""
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if rows is None and isinstance(payload, dict):
        rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("invalid rerank response")
    scores = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("index"), int):
            raise ValueError("invalid rerank row")
        raw = row.get("relevance_score", row.get("score"))
        score = float(raw)
        if not math.isfinite(score):
            raise ValueError("invalid rerank score")
        scores.append((row["index"], score))
    return scores


def ranked_order(scores, count):
    """Deterministic rerank order over ``count`` candidates.

    Scored candidates come first ordered by (-score, original index); unscored
    candidates keep their original relative order after them. Returns None when
    there is no meaningful rerank signal (empty or a single unique candidate).
    """
    seen = {}
    for index, score in scores:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < count:
            raise ValueError("rerank index out of range")
        if not math.isfinite(float(score)):
            raise ValueError("invalid rerank score")
        seen[index] = max(seen.get(index, float(score)), float(score))
    if len(seen) < 2:
        return None
    order = sorted(seen, key=lambda index: (-seen[index], index))
    order.extend(index for index in range(count) if index not in seen)
    return order


class RerankClient:
    """Minimal client for the common ``POST {base}/rerank`` vendor shape."""

    def __init__(self, base_url, model_id, credential, timeout_seconds=5.0, transport=None):
        self.base_url = str(base_url).rstrip("/")
        self.model_id = str(model_id)
        self._credential = credential
        self._transport = transport
        self.timeout_seconds = min(float(timeout_seconds or 5.0), 30.0)

    async def rerank(self, query, documents):
        documents = list(documents)
        if not documents:
            return []
        payload = {"model": self.model_id, "query": query,
                   "documents": documents, "top_n": len(documents)}
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False,
                                     transport=self._transport) as client:
            reply = await client.post(
                self.base_url + "/rerank",
                headers={"Authorization": "Bearer " + self._credential},
                json=payload)
            reply.raise_for_status()
            result = reply.json()
        return _scores(result)


async def rerank_hits(db, query, hits, limit=RERANK_CANDIDATES):
    """Rerank a bounded prefix of already-ordered hits.

    Returns ``(hits, covered)`` where ``covered`` is the number of leading hits
    that received an external score (0 means the caller keeps its ordering).
    """
    settings = getattr(db, "memory_settings", None)
    client = getattr(db, "memory_rerank_client", None)
    if client is None:
        config = rerank_config(settings)
        if config is None:
            return hits, 0
        client = RerankClient(**config)
    if not query or not str(query).strip() or len(hits) < 2:
        return hits, 0
    if time.monotonic() < getattr(db, "_memory_rerank_retry_at", 0):
        return hits, 0
    candidates = list(hits[:max(2, int(limit))])
    documents = [str(hit.content)[:RERANK_TEXT_CHARS] for hit in candidates]
    timeout = min(float(getattr(client, "timeout_seconds", RERANK_TIMEOUT_SECONDS) or
                        RERANK_TIMEOUT_SECONDS), RERANK_TIMEOUT_SECONDS)
    try:
        scores = await asyncio.wait_for(client.rerank(str(query)[:4096], documents), timeout=timeout)
        order = ranked_order(scores, len(candidates))
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, TimeoutError, RuntimeError) as error:
        # Keep the failure diagnosable by type only; payloads and credentials never
        # enter logs and the caller keeps its deterministic ordering.
        logger.debug("external rerank unavailable (%s)", type(error).__name__)
        db._memory_rerank_state = "unavailable"
        db._memory_rerank_retry_at = time.monotonic() + RERANK_RETRY_SECONDS
        return hits, 0
    if order is None:
        return hits, 0
    reordered = [candidates[index] for index in order] + list(hits[len(candidates):])
    db._memory_rerank_state = "external_rerank"
    db._memory_rerank_retry_at = 0
    return reordered, len(candidates)
