"""Manual-trigger autobiography organizer: preview -> confirm -> undo.

This module is an isolated backend core. It deliberately owns no database access,
no model client and no API wiring:

* :meth:`AutobiographyOrganizer.preview` groups already selected/queried memory
  entries, asks an injected summarizer for one summary per chapter and returns a
  preview. It only registers the preview in process memory; the injected
  :class:`AutobiographyWriter` is not touched.
* :meth:`AutobiographyOrganizer.confirm` is the explicit write trigger. It hands
  the whole preview to the writer exactly once and records a reversible commit.
* :meth:`AutobiographyOrganizer.undo` asks the writer to restore the originals
  and deactivates the generated summaries.

The organizer keeps a full snapshot of every original entry inside the preview
and the commit record, so originals are preserved even before a persistence
adapter is wired in. Everything is deterministic by construction: sorting is
stable, the default summarizer is extractive (no model call) and both the clock
and the id generator are injectable.

Integration (task-7b wiring lives in ``api/app.py``; this module stays model-free):
  * :class:`SqlAutobiographyWriter` is the transactional persistence adapter
    (insert summary rows, retire but never delete originals; undo restores them);
  * routes for status / preview / confirm / undo and the
    ``memory_autobiography_enabled`` settings flag;
  * an LLM-backed :class:`AutobiographySummarizer` (or reuse the memory_extract
    role). Tests and the default path only need the deterministic summarizer.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Protocol
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..archive.repository import lock
from ..core.errors import AppError
from ..core.types import Actor
from ..identity.service import authorize
from . import models as mm
from .lifecycle import lifecycle_values

# Target shape of the derived summary memories. ``summary_level`` is a plain text
# column; the value intentionally differs from daily/weekly consolidation so an
# autobiography can be filtered independently later.
AUTOBIOGRAPHY_KIND = "episode"
AUTOBIOGRAPHY_EPISTEMIC_TYPE = "inferred"
AUTOBIOGRAPHY_SUMMARY_LEVEL = "autobiography"
AUTOBIOGRAPHY_PREDICATE = "autobiography"

GROUP_BY_PERIOD = "period"
GROUP_BY_KIND = "kind"
GROUP_BY_SUBJECT = "subject"
GROUPINGS = (GROUP_BY_PERIOD, GROUP_BY_KIND, GROUP_BY_SUBJECT)

KIND_ORDER = ("identity", "relationship", "preference", "commitment",
              "fact", "episode", "plan", "other")
KIND_TITLES = {
    "identity": "身份",
    "relationship": "关系",
    "preference": "偏好",
    "commitment": "承诺",
    "fact": "事实",
    "episode": "经历",
    "plan": "计划",
    "other": "其他",
}
UNKNOWN_PERIOD = "__unknown__"
UNKNOWN_PERIOD_TITLE = "未标注时间"
UNCLASSIFIED_SUBJECT = "__unclassified__"
UNCLASSIFIED_SUBJECT_TITLE = "未分类"
CUSTOM_GROUP = "custom"
CUSTOM_GROUP_TITLE = "其他"


class AutobiographyError(AppError):
    """Domain error with a stable code/status; API routes can map it directly."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(code, message, status)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid4().hex


