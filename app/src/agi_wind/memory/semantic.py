"""Disposable local-CPU vector projection. Authority and provenance stay in PostgreSQL.

No model imports/downloads occur in the Gateway. All model I/O requires an explicit
settings attachment and the two local deployment files. Ranking is deterministic
cosine ranking, not a neural cross-encoder.
"""
import asyncio
import hashlib
import json
import math
import struct
import time
import threading
from collections import OrderedDict
from pathlib import Path

import httpx
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from . import models as mm
from .access import (conversation_pool_clause, item_pool_clause, ref_visible_in_scene)
from .embedding import EmbeddingSpace
from .lifecycle import now_utc
from ..archive import models as m
from ..archive.repository import digest, lock
from ..identity.service import authorize

DIMENSION = 512
_ANN_LOCK = threading.Lock()
_ANN_PAGE = 512


def active_clause(reference=None):
    reference = reference or now_utc()
    a = mm.memory_items.c
    return and_(a.status == "active", a.epistemic_type != "fictional",
                or_(a.valid_from.is_(None), a.valid_from <= reference),
                or_(a.valid_to.is_(None), a.valid_to > reference),
                or_(a.is_pinned.is_(True), a.expires_at.is_(None), a.expires_at > reference))


def item_scope(scope):
    a = mm.memory_items.c
    return and_(a.account_id == scope.account_id, a.companion_id == scope.companion_id,
                or_(a.user_id == scope.user_id, a.user_id.is_(None)), item_pool_clause(scope))


def event_scope(scope):
    e, v = m.events.c, m.conversations.c
    return and_(e.account_id == scope.account_id, e.companion_id == scope.companion_id,
                e.visibility == "visible", v.account_id == scope.account_id,
                v.companion_id == scope.companion_id, v.user_id == scope.user_id,
                v.state != "hidden", conversation_pool_clause(scope))


def blocked_sources(scope):
    """Long-term raw recall must not reintroduce a hidden/closed/fictional fact.

    Recent visible conversation is intentionally a separate context channel.
    Blocking a whole source is conservative when a source supports several facts.
    """
    return select(mm.source_refs.c.event_id).join(mm.memory_items).where(
        item_scope(scope), ~active_clause())


def indexable_sources_clause(scope):
    """Exclude ineligible source rows before LIMIT so they cannot starve a worker.

    This SQL gate is followed by complete source-bound Python verification; it is
    not a substitute for that verification or for the post-encoder recheck.
    """
    r, e, v = mm.source_refs.alias("index_ref"), m.events.alias("index_event"), m.conversations.alias("index_conversation")
    text = e.c.content["text"].astext
    quote = case((r.c.start_offset.is_(None), text), else_=func.substring(text,r.c.start_offset+1,r.c.end_offset-r.c.start_offset))
    valid = and_(e.c.account_id==scope.account_id,e.c.companion_id==scope.companion_id,
        e.c.visibility=="visible", v.c.account_id==scope.account_id,v.c.companion_id==scope.companion_id,
        v.c.user_id==scope.user_id,v.c.state!="hidden",r.c.json_pointer=="/text",
        or_(and_(r.c.start_offset.is_(None),r.c.end_offset.is_(None)),
            and_(r.c.start_offset>=0,r.c.end_offset>r.c.start_offset,r.c.end_offset<=func.length(text))),
        or_(r.c.quote_hash.is_(None),r.c.quote_hash==func.encode(func.sha256(func.convert_to(quote,"UTF8")),"hex")))
    if getattr(scope,'scene','owner_private')=='group':
        valid=and_(valid,v.c.id==scope.conversation_id)
    else:
        owner=exists(select(m.external_principals.c.id).where(
            m.external_principals.c.id==e.c.sender_principal_id,m.external_principals.c.is_owner.is_(True)))
        valid=and_(valid,or_(v.c.channel_kind!='group',owner))
    source_rows = select(r.c.id).select_from(r.join(e,r.c.event_id==e.c.id).join(v,e.c.conversation_id==v.c.id)).where(r.c.memory_id==mm.memory_items.c.id)
    summary_blocked = exists(select(r.c.id).where(r.c.memory_id==mm.memory_items.c.id,
        r.c.event_id.in_(blocked_sources(scope))))
    # A memory whose conversation was permanently deleted has no provenance rows
    # left. It is still a valid standalone memory, so do not filter it out; only
    # memories that still have source rows must pass full source verification.
    has_sources = exists(source_rows)
    return and_(or_(~has_sources, ~exists(source_rows.where(valid.is_not(True)))),
                or_(mm.memory_items.c.summary_level=="",~summary_blocked))


