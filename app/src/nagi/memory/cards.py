"""Bounded memory cards, archive, forget/merge/undo and full-text search.

This module is deliberately independent from the Gateway API layer: it imports no
FastAPI code and all persistence goes through SQLAlchemy so a thin router (or a
future CLI/worker) can call it with an authenticated ``Scope``.

A card is a deterministic, character-budgeted rendering of active ``memory_items``
for one scope. Rendering never mutates state. ``forget``/``merge`` first copy the
affected rows into ``memory_archive`` (same SQLAlchemy ``MetaData`` as the archive
models) and only then remove them; ``undo`` restores a row from its archived
snapshot.

Integration notes for the API owner
----------------------------------
* ``memory_archive`` is declared here but is NOT part of the Alembic revision in
  this repository yet. A migration must create it (see ``TABLE_DEFINITION``) or
  call ``memory_archive.create(bind=engine)`` for tests.
* Construct ``MemoryCardService(db)`` where ``db`` is the existing
  ``nagi.archive.repository.Database`` wrapper, or pass any zero-argument
  callable returning a SQLAlchemy ``Connection``/``Session`` context manager
  (for example ``lambda: engine.begin()``).
* Routes should require the authenticated actor, pass the owner-private
  ``Scope``/``SceneScope`` and reject group scenes explicitly if the product only
  exposes personal cards.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID, uuid4

from sqlalchemy import (
    Column,
    Float,
    Index,
    Table,
    Text,
    and_,
    delete,
    func,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from ..archive.models import idcol, fk, js, metadata, ts
from ..archive.repository import lock
from ..core.errors import AppError
from . import models as mm
from .access import item_pool_clause
from .keyword import terms
from .semantic import active_clause

DEFAULT_BUDGET_CHARS = 1200
MIN_BUDGET_CHARS = 60

CARD_TITLE = "记忆卡片"
EMPTY_CARD = "（暂无记忆）"
PIN_MARKER = "★"

ARCHIVE_REASON_FORGET = "forget"
ARCHIVE_REASON_MERGE = "merge"

# Section order follows the product wording: facts, preferences, episodes,
# relationships, then the less frequent identity/commitment/plan material.
SECTION_ORDER = ("fact", "preference", "episode", "relationship", "commitment", "identity", "plan", "other")
SECTION_TITLES = {
    "fact": "事实",
    "preference": "偏好",
    "episode": "经历",
    "relationship": "关系",
    "commitment": "承诺",
    "identity": "身份",
    "plan": "计划",
    "other": "其他",
}

# ``memory_archive`` is declared on the shared archive MetaData so migrations and
# the archive tooling can discover it. It intentionally has no FK on
# ``source_memory_id``: the source row is removed right after the archive copy and
# a FK would either block that removal or cascade-delete the archive itself.
memory_archive = Table(
    "memory_archive",
    metadata,
    idcol(),
    fk("account_id", "accounts"),
    fk("companion_id", "companions"),
    Column("source_memory_id", PGUUID(as_uuid=True), nullable=False),
    Column("kind", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("scope", Text, nullable=False, server_default="owner_private", default="owner_private"),
    Column("importance", Float, nullable=False, server_default="0.5", default=0.5),
    js("metadata_json"),
    ts("archived_at"),
    Index("memory_archive_scope_idx", "account_id", "companion_id"),
    Index("memory_archive_source_idx", "source_memory_id"),
)

# JSON-serialisable description for the migration/wiring owner (PostgreSQL DDL).
TABLE_DEFINITION: dict[str, Any] = {
    "table": "memory_archive",
    "columns": [
        {"name": "id", "type": "uuid", "primary_key": True, "nullable": False},
        {"name": "account_id", "type": "uuid", "nullable": False},
        {"name": "companion_id", "type": "uuid", "nullable": False},
        {"name": "source_memory_id", "type": "uuid", "nullable": False},
        {"name": "kind", "type": "text", "nullable": False},
        {"name": "content", "type": "text", "nullable": False},
        {"name": "scope", "type": "text", "nullable": False, "server_default": "owner_private"},
        {"name": "importance", "type": "double precision", "nullable": False, "server_default": "0.5"},
        {"name": "metadata_json", "type": "jsonb", "nullable": False, "server_default": "'{}'::jsonb"},
        {"name": "archived_at", "type": "timestamp with time zone", "nullable": False, "server_default": "now()"},
    ],
    "indexes": [
        {"name": "memory_archive_scope_idx", "columns": ["account_id", "companion_id"]},
        {"name": "memory_archive_source_idx", "columns": ["source_memory_id"]},
    ],
}

_UUID_FIELDS = (
    "id",
    "account_id",
    "companion_id",
    "user_id",
    "subject_principal_id",
    "origin_room_id",
    "visibility_room_id",
)
_TIMESTAMP_FIELDS = (
    "last_accessed_at",
    "last_reinforced_at",
    "expires_at",
    "created_at",
    "updated_at",
    "valid_from",
    "valid_to",
    "archived_at",
    "restored_at",
)


class MemoryCardError(AppError):
    """Domain error with a stable public code (rendered by the API error handler)."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code, message, status)