def _to_utc(value: Any) -> datetime | None:
    """Best-effort timestamp normalisation, tolerant of ISO strings and naive values."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _normalize_scope(scope: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if scope is None:
        return MappingProxyType({})
    if not isinstance(scope, Mapping):
        raise AutobiographyError("INVALID_SCOPE", "scope 必须是可序列化的映射", 422)
    return MappingProxyType({str(key): None if value is None else str(value)
                             for key, value in scope.items()})


def _entry_sort_key(entry: "MemoryEntry"):
    # Entries without a timestamp sort first, then chronologically, then by id so
    # identical inputs always produce identical chapters.
    missing = entry.created_at is None
    return (missing, entry.created_at or datetime.max.replace(tzinfo=timezone.utc),
            entry.memory_id)


@dataclass(frozen=True)
class MemoryEntry:
    """Normalised, persistence-agnostic view of one selected memory entry.

    ``MemoryEntry.coerce`` accepts this dataclass, a mapping row from
    ``MemoryService.list_items`` / ``memory_items``, or a retrieval hit such as
    ``MemoryHit`` from ``MemoryService.search``.
    """

    memory_id: str
    content: str
    kind: str = "other"
    epistemic_type: str = "inferred"
    importance: float = 0.5
    created_at: datetime | None = None
    status: str = "active"
    is_pinned: bool = False
    subject_key: str = ""
    confidence: float = 0.6

    @classmethod
    def coerce(cls, value: Any) -> "MemoryEntry":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls.from_mapping(value)
        if hasattr(value, "content") and (hasattr(value, "memory_id") or hasattr(value, "chunk_id")):
            return cls.from_hit(value)
        raise AutobiographyError(
            "INVALID_ENTRY",
            "无法识别的记忆条目：需要 MemoryEntry、映射或检索命中对象",
            422,
        )

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "MemoryEntry":
        raw_id = row.get("memory_id") or row.get("id") or row.get("chunk_id")
        content = row.get("content", "")
        if raw_id in (None, "") or not isinstance(content, str):
            raise AutobiographyError(
                "INVALID_ENTRY",
                "记忆条目缺少 memory_id 或 content 不是字符串",
                422,
            )
        return cls(
            memory_id=str(raw_id),
            content=content,
            kind=str(row.get("kind") or "other"),
            epistemic_type=str(row.get("epistemic_type") or "inferred"),
            importance=_clamp(_as_float(row.get("importance"), 0.5)),
            created_at=_to_utc(row.get("created_at") or row.get("recorded_at")
                               or row.get("occurred_at")),
            status=str(row.get("status") or "active"),
            is_pinned=bool(row.get("is_pinned")),
            subject_key=str(row.get("subject_key") or ""),
            confidence=_clamp(_as_float(row.get("confidence"), 0.6)),
        )

    @classmethod
    def from_hit(cls, hit: Any) -> "MemoryEntry":
        raw_id = getattr(hit, "memory_id", None) or getattr(hit, "chunk_id", None)
        content = getattr(hit, "content", None)
        if raw_id in (None, "") or not isinstance(content, str):
            raise AutobiographyError(
                "INVALID_ENTRY",
                "检索命中缺少 memory_id/chunk_id 或 content 不是字符串",
                422,
            )
        created_at = None
        time_range = getattr(hit, "time_range", None)
        if isinstance(time_range, (tuple, list)) and len(time_range) == 2:
            created_at = time_range[0]
        if created_at is None:
            created_at = getattr(hit, "occurred_at", None)
        return cls(
            memory_id=str(raw_id),
            content=content,
            kind=str(getattr(hit, "kind", None) or "other"),
            epistemic_type=str(getattr(hit, "epistemic_type", None) or "inferred"),
            importance=_clamp(_as_float(getattr(hit, "importance", 0.5), 0.5)),
            created_at=_to_utc(created_at),
            status=str(getattr(hit, "status", None) or "active"),
            is_pinned=bool(getattr(hit, "is_pinned", False)),
            subject_key=str(getattr(hit, "subject_key", None) or ""),
            confidence=_clamp(_as_float(getattr(hit, "confidence", 0.6), 0.6)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "kind": self.kind,
            "epistemic_type": self.epistemic_type,
            "importance": self.importance,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "status": self.status,
            "is_pinned": self.is_pinned,
            "subject_key": self.subject_key,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class SummaryRequest:
    """Input handed to an injected summarizer for one chapter chunk."""

    chapter_key: str
    title: str
    entries: tuple[MemoryEntry, ...]
    max_summary_chars: int
    language: str = "zh"
    scope: Mapping[str, Any] = field(default_factory=dict)

    @property
    def entry_ids(self) -> tuple[str, ...]:
        return tuple(entry.memory_id for entry in self.entries)


@dataclass(frozen=True)
class SummaryDraft:
    """Summarizer output. ``title`` overrides the default deterministic title."""

    summary: str
    title: str | None = None
    confidence: float = 0.5
    metadata: Mapping[str, Any] = field(default_factory=dict)


class AutobiographySummarizer(Protocol):
    """Injectable summarizer port. Implementations may be extractive or LLM-backed."""

    def summarize(self, request: SummaryRequest) -> SummaryDraft:
        ...


class DeterministicSummarizer:
    """Default extractive summarizer; no model call, fully reproducible.

    It keeps the first ``max_quotes`` entry snippets in chronological order and
    joins them, which is deliberately boring: tests and offline/dev preview never
    need a provider, and an LLM adapter can replace it at integration time.
    """

    def __init__(self, max_quotes: int = 5, quote_chars: int = 80, separator: str = "；"):
        self.max_quotes = max(1, int(max_quotes))
        self.quote_chars = max(8, int(quote_chars))
        self.separator = separator

    def summarize(self, request: SummaryRequest) -> SummaryDraft:
        parts: list[str] = []
        for entry in request.entries[:self.max_quotes]:
            text = " ".join(entry.content.split())
            if not text:
                continue
            if len(text) > self.quote_chars:
                text = text[:self.quote_chars].rstrip() + "…"
            parts.append(text)
        summary = self.separator.join(parts)
        budget = max(20, int(request.max_summary_chars))
        if len(summary) > budget:
            summary = summary[:budget - 1].rstrip() + "…"
        confidence = (round(sum(entry.importance for entry in request.entries)
                            / len(request.entries), 4)
                      if request.entries else 0.0)
        return SummaryDraft(summary=summary, confidence=confidence)


@dataclass(frozen=True)
class AutobiographyChapter:
    """One preview chapter: a group chunk plus its summary."""

    chapter_id: str
    title: str
    summary: str
    entry_ids: tuple[str, ...]
    importance: float
    confidence: float
    kind: str = AUTOBIOGRAPHY_KIND
    epistemic_type: str = AUTOBIOGRAPHY_EPISTEMIC_TYPE
    summary_level: str = AUTOBIOGRAPHY_SUMMARY_LEVEL
    start_at: datetime | None = None
    end_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "title": self.title,
            "summary": self.summary,
            "entry_ids": list(self.entry_ids),
            "entry_count": len(self.entry_ids),
            "importance": self.importance,
            "confidence": self.confidence,
            "kind": self.kind,
            "epistemic_type": self.epistemic_type,
            "summary_level": self.summary_level,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
        }


@dataclass(frozen=True)
class AutobiographyPreview:
    """Read-only preview. Nothing is persisted until ``confirm`` is called."""

    preview_id: str
    scope: Mapping[str, Any]
    created_at: datetime
    group_by: str
    chapters: tuple[AutobiographyChapter, ...]
    originals: tuple[MemoryEntry, ...]
    warnings: tuple[str, ...] = ()
    entry_count: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.chapters

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preview_id": self.preview_id,
            "scope": dict(self.scope),
            "created_at": self.created_at.isoformat(),
            "group_by": self.group_by,
            "chapter_count": self.chapter_count,
            "entry_count": self.entry_count,
            "is_empty": self.is_empty,
            "warnings": list(self.warnings),
            "chapters": [chapter.to_dict() for chapter in self.chapters],
            "original_ids": [entry.memory_id for entry in self.originals],
            "originals": [entry.to_dict() for entry in self.originals],
        }


@dataclass(frozen=True)
class AutobiographyCommit:
    """Writer-facing commit record; ``result`` is the adapter's own return value."""

    record_id: str
    preview_id: str
    scope: Mapping[str, Any]
    originals: tuple[MemoryEntry, ...]
    created_at: datetime
    result: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AutobiographyRecord:
    """Client-facing result of a confirm/undo; JSON-serialisable via ``to_dict``."""

    record_id: str
    preview_id: str
    scope: Mapping[str, Any]
    status: str
    created_at: datetime
    chapter_count: int
    created_memory_ids: tuple[str, ...]
    original_ids: tuple[str, ...]
    reverted_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "preview_id": self.preview_id,
            "scope": dict(self.scope),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "reverted_at": self.reverted_at.isoformat() if self.reverted_at else None,
            "chapter_count": self.chapter_count,
            "created_memory_ids": list(self.created_memory_ids),
            "original_ids": list(self.original_ids),
        }