def verified_refs(conn, scope, memories):
    """Batch verification; never authorize a source merely through a vector ID."""
    if not memories:
        return {}
    ids = [row["id"] for row in memories]
    rows = list(conn.execute(select(mm.source_refs, m.events.c.content,
        m.events.c.content_sha256, m.events.c.visibility,
        m.events.c.account_id, m.events.c.companion_id, m.events.c.conversation_id,
        m.events.c.sender_principal_id, m.conversations.c.user_id,
        m.conversations.c.channel_kind, m.conversations.c.state.label("conversation_state"),
        m.external_principals.c.is_owner.label("sender_is_owner"))
        .join(m.events, mm.source_refs.c.event_id == m.events.c.id)
        .join(m.conversations, m.events.c.conversation_id == m.conversations.c.id)
        .outerjoin(m.external_principals, m.events.c.sender_principal_id == m.external_principals.c.id)
        .where(mm.source_refs.c.memory_id.in_(ids))).mappings())
    by_id, invalid = {}, set()
    for ref in rows:
        mid = ref["memory_id"]
        text = ref["content"].get("text", "")
        start, end = ref["start_offset"], ref["end_offset"]
        valid_offsets = ((start is None and end is None) or
                         (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)))
        quote = text if start is None else text[start:end]
        if (ref["account_id"] != scope.account_id or ref["companion_id"] != scope.companion_id
            or ref["user_id"] != scope.user_id or ref["visibility"] != "visible"
            or ref["conversation_state"] == "hidden" or ref["json_pointer"] != "/text"
            or not ref_visible_in_scene(scope, dict(ref))
            or not valid_offsets or digest(ref["content"]) != ref["content_sha256"]
            or (ref["quote_hash"] and hashlib.sha256(quote.encode()).hexdigest() != ref["quote_hash"])):
            invalid.add(mid)
        by_id.setdefault(mid, []).append(dict(ref))
    # Summaries inherit every source's suppression boundary, including facts that
    # were valid when the summary was written and have since become inactive.
    summary_events = {ref["event_id"] for row in memories if row.get("summary_level")
                      for ref in by_id.get(row["id"], [])}
    blocked = set(conn.scalars(blocked_sources(scope).where(mm.source_refs.c.event_id.in_(summary_events)))) if summary_events else set()
    return {row["id"]: by_id[row["id"]] for row in memories
            if row["id"] in by_id and row["id"] not in invalid
            and not (row.get("summary_level") and any(ref["event_id"] in blocked for ref in by_id[row["id"]]))}


def memory_hash(row, refs):
    return hashlib.sha256(row["content"].encode()).hexdigest()


def source_binding(refs):
    return digest(sorted(
        [(str(ref["event_id"]), ref["content_sha256"], ref["json_pointer"],
          ref["start_offset"], ref["end_offset"], ref["quote_hash"]) for ref in refs]))


def chunk_valid(row):
    text = row["event_content"].get("text", "")
    return (row["source_hash"] == row["content_sha256"] == digest(row["event_content"])
            and row["content_hash"] == digest(row["content"])
            and text[row["start_offset"]:row["end_offset"]] == row["content"])


def chunk_query(scope):
    c, e = mm.retrieval_chunks, m.events
    return select(c, e.c.content.label("event_content"), e.c.content_sha256,
                  e.c.role, e.c.kind, e.c.occurred_at, e.c.recorded_at).join(
        e, c.c.source_id == e.c.id).join(m.conversations, e.c.conversation_id == m.conversations.c.id).where(
        c.c.account_id == scope.account_id, c.c.companion_id == scope.companion_id,
        event_scope(scope), ~c.c.source_id.in_(blocked_sources(scope)),
        c.c.source_hash == e.c.content_sha256)