def _uuid_or_error(value: Any, field: str, code: str = "INVALID_ID", status: int = 422) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise MemoryCardError(code, f"{field} 不是有效的 UUID", status) from None


def _maybe_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise MemoryCardError("INVALID_SCOPE", "scope 中的 UUID 字段无效", 422) from None


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        raise MemoryCardError("INVALID_ARCHIVE", "归档时间戳损坏", 409) from None


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _row_snapshot(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe(value) for key, value in dict(row).items()}


def _scope_value(scope: Any, name: str) -> Any:
    if isinstance(scope, Mapping):
        return scope.get(name)
    return getattr(scope, name, None)


def _coerce_scope(scope: Any) -> SimpleNamespace:
    """Accept a Scope/SceneScope or a mapping and normalise it for SQL predicates."""
    if scope is None:
        raise MemoryCardError("INVALID_SCOPE", "缺少记忆作用域", 422)
    account_id = _maybe_uuid(_scope_value(scope, "account_id"))
    companion_id = _maybe_uuid(_scope_value(scope, "companion_id"))
    if account_id is None or companion_id is None:
        raise MemoryCardError("INVALID_SCOPE", "作用域必须包含 account_id 与 companion_id", 422)
    user_id = _maybe_uuid(_scope_value(scope, "user_id"))
    scene = str(_scope_value(scope, "scene") or "owner_private")
    if scene not in ("owner_private", "group"):
        raise MemoryCardError("INVALID_SCOPE", "未知的会话场景", 422)
    room_id = _maybe_uuid(_scope_value(scope, "room_id"))
    conversation_id = _maybe_uuid(_scope_value(scope, "conversation_id")) or uuid4()
    return SimpleNamespace(
        account_id=account_id,
        user_id=user_id,
        companion_id=companion_id,
        conversation_id=conversation_id,
        scene=scene,
        room_id=room_id,
    )


def _scope_filter(scope: SimpleNamespace):
    memory = mm.memory_items.c
    clauses = [memory.account_id == scope.account_id, memory.companion_id == scope.companion_id]
    if scope.user_id is not None:
        clauses.append(or_(memory.user_id == scope.user_id, memory.user_id.is_(None)))
    clauses.append(item_pool_clause(scope))
    return and_(*clauses)


def _archive_scope_filter(scope: SimpleNamespace):
    return and_(
        memory_archive.c.account_id == scope.account_id,
        memory_archive.c.companion_id == scope.companion_id,
    )