class AutobiographyWriter(Protocol):
    """Persistence port for confirmed previews.

    Contract for real adapters (the reference in-memory writer below follows it):

    * ``commit`` is only called for an explicitly confirmed preview and must be
      atomic: either every chapter summary is persisted and every existing
      original is retired (status/valid_to/archived_at), or nothing changes.
    * Originals are preserved, never deleted; their content stays readable so a
      user can inspect what the autobiography replaced.
    * ``commit`` returns a mapping. If it contains ``created_memory_ids`` (a
      sequence or mapping of ids) those ids are reported back to the caller on
      the record; the whole mapping is stored on the commit and handed to
      ``revert`` unchanged.
    * ``revert`` restores the retired originals to their previous state and makes
      sure summaries created by that commit are no longer active. It is only
      called after a successful commit.
    """

    def commit(self, preview: AutobiographyPreview, created_at: datetime) -> Mapping[str, Any]:
        ...

    def revert(self, commit: AutobiographyCommit) -> None:
        ...


class InMemoryAutobiographyWriter:
    """Reference adapter for tests and development; not a persistence backend.

    It mirrors the row semantics the SQL adapter should have: summaries are new
    active rows, originals become ``superseded`` while keeping their content, and
    ``revert`` restores the previous status and marks summaries ``reverted``.
    """

    def __init__(self):
        self.entries: dict[str, dict[str, Any]] = {}
        self.commit_calls = 0
        self.revert_calls = 0

    def seed(self, entries: Iterable[MemoryEntry | Mapping[str, Any]]) -> None:
        for raw in entries:
            entry = MemoryEntry.coerce(raw)
            self.entries[entry.memory_id] = {
                "id": entry.memory_id,
                "content": entry.content,
                "kind": entry.kind,
                "epistemic_type": entry.epistemic_type,
                "importance": entry.importance,
                "created_at": entry.created_at,
                "status": "active",
                "is_pinned": entry.is_pinned,
            }

    def commit(self, preview: AutobiographyPreview, created_at: datetime) -> Mapping[str, Any]:
        self.commit_calls += 1
        created: list[str] = []
        previous_statuses: dict[str, str] = {}
        for chapter in preview.chapters:
            summary_id = "autobiography-" + _new_id()
            self.entries[summary_id] = {
                "id": summary_id,
                "content": chapter.summary,
                "kind": chapter.kind,
                "epistemic_type": chapter.epistemic_type,
                "summary_level": chapter.summary_level,
                "importance": chapter.importance,
                "created_at": created_at,
                "status": "active",
                "is_pinned": False,
                "source_memory_ids": list(chapter.entry_ids),
            }
            created.append(summary_id)
        for original in preview.originals:
            row = self.entries.get(original.memory_id)
            if row is None:
                # Search hits can reference retrieval chunks rather than memory
                # rows; such originals only need their snapshot, not a row update.
                continue
            previous_statuses[original.memory_id] = row.get("status", "active")
            row["status"] = "superseded"
            row["archived_at"] = created_at
            row["valid_to"] = created_at
        return {"created_memory_ids": created, "original_statuses": previous_statuses}

    def revert(self, commit: AutobiographyCommit) -> None:
        self.revert_calls += 1
        result = commit.result
        created = result.get("created_memory_ids") or ()
        if isinstance(created, Mapping):
            created = created.keys()
        for summary_id in created:
            row = self.entries.get(str(summary_id))
            if row is not None:
                row["status"] = "reverted"
        previous = result.get("original_statuses") or {}
        if isinstance(previous, Mapping):
            for memory_id, status in previous.items():
                row = self.entries.get(str(memory_id))
                if row is not None:
                    row["status"] = str(status)
                    row.pop("archived_at", None)
                    row.pop("valid_to", None)