def _deployment(db):
    settings = getattr(db, "memory_settings", None)
    if settings is None or not getattr(settings, "memory_semantic_enabled", False):
        return None
    root = Path(settings.data_dir).resolve().parent
    config, token = root / "config/memory.local.json", root / "data/memory-token.txt"
    if not config.is_file() or not token.is_file():
        return None
    return config, token


def semantic_status(db):
    """Cheap, no-I/O status: pending means deployed but not yet probed this process."""
    if not _deployment(db):
        return "keyword_only"
    return getattr(db, "_memory_semantic_state", "local_pending")


class LocalClient:
    def __init__(self, config_path, token_path):
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if config.get("url", "http://127.0.0.1:8141") != "http://127.0.0.1:8141":
            raise ValueError("local memory endpoint must be loopback port 8141")
        self.space = EmbeddingSpace(**config["space"])
        self.token = token_path.read_text(encoding="utf-8-sig").strip()
        if len(self.token) < 32 or self.space.dimension != DIMENSION or not self.space.normalize:
            raise ValueError("invalid local memory deployment")

    async def embed(self, texts, query=False):
        async with httpx.AsyncClient(trust_env=False, timeout=0.8 if query else 20.0) as client:
            reply = await client.post("http://127.0.0.1:8141/embed",
                headers={"Authorization": "Bearer " + self.token},
                json={"texts": texts, "query": query, "fingerprint": self.space.fingerprint})
            reply.raise_for_status()
            result = reply.json()
        if result["fingerprint"] != self.space.fingerprint:
            raise ValueError("model fingerprint changed; explicit setup required")
        return result["vectors"]


def _client(db):
    deployment = _deployment(db)
    if not deployment:
        return None
    # Explicit dependency injection for isolated encoders; still obey deployment gate.
    return getattr(db, "memory_semantic_client", None) or LocalClient(*deployment)


def pack_vector(vector):
    if len(vector) != DIMENSION or not all(math.isfinite(float(v)) for v in vector):
        raise ValueError("invalid local embedding")
    norm = math.sqrt(sum(float(v)*float(v) for v in vector))
    if norm < 1e-12:
        raise ValueError("zero local embedding")
    return struct.pack("<512f", *(float(v)/norm for v in vector))


def rank_vectors(vector, rows, limit=80):
    """Bounded deterministic cosine ranking; callable in a CPU thread."""
    q = struct.unpack("<512f", pack_vector(vector))
    scored = []
    for key, raw in rows:
        if len(raw) != DIMENSION*4:
            continue
        values = struct.unpack("<512f", raw)
        score = sum(a*b for a, b in zip(q, values))
        if math.isfinite(score) and score >= 0.35:
            scored.append((key, score))
    return sorted(scored, key=lambda pair: (-pair[1], str(pair[0])))[:limit]