def _like_pattern(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _normalize_line(content: Any) -> str:
    return re.sub(r"\s+", " ", str(content or "")).strip()


class MemoryCardService:
    """Deterministic card renderer plus archive-backed forget/merge/undo.

    ``session_factory`` may be:
      * the repository ``Database`` wrapper (``db.engine.begin`` is used), or
      * a zero-argument callable returning a connection/session context manager.
    """

    def __init__(self, session_factory: Any, *, clock=None):
        if session_factory is None:
            raise TypeError("session_factory 不能为空")
        self._factory = session_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # -- session plumbing -------------------------------------------------
    def _session(self):
        factory = self._factory
        if hasattr(factory, "engine"):  # repository.Database convenience
            return factory.engine.begin()
        if callable(factory):
            return factory()
        if hasattr(factory, "begin") and hasattr(factory, "connect"):
            return factory.begin()
        raise TypeError("session_factory 必须是可调用对象或 Database/Engine")

    # -- rendering --------------------------------------------------------
    def render(self, scope: Any, budget_chars: int | None = None) -> str:
        """Render at most ``budget_chars`` characters for ``scope`` (no writes)."""
        target = _coerce_scope(scope)
        budget = self._budget(budget_chars)
        rows = self._render_rows(target)
        return self._compose(rows, budget)

    def status(self, scope: Any = None) -> dict[str, Any]:
        """Operational summary; pass a scope for per-companion counters."""
        target = _coerce_scope(scope) if scope is not None else None
        with self._session() as conn:
            active_query = select(func.count()).select_from(mm.memory_items).where(active_clause())
            archived_query = select(func.count()).select_from(memory_archive)
            pinned_query = (
                select(func.count())
                .select_from(mm.memory_items)
                .where(active_clause(), mm.memory_items.c.is_pinned.is_(True))
            )
            kinds_query = (
                select(mm.memory_items.c.kind, func.count())
                .where(active_clause())
                .group_by(mm.memory_items.c.kind)
            )
            if target is not None:
                scope_clause = _scope_filter(target)
                active_query = active_query.where(scope_clause)
                pinned_query = pinned_query.where(scope_clause)
                kinds_query = kinds_query.where(scope_clause)
                archived_query = archived_query.where(_archive_scope_filter(target))
            active = int(conn.scalar(active_query) or 0)
            archived = int(conn.scalar(archived_query) or 0)
            pinned = int(conn.scalar(pinned_query) or 0)
            kinds = {str(kind or "other"): int(count) for kind, count in conn.execute(kinds_query)}
        sections = {title: 0 for title in SECTION_TITLES.values()}
        for kind, count in kinds.items():
            title = SECTION_TITLES.get(kind, SECTION_TITLES["other"])
            sections[title] += count
        return {
            "enabled": True,
            "table": "memory_archive",
            "active": active,
            "archived": archived,
            "pinned": pinned,
            "kinds": kinds,
            "sections": sections,
        }

    # -- forget / merge / undo -------------------------------------------
    def forget(self, memory_id: Any, scope: Any = None) -> dict[str, Any]:
        """Archive one memory row, then delete it; returns the archive handle."""
        target_id = _uuid_or_error(memory_id, "memory_id", "INVALID_MEMORY_ID")
        target = _coerce_scope(scope) if scope is not None else None
        with self._session() as conn:
            row = self._select_one(conn, target_id, target, for_update=True)
            if row is None:
                raise MemoryCardError("MEMORY_NOT_FOUND", "记忆不存在或不属于此作用域", 404)
            lock(conn, row["companion_id"])
            archived = self._archive(conn, row, reason=ARCHIVE_REASON_FORGET)
            self._remove_memory_rows(conn, [row["id"]])
        return {
            "memory_id": str(row["id"]),
            "archive_id": archived["archive_id"],
            "kind": row["kind"],
            "content": row["content"],
        }

    def merge(
        self,
        memory_ids: Sequence[Any],
        title: str = "",
        content: str | None = None,
        scope: Any = None,
    ) -> dict[str, Any]:
        """Archive the sources, remove them and create one new episode memory."""
        if isinstance(memory_ids, (str, bytes, UUID)) or memory_ids is None:
            raise MemoryCardError("INVALID_MERGE", "memory_ids 必须是记忆 ID 序列", 422)
        ids = [str(value) for value in memory_ids]
        if len(ids) < 2:
            raise MemoryCardError("INVALID_MERGE", "合并至少需要两条记忆", 422)
        ids = list(dict.fromkeys(ids))
        if len(ids) < 2:
            raise MemoryCardError("INVALID_MERGE", "合并至少需要两条不同的记忆", 422)
        target = _coerce_scope(scope) if scope is not None else None
        title = (title or "").strip()
        body = content.strip() if isinstance(content, str) and content.strip() else None
        with self._session() as conn:
            rows = self._select_many(conn, ids, target, for_update=True)
            if len(rows) != len(ids):
                raise MemoryCardError("MEMORY_NOT_FOUND", "部分记忆不存在或不属于此作用域", 404)
            first = rows[0]
            lock(conn, first["companion_id"])
            for row in rows[1:]:
                if (
                    row["account_id"] != first["account_id"]
                    or row["companion_id"] != first["companion_id"]
                    or row["user_id"] != first["user_id"]
                ):
                    raise MemoryCardError("MERGE_SCOPE_MISMATCH", "只能合并同一作用域内的记忆", 409)
            archived = [
                self._archive(
                    conn,
                    row,
                    reason=ARCHIVE_REASON_MERGE,
                    extra={"merge_title": title},
                )
                for row in rows
            ]
            self._remove_memory_rows(conn, [row["id"] for row in rows])
            joined = body if body else "；".join(_normalize_line(row["content"]) for row in rows)
            merged_content = f"{title}\n{joined}" if title and body else (title or joined)
            if not merged_content:
                raise MemoryCardError("INVALID_MERGE", "合并内容不能为空", 422)
            merged_id = uuid4()
            importance = max(float(row["importance"] or 0.5) for row in rows)
            conn.execute(
                mm.memory_items.insert().values(
                    id=merged_id,
                    account_id=first["account_id"],
                    companion_id=first["companion_id"],
                    user_id=first["user_id"],
                    kind="episode",
                    content=merged_content,
                    epistemic_type="inferred",
                    confidence=max(float(row["confidence"] or 1) for row in rows),
                    status="active",
                    revision=1,
                    importance=min(1.0, max(0.5, importance)),
                    access_count=0,
                    ttl_days=float(first["ttl_days"] or 30),
                    decay_type=str(first["decay_type"] or "exponential"),
                    subject_key="",
                    predicate="",
                    value="",
                    is_pinned=any(bool(row["is_pinned"]) for row in rows),
                    summary_level="",
                    pool_kind=str(first["pool_kind"] or "owner"),
                    visibility=str(first["visibility"] or "owner_private"),
                    visibility_room_id=first["visibility_room_id"],
                    origin_room_id=first["origin_room_id"],
                )
            )
            archive_ids = [archive["archive_id"] for archive in archived]
        return {
            "memory_id": str(merged_id),
            "title": title,
            "content": merged_content,
            "source_memory_ids": [str(row["id"]) for row in rows],
            "archive_ids": archive_ids,
        }

    def undo(self, archive_id: Any) -> dict[str, Any]:
        """Restore one archived memory row from its snapshot (idempotent-safe)."""
        target_id = _uuid_or_error(archive_id, "archive_id", "INVALID_ARCHIVE_ID")
        with self._session() as conn:
            row = (
                conn.execute(select(memory_archive).where(memory_archive.c.id == target_id))
                .mappings()
                .first()
            )
            if row is None:
                raise MemoryCardError("ARCHIVE_NOT_FOUND", "归档记录不存在", 404)
            meta = dict(row["metadata_json"] or {})
            if meta.get("restored"):
                raise MemoryCardError("ALREADY_RESTORED", "该记忆已经恢复过", 409)
            snapshot = meta.get("memory") or {}
            memory_id = _maybe_uuid(snapshot.get("id") or row["source_memory_id"])
            if memory_id is None:
                raise MemoryCardError("INVALID_ARCHIVE", "归档记录缺少有效记忆 ID", 409)
            lock(conn, row["companion_id"])
            existing = conn.execute(
                select(mm.memory_items.c.id).where(mm.memory_items.c.id == memory_id)
            ).first()
            if existing is not None:
                raise MemoryCardError("MEMORY_CONFLICT", "原始记忆已存在，无法恢复", 409)
            values = self._restore_values(snapshot, row)
            conn.execute(mm.memory_items.insert().values(**values))
            self._restore_source_refs(conn, memory_id, meta.get("source_refs") or [])
            conn.execute(
                memory_archive.update()
                .where(memory_archive.c.id == target_id)
                .values(
                    metadata_json={
                        **meta,
                        "restored": True,
                        "restored_at": _json_safe(self._clock()),
                    }
                )
            )
        return {
            "memory_id": str(memory_id),
            "archive_id": str(target_id),
            "restored": True,
            "kind": values.get("kind"),
            "content": values.get("content"),
        }

    # -- full-text search -------------------------------------------------
    def full_search(self, query: str, scope: Any = None, limit: int = 20) -> list[dict[str, Any]]:
        """Search active and archived cards with deterministic CJK-aware matching."""
        text = (query or "").strip()
        if not text:
            raise MemoryCardError("INVALID_QUERY", "搜索词不能为空", 422)
        target = _coerce_scope(scope) if scope is not None else None
        try:
            size = int(limit)
        except (TypeError, ValueError):
            raise MemoryCardError("INVALID_LIMIT", "limit 必须是整数", 422) from None
        size = max(1, min(size, 100))
        tokens = terms(text) or [text.lower()]
        active_content = and_(
            *(
                func.lower(mm.memory_items.c.content).like(_like_pattern(token), escape="\\")
                for token in tokens
            )
        )
        archived_content = and_(
            *(
                func.lower(memory_archive.c.content).like(_like_pattern(token), escape="\\")
                for token in tokens
            )
        )
        active_query = (
            select(
                mm.memory_items.c.id,
                mm.memory_items.c.kind,
                mm.memory_items.c.content,
                mm.memory_items.c.importance,
                mm.memory_items.c.is_pinned,
                mm.memory_items.c.archived_at,
            )
            .where(active_clause(), active_content)
            .order_by(
                mm.memory_items.c.is_pinned.is_(True).desc(),
                mm.memory_items.c.importance.desc(),
                mm.memory_items.c.id,
            )
            .limit(size)
        )
        archived_query = (
            select(
                memory_archive.c.id,
                memory_archive.c.source_memory_id,
                memory_archive.c.kind,
                memory_archive.c.content,
                memory_archive.c.importance,
                memory_archive.c.archived_at,
            )
            .where(archived_content)
            .order_by(memory_archive.c.archived_at.desc(), memory_archive.c.id)
            .limit(size)
        )
        if target is not None:
            active_query = active_query.where(_scope_filter(target))
            archived_query = archived_query.where(_archive_scope_filter(target))
        results: list[dict[str, Any]] = []
        with self._session() as conn:
            for row in conn.execute(active_query).mappings():
                results.append(
                    {
                        "source": "active",
                        "memory_id": str(row["id"]),
                        "kind": row["kind"],
                        "content": row["content"],
                        "importance": float(row["importance"] or 0.5),
                        "is_pinned": bool(row["is_pinned"]),
                        "archived_at": None,
                    }
                )
            for row in conn.execute(archived_query).mappings():
                results.append(
                    {
                        "source": "archived",
                        "memory_id": str(row["source_memory_id"]),
                        "archive_id": str(row["id"]),
                        "kind": row["kind"],
                        "content": row["content"],
                        "importance": float(row["importance"] or 0.5),
                        "is_pinned": False,
                        "archived_at": _json_safe(row["archived_at"]),
                    }
                )
        results.sort(
            key=lambda item: (
                0 if item["source"] == "active" else 1,
                -float(item["importance"]),
                item["content"],
            )
        )
        return results[:size]

    # -- internals --------------------------------------------------------
    def _budget(self, budget_chars: int | None) -> int:
        if budget_chars is None:
            return DEFAULT_BUDGET_CHARS
        try:
            budget = int(budget_chars)
        except (TypeError, ValueError):
            raise MemoryCardError("INVALID_BUDGET", "budget_chars 必须是整数", 422) from None
        if budget < MIN_BUDGET_CHARS:
            raise MemoryCardError(
                "INVALID_BUDGET", f"budget_chars 不能小于 {MIN_BUDGET_CHARS}", 422
            )
        return budget

    def _render_rows(self, scope: SimpleNamespace) -> list[Mapping[str, Any]]:
        order = (
            mm.memory_items.c.is_pinned.is_(True).desc(),
            mm.memory_items.c.importance.desc(),
            func.coalesce(
                mm.memory_items.c.last_reinforced_at,
                mm.memory_items.c.updated_at,
                mm.memory_items.c.created_at,
            ).desc().nullslast(),
            mm.memory_items.c.access_count.desc(),
            mm.memory_items.c.id,
        )
        query = (
            select(
                mm.memory_items.c.id,
                mm.memory_items.c.kind,
                mm.memory_items.c.content,
                mm.memory_items.c.importance,
                mm.memory_items.c.is_pinned,
                mm.memory_items.c.last_reinforced_at,
            )
            .where(active_clause(), _scope_filter(scope))
            .order_by(*order)
            .limit(500)
        )
        with self._session() as conn:
            return list(conn.execute(query).mappings())

    def _compose(self, rows: Iterable[Mapping[str, Any]], budget: int) -> str:
        text = CARD_TITLE if len(CARD_TITLE) <= budget else ""
        if not rows:
            empty = f"\n\n{EMPTY_CARD}"
            if text and len(text) + len(empty) <= budget:
                return text + empty
            return text
        seen_sections: list[str] = []
        sections: dict[str, list[str]] = {kind: [] for kind in SECTION_ORDER}
        for row in rows:
            content = _normalize_line(row["content"])
            if not content:
                continue
            kind = str(row["kind"] or "other")
            if kind not in SECTION_TITLES:
                kind = "other"
            line = f"- {PIN_MARKER} {content}" if row["is_pinned"] else f"- {content}"
            header_cost = 0
            if kind not in seen_sections:
                header_cost = len(SECTION_TITLES[kind]) + 4  # "\n\n【" + title + "】"
            if len(text) + 1 + header_cost + len(line) > budget:
                break
            if kind not in seen_sections:
                seen_sections.append(kind)
                sections[kind] = []
                text += f"\n\n【{SECTION_TITLES[kind]}】"
            text += f"\n{line}"
        return text

    def _select_one(
        self, conn, memory_id: UUID, scope: SimpleNamespace | None, *, for_update: bool = False
    ) -> Mapping[str, Any] | None:
        clauses = [mm.memory_items.c.id == memory_id]
        if scope is not None:
            clauses.append(_scope_filter(scope))
        query = select(mm.memory_items).where(*clauses)
        if for_update:
            query = query.with_for_update()
        return conn.execute(query).mappings().first()

    def _select_many(
        self,
        conn,
        memory_ids: Sequence[str],
        scope: SimpleNamespace | None,
        *,
        for_update: bool = False,
    ) -> list[Mapping[str, Any]]:
        ids = [_uuid_or_error(value, "memory_id", "INVALID_MEMORY_ID") for value in memory_ids]
        clauses = [mm.memory_items.c.id.in_(ids)]
        if scope is not None:
            clauses.append(_scope_filter(scope))
        query = select(mm.memory_items).where(*clauses)
        if for_update:
            query = query.with_for_update()
        rows = {row["id"]: row for row in conn.execute(query).mappings()}
        return [rows[value] for value in ids if value in rows]

    def _archive(
        self,
        conn,
        row: Mapping[str, Any],
        *,
        reason: str,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_refs = [
            _row_snapshot(ref)
            for ref in conn.execute(
                select(mm.source_refs).where(mm.source_refs.c.memory_id == row["id"])
            ).mappings()
        ]
        metadata_json: dict[str, Any] = {
            "reason": reason,
            "memory": _row_snapshot(row),
            "source_refs": source_refs,
            "restored": False,
        }
        if extra:
            metadata_json.update(_json_safe(dict(extra)))
        archive_id = uuid4()
        conn.execute(
            memory_archive.insert().values(
                id=archive_id,
                account_id=row["account_id"],
                companion_id=row["companion_id"],
                source_memory_id=row["id"],
                kind=row["kind"],
                content=row["content"],
                scope=str(row["visibility"] or "owner_private"),
                importance=float(row["importance"] or 0.5),
                metadata_json=metadata_json,
            )
        )
        return {"archive_id": str(archive_id), "source_memory_id": str(row["id"])}

    def _remove_memory_rows(self, conn, memory_ids: Sequence[UUID]) -> None:
        ids = list(memory_ids)
        if not ids:
            return
        conn.execute(delete(mm.source_refs).where(mm.source_refs.c.memory_id.in_(ids)))
        conn.execute(
            delete(mm.memory_relations).where(
                or_(
                    mm.memory_relations.c.from_memory_id.in_(ids),
                    mm.memory_relations.c.to_memory_id.in_(ids),
                )
            )
        )
        conn.execute(delete(mm.memory_items).where(mm.memory_items.c.id.in_(ids)))

    def _restore_values(self, snapshot: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field in mm.memory_items.c.keys():
            if field in snapshot:
                values[field] = snapshot[field]
        if not values.get("account_id"):
            values["account_id"] = row["account_id"]
        if not values.get("companion_id"):
            values["companion_id"] = row["companion_id"]
        if not values.get("kind"):
            values["kind"] = row["kind"]
        if not values.get("content"):
            values["content"] = row["content"]
        values["id"] = _maybe_uuid(values.get("id")) or _maybe_uuid(row["source_memory_id"])
        values["account_id"] = _maybe_uuid(row["account_id"])
        values["companion_id"] = _maybe_uuid(row["companion_id"])
        for field in _UUID_FIELDS:
            if field in values and values[field] is not None:
                values[field] = _maybe_uuid(values[field])
        for field in _TIMESTAMP_FIELDS:
            if field in values:
                values[field] = _parse_timestamp(values[field])
        values["status"] = str(values.get("status") or "active")
        values["archived_at"] = None
        values["valid_to"] = None
        return values

    def _restore_source_refs(
        self, conn, memory_id: UUID, source_refs: Sequence[Mapping[str, Any]]
    ) -> None:
        for ref in source_refs:
            try:
                with conn.begin_nested():
                    conn.execute(
                        mm.source_refs.insert().values(
                            memory_id=memory_id,
                            event_id=_maybe_uuid(ref.get("event_id"))
                            or _maybe_uuid(ref.get("id")),
                            json_pointer=str(ref.get("json_pointer") or "/text"),
                            start_offset=ref.get("start_offset"),
                            end_offset=ref.get("end_offset"),
                            quote_hash=ref.get("quote_hash"),
                        )
                    )
            except Exception:
                # The source event may have been deleted after the memory was
                # archived; the memory row itself is still restored.
                continue


def ensure_archive_table(bind) -> None:
    """Create ``memory_archive`` if the caller runs without a migration (tests)."""
    memory_archive.create(bind, checkfirst=True)