@dataclass(frozen=True)
class AutobiographyConfig:
    """Tunables for the organizer; all bounded and validated at construction."""

    group_by: str | Callable[[MemoryEntry], Any] = GROUP_BY_PERIOD
    timezone: str = "UTC"
    max_entries: int = 500
    max_entries_per_chapter: int = 12
    max_summary_chars: int = 400
    max_pending_previews: int = 32
    language: str = "zh"

    def __post_init__(self):
        if not (callable(self.group_by) or self.group_by in GROUPINGS):
            raise ValueError("group_by must be one of " + ", ".join(GROUPINGS) + " or a callable")
        try:
            ZoneInfo(self.timezone)
        except Exception as error:  # noqa: BLE001 - ZoneInfo raises several types
            raise ValueError("invalid timezone: " + str(self.timezone)) from error
        for name in ("max_entries", "max_entries_per_chapter", "max_pending_previews"):
            if int(getattr(self, name)) < 1:
                raise ValueError(name + " must be positive")
        if int(self.max_summary_chars) < 20:
            raise ValueError("max_summary_chars must be at least 20")

    def grouping_name(self) -> str:
        return self.group_by if isinstance(self.group_by, str) else CUSTOM_GROUP


class AutobiographyOrganizer:
    """Manual two-step autobiography flow for one scope/companion.

    The organizer is deliberately synchronous: it fits the existing sync FastAPI
    routes and keeps the persistence port a plain callable. An LLM summarizer can
    still perform its own async work outside (or wrap a client synchronously) when
    the Lead wires it in.
    """

    def __init__(self, writer: AutobiographyWriter, *, scope: Mapping[str, Any] | None = None,
                 summarizer: AutobiographySummarizer | None = None,
                 config: AutobiographyConfig | None = None,
                 clock: Callable[[], datetime] | None = None,
                 id_factory: Callable[[], str] | None = None):
        self.writer = writer
        self.scope = _normalize_scope(scope)
        self.summarizer = summarizer or DeterministicSummarizer()
        self.config = config or AutobiographyConfig()
        self._clock = clock or _utcnow
        self._id_factory = id_factory or _new_id
        self._zone = ZoneInfo(self.config.timezone)
        self._lock = threading.Lock()
        self._pending: dict[str, AutobiographyPreview] = {}
        self._confirming: set[str] = set()
        self._confirmed: set[str] = set()
        self._commits: dict[str, AutobiographyCommit] = {}
        self._records: dict[str, AutobiographyRecord] = {}
        self._reverting: set[str] = set()

    # ------------------------------------------------------------------ preview
    def preview(self, entries: Iterable[Any] | None, *, group_by: Any = None) -> AutobiographyPreview:
        """Group/summarise entries and return a preview without persisting anything."""
        grouping = self.config.group_by if group_by is None else group_by
        grouping = self._resolve_grouping(grouping)
        grouping_name = grouping if isinstance(grouping, str) else CUSTOM_GROUP
        selected, warnings = self._normalize(entries)
        chapters, truncated = self._build_chapters(selected, grouping)
        if truncated:
            warnings = (*warnings, f"{truncated} 条章节摘要超过长度上限，已截断")
        preview = AutobiographyPreview(
            preview_id=self._id_factory(),
            scope=self.scope,
            created_at=self._timestamp(),
            group_by=grouping_name,
            chapters=chapters,
            originals=selected,
            warnings=warnings,
            entry_count=len(selected),
        )
        with self._lock:
            limit = int(self.config.max_pending_previews)
            while len(self._pending) >= limit:
                self._pending.pop(next(iter(self._pending)))
            self._pending[preview.preview_id] = preview
        return preview

    # ------------------------------------------------------------------ confirm
    def confirm(self, preview_id: str) -> AutobiographyRecord:
        """Persist one preview through the injected writer; called only by an explicit user action."""
        with self._lock:
            if preview_id in self._confirming:
                raise AutobiographyError("PREVIEW_CONFIRMING", "这份自传草稿正在提交中", 409)
            if preview_id in self._confirmed:
                raise AutobiographyError("ALREADY_CONFIRMED", "这份自传草稿已经确认过", 409)
            preview = self._pending.get(preview_id)
            if preview is None:
                raise AutobiographyError("PREVIEW_NOT_FOUND", "自传草稿不存在或已过期", 404)
            if preview.is_empty:
                raise AutobiographyError("EMPTY_PREVIEW", "没有可整理的记忆，无法生成自传", 422)
            created_at = self._timestamp()
            self._confirming.add(preview_id)
            self._pending.pop(preview_id, None)
        try:
            result = self._call_writer_commit(preview, created_at)
        except Exception:
            with self._lock:
                self._confirming.discard(preview_id)
                # The writer contract is atomic, so a failed commit left nothing
                # visible; restore the preview so the user can retry.
                self._pending[preview_id] = preview
            raise
        commit = AutobiographyCommit(
            record_id=self._id_factory(),
            preview_id=preview.preview_id,
            scope=preview.scope,
            originals=preview.originals,
            created_at=created_at,
            result=result,
        )
        record = AutobiographyRecord(
            record_id=commit.record_id,
            preview_id=preview.preview_id,
            scope=preview.scope,
            status="committed",
            created_at=created_at,
            chapter_count=len(preview.chapters),
            created_memory_ids=self._created_ids(result),
            original_ids=tuple(entry.memory_id for entry in preview.originals),
        )
        with self._lock:
            self._confirming.discard(preview_id)
            self._confirmed.add(preview_id)
            self._commits[commit.record_id] = commit
            self._records[commit.record_id] = record
        return record

    # --------------------------------------------------------------------- undo
    def undo(self, record_id: str) -> AutobiographyRecord:
        """Reverse one confirmed commit through the writer; originals are restored."""
        with self._lock:
            if record_id in self._reverting:
                raise AutobiographyError("RECORD_REVERTING", "这次自传提交正在撤销中", 409)
            record = self._records.get(record_id)
            if record is None:
                raise AutobiographyError("UNDO_NOT_AVAILABLE", "没有可撤销的自传提交记录", 404)
            if record.status == "reverted":
                raise AutobiographyError("ALREADY_UNDONE", "这次自传提交已经撤销过", 409)
            commit = self._commits.get(record_id)
            self._reverting.add(record_id)
        try:
            self._call_writer_revert(commit)
        except Exception:
            with self._lock:
                self._reverting.discard(record_id)
            raise
        reverted = replace(record, status="reverted", reverted_at=self._timestamp())
        with self._lock:
            self._reverting.discard(record_id)
            self._records[record_id] = reverted
        return reverted

    # ------------------------------------------------------------------ read side
    def get_preview(self, preview_id: str) -> AutobiographyPreview | None:
        with self._lock:
            return self._pending.get(preview_id)

    def get_record(self, record_id: str) -> AutobiographyRecord | None:
        with self._lock:
            return self._records.get(record_id)

    def pending_preview_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pending)

    def discard(self, preview_id: str) -> bool:
        """Drop a pending preview without writing anything (cancel action)."""
        with self._lock:
            return self._pending.pop(preview_id, None) is not None

    # ----------------------------------------------------------------- internals
    def _timestamp(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise AutobiographyError("INVALID_CLOCK", "clock 必须返回 datetime", 500)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _resolve_grouping(grouping: Any) -> Any:
        if callable(grouping):
            return grouping
        if isinstance(grouping, str) and grouping in GROUPINGS:
            return grouping
        raise AutobiographyError("INVALID_GROUPING", "分组方式未知", 422)

    def _normalize(self, entries: Iterable[Any] | None) -> tuple[tuple[MemoryEntry, ...], tuple[str, ...]]:
        if entries is None:
            return (), ()
        try:
            iterator = iter(entries)
        except TypeError as error:
            raise AutobiographyError("INVALID_ENTRY", "记忆条目集合不可迭代", 422) from error
        selected: list[MemoryEntry] = []
        seen: set[str] = set()
        blank = duplicates = 0
        for raw in iterator:
            entry = MemoryEntry.coerce(raw)
            if not entry.content.strip():
                blank += 1
                continue
            if entry.memory_id in seen:
                duplicates += 1
                continue
            seen.add(entry.memory_id)
            selected.append(entry)
        selected.sort(key=_entry_sort_key)
        if len(selected) > int(self.config.max_entries):
            raise AutobiographyError(
                "TOO_MANY_ENTRIES",
                f"一次最多整理 {int(self.config.max_entries)} 条记忆，当前 {len(selected)} 条",
                422,
            )
        warnings: list[str] = []
        if blank:
            warnings.append(f"已跳过 {blank} 条空白记忆")
        if duplicates:
            warnings.append(f"已忽略 {duplicates} 条重复记忆（同一 memory_id）")
        return tuple(selected), tuple(warnings)

    def _build_chapters(self, entries: tuple[MemoryEntry, ...], grouping: Any):
        if not entries:
            return (), 0
        groups = self._group_entries(entries, grouping)
        size = int(self.config.max_entries_per_chapter)
        chapters: list[AutobiographyChapter] = []
        truncated = 0
        ordinal = 0
        for label, title, group_entries in groups:
            chunks = [group_entries[index:index + size]
                      for index in range(0, len(group_entries), size)]
            for chunk_index, chunk in enumerate(chunks):
                ordinal += 1
                display_title = title if chunk_index == 0 else f"{title}（续{chunk_index}）"
                chapter, was_truncated = self._summarize_chunk(label, display_title,
                                                               chunk, ordinal, grouping)
                chapters.append(chapter)
                truncated += int(was_truncated)
        return tuple(chapters), truncated

    def _summarize_chunk(self, label: str, title: str, chunk: tuple[MemoryEntry, ...],
                         ordinal: int, grouping: Any) -> tuple[AutobiographyChapter, bool]:
        grouping_name = grouping if isinstance(grouping, str) else CUSTOM_GROUP
        request = SummaryRequest(
            chapter_key=f"{grouping_name}:{label}",
            title=title,
            entries=chunk,
            max_summary_chars=int(self.config.max_summary_chars),
            language=self.config.language,
            scope=self.scope,
        )
        try:
            draft = self.summarizer.summarize(request)
        except Exception as error:  # noqa: BLE001 - injected port boundary
            if isinstance(error, AppError):
                raise
            raise AutobiographyError(
                "SUMMARIZER_FAILED",
                "自传章节摘要生成失败：" + type(error).__name__,
                502,
            ) from error
        if isinstance(draft, str):
            draft = SummaryDraft(summary=draft)
        if not isinstance(draft, SummaryDraft) or not isinstance(draft.summary, str):
            raise AutobiographyError("SUMMARIZER_FAILED", "摘要器返回值不是 SummaryDraft", 502)
        summary = draft.summary.strip()
        if not summary:
            raise AutobiographyError("SUMMARIZER_FAILED", "摘要器返回了空摘要", 502)
        budget = int(self.config.max_summary_chars)
        truncated = False
        if len(summary) > budget:
            summary = summary[:budget - 1].rstrip() + "…"
            truncated = True
        chapter_title = (draft.title or "").strip() or title
        timestamps = [entry.created_at for entry in chunk if entry.created_at is not None]
        importance = round(sum(entry.importance for entry in chunk) / len(chunk), 4)
        chapter = AutobiographyChapter(
            chapter_id=f"{grouping_name}:{ordinal:02d}",
            title=chapter_title,
            summary=summary,
            entry_ids=tuple(entry.memory_id for entry in chunk),
            importance=_clamp(importance),
            confidence=_clamp(_as_float(draft.confidence, 0.5)),
            start_at=min(timestamps) if timestamps else None,
            end_at=max(timestamps) if timestamps else None,
        )
        return chapter, truncated

    def _group_entries(self, entries: tuple[MemoryEntry, ...], grouping: Any):
        """Return ordered ``(label, title, entries)`` groups; deterministic for equal input."""
        if callable(grouping):
            buckets: dict[str, list[MemoryEntry]] = {}
            labels: dict[str, str] = {}
            for entry in entries:
                key = grouping(entry)
                label = str(key).strip() if key is not None else ""
                label = label or CUSTOM_GROUP_TITLE
                labels[label] = label
                buckets.setdefault(label, []).append(entry)
            ordered = sorted(buckets, key=lambda label: (
                _entry_sort_key(buckets[label][0])[1], label))
            return [(label, labels[label], tuple(buckets[label])) for label in ordered]

        if grouping == GROUP_BY_KIND:
            buckets: dict[str, list[MemoryEntry]] = {}
            for entry in entries:
                buckets.setdefault(entry.kind or "other", []).append(entry)
            order = {kind: index for index, kind in enumerate(KIND_ORDER)}
            labels = sorted(buckets, key=lambda kind: (order.get(kind, len(KIND_ORDER)), kind))
            return [(kind, KIND_TITLES.get(kind, kind), tuple(buckets[kind])) for kind in labels]

        if grouping == GROUP_BY_SUBJECT:
            buckets: dict[str, list[MemoryEntry]] = {}
            for entry in entries:
                key = entry.subject_key.strip() or UNCLASSIFIED_SUBJECT
                buckets.setdefault(key, []).append(entry)
            ordered = sorted(buckets, key=lambda key: (
                key == UNCLASSIFIED_SUBJECT,
                "" if key == UNCLASSIFIED_SUBJECT else key,
            ))
            return [(key,
                     UNCLASSIFIED_SUBJECT_TITLE if key == UNCLASSIFIED_SUBJECT else key,
                     tuple(buckets[key]))
                    for key in ordered]

        # Default: chronological year chapters, unknown timestamps last.
        buckets: dict[str, list[MemoryEntry]] = {}
        years: dict[str, int] = {}
        for entry in entries:
            if entry.created_at is None:
                key = UNKNOWN_PERIOD
            else:
                key = str(entry.created_at.astimezone(self._zone).year)
                years[key] = int(key)
            buckets.setdefault(key, []).append(entry)
        ordered = sorted(buckets, key=lambda key: (
            key == UNKNOWN_PERIOD, years.get(key, 0), key))
        return [(key,
                 UNKNOWN_PERIOD_TITLE if key == UNKNOWN_PERIOD else key + "年",
                 tuple(buckets[key]))
                for key in ordered]

    # ------------------------------------------------------------------- writer
    def _call_writer_commit(self, preview: AutobiographyPreview,
                            created_at: datetime) -> Mapping[str, Any]:
        try:
            result = self.writer.commit(preview, created_at)
        except Exception as error:  # noqa: BLE001 - injected port boundary
            if isinstance(error, AppError):
                raise
            raise AutobiographyError(
                "CONFIRM_FAILED",
                "自传写入失败：" + type(error).__name__,
                500,
            ) from error
        if result is None:
            return {}
        if not isinstance(result, Mapping):
            raise AutobiographyError("CONFIRM_FAILED", "写入器返回值必须是映射", 500)
        return dict(result)

    def _call_writer_revert(self, commit: AutobiographyCommit | None) -> None:
        if commit is None:
            raise AutobiographyError("UNDO_NOT_AVAILABLE", "缺少可撤销的提交数据", 404)
        try:
            self.writer.revert(commit)
        except Exception as error:  # noqa: BLE001 - injected port boundary
            if isinstance(error, AppError):
                raise
            raise AutobiographyError(
                "UNDO_FAILED",
                "撤销自传失败：" + type(error).__name__,
                500,
            ) from error

    @staticmethod
    def _created_ids(result: Mapping[str, Any]) -> tuple[str, ...]:
        raw = result.get("created_memory_ids") or result.get("created") or ()
        if isinstance(raw, Mapping):
            raw = raw.keys()
        try:
            return tuple(str(item) for item in raw)
        except TypeError:
            return ()


# ---------------------------------------------------------------------------
# SQL persistence adapter (task-7b integration)
# ---------------------------------------------------------------------------
def _uuid_or_error(value: Any, field_name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        raise AutobiographyError("INVALID_SCOPE", f"scope.{field_name} 不是有效的 UUID", 422) from None


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _iso(value: Any) -> str | None:
    moment = _to_utc(value)
    return moment.isoformat() if moment else None


class SqlAutobiographyWriter:
    """Transactional ``memory_items`` adapter for confirmed autobiography previews.

    Row contract (mirrors :class:`InMemoryAutobiographyWriter`):

    * one new active summary row per chapter: ``kind='episode'``,
      ``epistemic_type='inferred'``, ``summary_level='autobiography'``,
      ``predicate='autobiography'``; pool/visibility are copied from ``preview.scope``
      (the API always passes owner-private scope);
    * originals are retired to ``status='superseded'`` with
      ``archived_at = valid_to = created_at``; their content is preserved, never deleted;
    * the originals' ``source_refs`` are copied onto the summary row and
      ``memory_relations`` records ``relation_type='summarizes'`` edges;
    * ``revert`` restores the exact status/archived_at/valid_to snapshot taken at
      commit (only while the row still carries this commit's retirement stamp) and
      marks the generated summaries ``reverted``.

    Both methods run inside one ``db.engine.begin()`` transaction under the companion
    advisory lock shared with extraction/consolidation, and enforce account/companion
    (plus user, when set) ownership with :func:`..identity.service.authorize` and per-row
    checks. Originals whose ``memory_id`` is not a real ``memory_items`` id (a retrieval
    ``chunk_id``, for example) are treated as snapshot-only and skipped.
    """

    def __init__(self, db, retention_days: Mapping[str, float] | None = None):
        self.db = db
        self.retention_days = dict(retention_days or {}) or None

    @staticmethod
    def _scope(scope: Mapping[str, Any] | None):
        if not isinstance(scope, Mapping):
            raise AutobiographyError("INVALID_SCOPE", "scope 必须是映射", 422)
        actor = Actor(account_id=_uuid_or_error(scope.get("account_id"), "account_id"),
                      user_id=_uuid_or_error(scope.get("user_id"), "user_id"))
        companion_id = _uuid_or_error(scope.get("companion_id"), "companion_id")
        visibility: dict[str, Any] = {}
        pool = scope.get("pool_kind")
        if pool not in (None, ""):
            if pool not in ("owner", "member"):
                raise AutobiographyError("INVALID_SCOPE", "scope.pool_kind 无效", 422)
            visibility["pool_kind"] = pool
        visible = scope.get("visibility")
        if visible not in (None, ""):
            if visible not in ("owner_private", "group_public"):
                raise AutobiographyError("INVALID_SCOPE", "scope.visibility 无效", 422)
            visibility["visibility"] = visible
        room = scope.get("visibility_room_id")
        if room not in (None, ""):
            visibility["visibility_room_id"] = _uuid_or_error(room, "visibility_room_id")
        origin_room = scope.get("origin_room_id")
        if origin_room not in (None, ""):
            visibility["origin_room_id"] = _uuid_or_error(origin_room, "origin_room_id")
        visibility.setdefault("pool_kind", "owner")
        visibility.setdefault("visibility", "owner_private")
        return actor, companion_id, visibility

    def commit(self, preview: AutobiographyPreview, created_at: datetime) -> Mapping[str, Any]:
        reference = _to_utc(created_at) or _utcnow()
        actor, companion_id, visibility = self._scope(preview.scope)
        requested = [str(entry.memory_id) for entry in preview.originals]
        requested_ids = [UUID(value) for value in requested if _is_uuid(value)]
        chapter_entry_ids = {str(memory_id) for chapter in preview.chapters for memory_id in chapter.entry_ids}
        unknown = chapter_entry_ids - set(requested)
        if unknown:
            raise AutobiographyError("INVALID_ENTRY", "章节引用了未包含在预览中的记忆", 422)
        result: dict[str, Any] = {"created_memory_ids": [], "original_states": {},
                                  "missing_original_ids": []}
        with self.db.engine.begin() as conn:
            lock(conn, companion_id)
            authorize(conn, actor, companion_id)
            rows: dict[str, Any] = {}
            if requested_ids:
                for row in conn.execute(select(mm.memory_items).where(
                        mm.memory_items.c.id.in_(requested_ids))).mappings():
                    rows[str(row["id"])] = row
            for row in rows.values():
                if row["account_id"] != actor.account_id or row["companion_id"] != companion_id:
                    raise AppError("FORBIDDEN", "无权整理该角色的记忆", 403)
                if row["user_id"] is not None and row["user_id"] != actor.user_id:
                    raise AppError("FORBIDDEN", "无权整理该用户的记忆", 403)
                if row["status"] != "active":
                    raise AutobiographyError("MEMORY_CHANGED", "记忆状态已变化，请重新生成自传草稿", 409)
            states = {memory_id: {"status": row["status"], "archived_at": _iso(row["archived_at"]),
                                  "valid_to": _iso(row["valid_to"])}
                      for memory_id, row in rows.items()}
            refs = self._load_refs(conn, [row["id"] for row in rows.values()])
            created: list[str] = []
            for chapter in preview.chapters:
                summary_id = uuid4()
                values = dict(lifecycle_values("episode", chapter.confidence, reference=reference,
                                               retention_days=self.retention_days))
                values["importance"] = _clamp(_as_float(chapter.importance, values["importance"]))
                conn.execute(mm.memory_items.insert().values(
                    id=summary_id, account_id=actor.account_id, companion_id=companion_id,
                    user_id=actor.user_id, kind=chapter.kind or AUTOBIOGRAPHY_KIND,
                    content=chapter.summary,
                    epistemic_type=chapter.epistemic_type or AUTOBIOGRAPHY_EPISTEMIC_TYPE,
                    confidence=_clamp(_as_float(chapter.confidence, 0.5)), status="active", revision=1,
                    subject_key="", predicate=AUTOBIOGRAPHY_PREDICATE, value="",
                    valid_from=_to_utc(chapter.start_at) or reference, valid_to=None,
                    is_pinned=False, summary_level=AUTOBIOGRAPHY_SUMMARY_LEVEL,
                    **visibility, **values))
                copied: set[tuple[Any, Any, Any]] = set()
                for entry_id in chapter.entry_ids:
                    original = rows.get(str(entry_id))
                    if original is None:
                        continue
                    conn.execute(mm.memory_relations.insert().values(
                        from_memory_id=summary_id, to_memory_id=original["id"],
                        relation_type="summarizes"))
                    for ref in refs.get(str(entry_id), ()):
                        key = (ref["event_id"], ref["start_offset"], ref["end_offset"])
                        if key in copied:
                            continue
                        copied.add(key)
                        conn.execute(mm.source_refs.insert().values(
                            memory_id=summary_id, event_id=ref["event_id"],
                            json_pointer=ref["json_pointer"], start_offset=ref["start_offset"],
                            end_offset=ref["end_offset"], quote_hash=ref["quote_hash"]))
                created.append(str(summary_id))
            retired = 0
            if rows:
                retired = conn.execute(mm.memory_items.update().where(
                    mm.memory_items.c.id.in_([row["id"] for row in rows.values()]),
                    mm.memory_items.c.status == "active").values(
                    status="superseded", archived_at=reference, valid_to=reference)).rowcount
            if retired != len(rows):
                raise AutobiographyError("MEMORY_CHANGED", "部分记忆状态已变化，请重新生成自传草稿", 409)
            result["created_memory_ids"] = created
            result["original_states"] = states
        return result

    @staticmethod
    def _load_refs(conn, memory_ids):
        by_memory: dict[str, list[Any]] = {}
        if memory_ids:
            for ref in conn.execute(select(mm.source_refs).where(
                    mm.source_refs.c.memory_id.in_(memory_ids))).mappings():
                by_memory.setdefault(str(ref["memory_id"]), []).append(dict(ref))
        return by_memory

    def revert(self, commit: AutobiographyCommit) -> None:
        actor, companion_id, _visibility = self._scope(commit.scope)
        payload = commit.result or {}
        created = [str(value) for value in payload.get("created_memory_ids") or ()
                   if _is_uuid(value)]
        states = payload.get("original_states") or {}
        reverted_at = _utcnow()
        with self.db.engine.begin() as conn:
            lock(conn, companion_id)
            authorize(conn, actor, companion_id)
            if created:
                conn.execute(mm.memory_items.update().where(
                    mm.memory_items.c.id.in_([UUID(value) for value in created]),
                    mm.memory_items.c.account_id == actor.account_id,
                    mm.memory_items.c.companion_id == companion_id,
                    mm.memory_items.c.summary_level == AUTOBIOGRAPHY_SUMMARY_LEVEL).values(
                    status="reverted", archived_at=reverted_at, valid_to=reverted_at))
            retired_at = _to_utc(commit.created_at)
            if isinstance(states, Mapping):
                for memory_id, state in states.items():
                    if not _is_uuid(memory_id) or not isinstance(state, Mapping):
                        continue
                    conn.execute(mm.memory_items.update().where(
                        mm.memory_items.c.id == UUID(str(memory_id)),
                        mm.memory_items.c.account_id == actor.account_id,
                        mm.memory_items.c.companion_id == companion_id,
                        mm.memory_items.c.status == "superseded",
                        mm.memory_items.c.archived_at == retired_at).values(
                        status=str(state.get("status") or "active"),
                        archived_at=_to_utc(state.get("archived_at")),
                        valid_to=_to_utc(state.get("valid_to"))))