async def index_semantic(db, scope, limit=64):
    """Bounded incremental projection; model work runs outside every DB transaction."""
    limit = max(1, min(64, int(limit)))
    if not _deployment(db):
        return {"indexed": 0, "status": "keyword_only"}
    try:
        client = _client(db)
        space = client.space
        with db.engine.begin() as conn:
            companion = authorize(conn, scope, scope.companion_id)
            revision = companion["memory_revision"]
            sid = conn.scalar(select(mm.embedding_spaces.c.id).where(mm.embedding_spaces.c.fingerprint == space.fingerprint))
            # revision is an object revision, not the unrelated companion revision.
            memory_current = exists(select(mm.memory_vectors.c.id).where(
                mm.memory_vectors.c.memory_id == mm.memory_items.c.id,
                mm.memory_vectors.c.space_id == sid,
                mm.memory_vectors.c.content_hash == func.encode(func.sha256(func.convert_to(mm.memory_items.c.content, "UTF8")), "hex"),
                mm.memory_vectors.c.revision == mm.memory_items.c.revision)) if sid else False
            memories = list(conn.execute(select(mm.memory_items).where(item_scope(scope), active_clause(), indexable_sources_clause(scope),
                ~memory_current if sid else True).order_by(mm.memory_items.c.is_pinned.desc(),
                mm.memory_items.c.created_at, mm.memory_items.c.id).limit(limit)).mappings())
            refs = verified_refs(conn, scope, memories)
            bindings = {mid:source_binding(value) for mid,value in refs.items()}
            pending = [("memory", row, memory_hash(row, refs[row["id"]])) for row in memories if row["id"] in refs]
            chunk_current = exists(select(mm.chunk_vectors.c.id).where(
                mm.chunk_vectors.c.chunk_id == mm.retrieval_chunks.c.id,
                mm.chunk_vectors.c.space_id == sid,
                mm.chunk_vectors.c.content_hash == mm.retrieval_chunks.c.content_hash)) if sid else False
            chunks = list(conn.execute(chunk_query(scope).where(~chunk_current if sid else True)
                .order_by(m.events.c.recorded_at, mm.retrieval_chunks.c.id).limit(limit-len(pending))).mappings())
            pending.extend(("chunk", row, row["content_hash"]) for row in chunks if chunk_valid(row))
        if not pending:
            # Probe without embedding user content, to avoid declaring a stopped model ready.
            await asyncio.wait_for(client.embed(["状态检查"]), timeout=21)
            db._memory_semantic_state = "semantic_local"
            return {"indexed": 0, "status": "semantic_local"}
        # Keep each HTTP batch inside the model service's character/body bounds,
        # even when an imported chunk uses long ASCII text.
        vectors, batch = [], []
        for _, row, _ in pending:
            if batch and (len(batch) >= 16 or sum(len(text) for text in batch)+len(row["content"]) > 30000):
                vectors.extend(await asyncio.wait_for(client.embed(batch), timeout=21))
                batch = []
            batch.append(row["content"])
        if batch:
            vectors.extend(await asyncio.wait_for(client.embed(batch), timeout=21))
        if len(vectors) != len(pending):
            raise ValueError("invalid embedding count")
        packed = [pack_vector(v) for v in vectors]
        count = 0
        with db.engine.begin() as conn:
            lock(conn, scope.companion_id)
            if authorize(conn, scope, scope.companion_id)["memory_revision"] != revision:
                return {"indexed": 0, "status": "local_pending", "reason": "revision_changed"}
            sid = conn.scalar(insert(mm.embedding_spaces).values(fingerprint=space.fingerprint,
                config=space.model_dump(), status="ready", checkpoint={}).on_conflict_do_update(
                index_elements=[mm.embedding_spaces.c.fingerprint], set_={"status": "ready"}).returning(mm.embedding_spaces.c.id))
            fresh_memories = list(conn.execute(select(mm.memory_items).where(item_scope(scope), active_clause(),
                mm.memory_items.c.id.in_([r["id"] for kind,r,_ in pending if kind == "memory"]))).mappings())
            fresh_refs = verified_refs(conn, scope, fresh_memories)
            current = {r["id"]:r for r in fresh_memories if r["id"] in fresh_refs}
            current_chunks = {r["id"]:r for r in conn.execute(chunk_query(scope).where(
                mm.retrieval_chunks.c.id.in_([r["id"] for kind,r,_ in pending if kind == "chunk"]))).mappings() if chunk_valid(r)}
            for (kind, row, content_hash), raw in zip(pending, packed):
                fresh = (current if kind == "memory" else current_chunks).get(row["id"])
                if fresh is None or fresh["revision"] != row["revision"]:
                    continue
                if kind == "memory" and source_binding(fresh_refs[fresh["id"]]) != bindings[row["id"]]:
                    continue
                expected = memory_hash(fresh, fresh_refs[fresh["id"]]) if kind == "memory" else fresh["content_hash"]
                if expected != content_hash:
                    continue
                table, field = (mm.memory_vectors, "memory_id") if kind == "memory" else (mm.chunk_vectors, "chunk_id")
                values = {field:row["id"], "space_id":sid, "content_hash":content_hash,
                          "revision":row["revision"], "vector":raw}
                conn.execute(insert(table).values(**values).on_conflict_do_update(
                    index_elements=[table.c[field], table.c.space_id], set_=values))
                count += 1
            conn.execute(mm.embedding_spaces.update().where(mm.embedding_spaces.c.id == sid).values(
                checkpoint={"indexed_last_batch": count, "updated_at": now_utc().isoformat()}))
        db._memory_semantic_state = "semantic_local"
        return {"indexed": count, "status": "semantic_local", "fingerprint":space.fingerprint}
    except (httpx.HTTPError, OSError, ValueError, KeyError, TimeoutError):
        db._memory_semantic_state = "local_unavailable"
        return {"indexed": 0, "status": "local_unavailable"}


def _load_candidate_vectors(db, scope, fingerprint, *, memory_ids=None, chunk_ids=None):
    """Own the connection in this worker thread, including all row verification.

    Neither synchronous PostgreSQL I/O nor processing up to 20k candidates may
    occupy the Gateway event loop used by audio WebSockets.
    """
    with db.engine.connect() as conn:
        authorize(conn, scope, scope.companion_id)
        sid = conn.scalar(select(mm.embedding_spaces.c.id).where(mm.embedding_spaces.c.fingerprint == fingerprint))
        if not sid:
            return None
        memory_rows = list(conn.execute(select(mm.memory_items, mm.memory_vectors.c.vector,
            mm.memory_vectors.c.content_hash.label("vector_hash"), mm.memory_vectors.c.revision.label("vector_revision"))
            .join(mm.memory_vectors, mm.memory_items.c.id == mm.memory_vectors.c.memory_id)
            .where(item_scope(scope), active_clause(), indexable_sources_clause(scope), mm.memory_vectors.c.space_id == sid,
                mm.memory_items.c.id.in_(memory_ids) if memory_ids is not None else True)
            .order_by(mm.memory_items.c.id)).mappings())
        refs = verified_refs(conn, scope, memory_rows)
        memories = [(r["id"], r["vector"]) for r in memory_rows if r["id"] in refs
            and r["vector_revision"] == r["revision"] and r["vector_hash"] == memory_hash(r, refs[r["id"]])]
        chunk_rows = conn.execute(chunk_query(scope).add_columns(mm.chunk_vectors.c.vector,
            mm.chunk_vectors.c.content_hash.label("vector_hash"), mm.chunk_vectors.c.revision.label("vector_revision"))
            .join(mm.chunk_vectors, mm.retrieval_chunks.c.id == mm.chunk_vectors.c.chunk_id)
            .where(mm.chunk_vectors.c.space_id == sid,
                mm.retrieval_chunks.c.id.in_(chunk_ids) if chunk_ids is not None else True)
            .order_by(mm.retrieval_chunks.c.id)).mappings()
        chunks = [(r["id"], r["vector"]) for r in chunk_rows if chunk_valid(r)
                  and r["vector_hash"] == r["content_hash"] and r["vector_revision"] == r["revision"]]
    return memories, chunks


def _projection_metadata(db, scope, fingerprint):
    """Read compact IDs/hashes, never vector blobs or private source text.

    This remains O(n) metadata work. It detects new/removed/replaced projections
    even when an indexing batch did not bump the companion's memory revision.
    Expiry and pool/scene gates are evaluated anew on every request.
    """
    with db.engine.connect() as conn:
        revision = authorize(conn, scope, scope.companion_id)['memory_revision']
        sid = conn.scalar(select(mm.embedding_spaces.c.id).where(mm.embedding_spaces.c.fingerprint == fingerprint))
        if not sid:
            return None
        a, v = mm.memory_items, mm.memory_vectors
        memories = list(conn.execute(select(a.c.id, a.c.revision, v.c.revision, v.c.content_hash)
            .join(v, a.c.id == v.c.memory_id).where(item_scope(scope), active_clause(),
                indexable_sources_clause(scope), v.c.space_id == sid).order_by(a.c.id)))
        c, v = mm.retrieval_chunks, mm.chunk_vectors
        chunks = list(conn.execute(chunk_query(scope).with_only_columns(
            c.c.id, c.c.revision, v.c.revision, v.c.content_hash)
            .join(v, c.c.id == v.c.chunk_id).where(v.c.space_id == sid).order_by(c.c.id)))
    signature = hashlib.sha256(repr((revision, memories, chunks)).encode()).digest()
    return signature, [r[0] for r in memories], [r[0] for r in chunks]


def _ann_candidates(db, scope, fingerprint, vector):
    from .ann import VectorIndex
    # Serialize native index construction/search and keep cache ownership on db.
    # Four scenes bound resident graph count. The LRU never owns source text.
    key = (scope.account_id, scope.companion_id, scope.user_id,
           getattr(scope, 'scene', 'owner_private'), getattr(scope, 'room_id', None),
           scope.conversation_id, fingerprint)
    with _ANN_LOCK:
        metadata = _projection_metadata(db, scope, fingerprint)
        if metadata is None:
            return None
        signature, memory_ids, chunk_ids = metadata
        cache = getattr(db, '_memory_ann_cache', None)
        if cache is None:
            cache = db._memory_ann_cache = OrderedDict()
        cached = cache.get(key)
        if cached is None or cached[0] != signature:
            indexes = (VectorIndex(), VectorIndex())
            # No MAX_POOL truncation: load and verify the entire eligible vector
            # projection in bounded pages, adding each page into the native graph.
            for start in range(0, max(len(memory_ids), len(chunk_ids)), _ANN_PAGE):
                loaded = _load_candidate_vectors(db, scope, fingerprint,
                    memory_ids=memory_ids[start:start+_ANN_PAGE],
                    chunk_ids=chunk_ids[start:start+_ANN_PAGE])
                if loaded is None:
                    return None
                for index, rows in zip(indexes, loaded):
                    index.add(rows)
            cache[key] = (signature, indexes)
            while len(cache) > 4:
                cache.popitem(last=False)
        else:
            indexes = cached[1]
        cache.move_to_end(key)
        # ANN is only candidate selection. Re-fetch current vectors and all original
        # provenance/visibility/revision gates before computing the final cosine.
        # Expand when rejected sources would otherwise starve lower-ranked facts.
        count = 256
        while True:
            selected = [index.search(vector, count) for index in indexes]
            loaded = _load_candidate_vectors(db, scope, fingerprint,
                memory_ids=selected[0], chunk_ids=selected[1])
            if loaded is None:
                return None
            ranks = tuple(rank_vectors(vector, rows) for rows in loaded)
            if all(len(rank) >= 80 or count >= index.size
                   for rank, index in zip(ranks, indexes)):
                return ranks
            # Low similarity is not source rejection and should not force a full
            # graph scan. Expansion is solely to compensate failed verification.
            if all(len(rows) == len(ids) for rows, ids in zip(loaded, selected)):
                return ranks
            count *= 2


async def semantic_candidates(db, scope, query):
    if not _deployment(db) or not query.strip():
        return {}, {}
    # A failing service gets a short circuit breaker so voice/chat never queues a
    # fresh timeout for every turn. Worker indexing independently probes recovery.
    if time.monotonic() < getattr(db, "_memory_semantic_retry_at", 0):
        return {}, {}
    try:
        client = _client(db)
        vectors = await asyncio.wait_for(client.embed([query[:4096]], query=True), timeout=1.0)
        if len(vectors) != 1:
            raise ValueError("invalid query vector")
        ranks = await asyncio.to_thread(_ann_candidates, db, scope, client.space.fingerprint, vectors[0])
        if ranks is None:
            db._memory_semantic_state = "local_pending"
            return {}, {}
        db._memory_semantic_state = "semantic_local"
        return dict(ranks[0]), dict(ranks[1])
    except (httpx.HTTPError, OSError, ValueError, KeyError, TimeoutError, ImportError, RuntimeError):
        db._memory_semantic_state = "local_unavailable"
        db._memory_semantic_retry_at = time.monotonic()+10
        return {}, {}
