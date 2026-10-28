"""Unit tests for the isolated autobiography backend core.

No database, no network, no model call: every collaborator is either the bundled
deterministic summarizer / reference writer or a tiny local fake.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agi_wind.memory.autobiography import (
    AUTOBIOGRAPHY_EPISTEMIC_TYPE,
    AUTOBIOGRAPHY_KIND,
    AUTOBIOGRAPHY_SUMMARY_LEVEL,
    AutobiographyConfig,
    AutobiographyError,
    AutobiographyOrganizer,
    DeterministicSummarizer,
    InMemoryAutobiographyWriter,
    MemoryEntry,
    SummaryDraft,
)

CLOCK = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def entry(memory_id, content, created_at=None, kind="episode", importance=0.5,
          subject_key="", epistemic_type="user_asserted"):
    return MemoryEntry(memory_id=memory_id, content=content, created_at=created_at,
                       kind=kind, importance=importance, subject_key=subject_key,
                       epistemic_type=epistemic_type)


class Clock:
    def __init__(self, *values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


class RecordingSummarizer:
    def __init__(self, confidence=0.7):
        self.requests = []
        self.confidence = confidence

    def summarize(self, request):
        self.requests.append(request)
        return SummaryDraft(summary=f"摘要-{request.chapter_key}", confidence=self.confidence)


class FailingSummarizer:
    def summarize(self, request):
        raise RuntimeError("provider down")


class EmptySummarizer:
    def summarize(self, request):
        return SummaryDraft(summary="  ")


class CountingWriter:
    """Writer spy: counts calls without persisting anything."""

    def __init__(self):
        self.commit_calls = 0
        self.revert_calls = 0
        self.last_preview = None

    def commit(self, preview, created_at):
        self.commit_calls += 1
        self.last_preview = preview
        return {"created_memory_ids": ["summary-1"]}

    def revert(self, commit):
        self.revert_calls += 1


class FlakyWriter:
    def __init__(self):
        self.commit_calls = 0
        self.revert_calls = 0

    def commit(self, preview, created_at):
        self.commit_calls += 1
        if self.commit_calls == 1:
            raise RuntimeError("database unavailable")
        return {"created_memory_ids": ["summary-1"]}

    def revert(self, commit):
        self.revert_calls += 1


def organizer(writer, **kwargs):
    ids = iter([f"id-{index}" for index in range(1, 64)])
    kwargs.setdefault("scope", {"companion_id": "companion-1"})
    kwargs.setdefault("clock", lambda: CLOCK)
    kwargs.setdefault("id_factory", lambda: next(ids))
    return AutobiographyOrganizer(writer, **kwargs)


def lifecycle_entries():
    return [
        entry("m1", "第一次见面是在旧书店。", datetime(2023, 5, 1, tzinfo=timezone.utc),
              kind="episode"),
        entry("m2", "用户喜欢下雨天。", datetime(2024, 2, 1, tzinfo=timezone.utc),
              kind="preference", importance=0.8),
        entry("m3", "用户计划学习钢琴。", datetime(2024, 9, 1, tzinfo=timezone.utc),
              kind="plan"),
        entry("m4", "没有时间的备忘。", None, kind="fact"),
    ]


# --------------------------------------------------------------------- preview
def test_preview_groups_by_year_and_writes_nothing():
    writer = CountingWriter()
    summarizer = RecordingSummarizer()
    org = organizer(writer, summarizer=summarizer)

    preview = org.preview(lifecycle_entries())

    # No external write happened; the writer was never even called.
    assert writer.commit_calls == 0
    assert writer.revert_calls == 0
    assert preview.preview_id == "id-1"
    assert preview.created_at == CLOCK
    assert preview.group_by == "period"
    assert preview.entry_count == 4
    assert preview.chapter_count == 3
    assert [chapter.title for chapter in preview.chapters] == ["2023年", "2024年", "未标注时间"]
    assert [chapter.entry_ids for chapter in preview.chapters] == [
        ("m1",), ("m2", "m3"), ("m4",)]
    assert [chapter.chapter_id for chapter in preview.chapters] == [
        "period:01", "period:02", "period:03"]
    assert [request.chapter_key for request in summarizer.requests] == [
        "period:2023", "period:2024", "period:__unknown__"]
    assert [chapter.summary for chapter in preview.chapters] == [
        "摘要-period:2023", "摘要-period:2024", "摘要-period:__unknown__"]
    assert dict(preview.scope) == {"companion_id": "companion-1"}


def test_preview_preserves_originals_and_is_json_serialisable():
    writer = CountingWriter()
    org = organizer(writer, summarizer=RecordingSummarizer())
    entries = lifecycle_entries()

    preview = org.preview(entries)

    # Every original is kept verbatim; ordering is deterministic (chronological,
    # entries without a timestamp last, then id).
    assert [original.memory_id for original in preview.originals] == [
        "m1", "m2", "m3", "m4"]
    assert [original.content for original in preview.originals] == [
        item.content for item in entries]
    payload = preview.to_dict()
    json.dumps(payload)  # API preview must be JSON-safe
    assert payload["original_ids"] == ["m1", "m2", "m3", "m4"]
    assert payload["chapters"][1]["entry_ids"] == ["m2", "m3"]
    assert payload["warnings"] == []
    assert writer.commit_calls == 0


def test_preview_skips_blank_entries_and_deduplicates_ids():
    writer = CountingWriter()
    org = organizer(writer, summarizer=RecordingSummarizer())

    preview = org.preview([
        entry("m1", "有效记忆", CLOCK),
        entry("m1", "重复记忆", CLOCK),   # same id, must be ignored
        entry("m2", "   ", CLOCK),        # blank, must be skipped
        entry("m3", "另一条", CLOCK),
    ])

    assert preview.entry_count == 2
    assert [original.memory_id for original in preview.originals] == ["m1", "m3"]
    assert any("跳过 1 条空白" in warning for warning in preview.warnings)
    assert any("1 条重复" in warning for warning in preview.warnings)
    assert writer.commit_calls == 0


# --------------------------------------------------------------------- confirm
def test_confirm_is_the_only_write_and_keeps_originals_recoverable():
    writer = InMemoryAutobiographyWriter()
    entries = lifecycle_entries()
    writer.seed(entries)
    org = organizer(writer, summarizer=RecordingSummarizer())

    preview = org.preview(entries)

    assert writer.commit_calls == 0
    for item in entries:
        assert writer.entries[item.memory_id]["status"] == "active"

    record = org.confirm(preview.preview_id)

    assert writer.commit_calls == 1
    assert record.status == "committed"
    assert record.preview_id == preview.preview_id
    assert record.chapter_count == 3
    assert record.created_at == CLOCK
    assert record.created_memory_ids and len(record.created_memory_ids) == 3
    assert [writer.entries[identifier]["content"] for identifier in record.created_memory_ids] == [
        chapter.summary for chapter in preview.chapters]
    assert all(writer.entries[identifier]["kind"] == AUTOBIOGRAPHY_KIND
               for identifier in record.created_memory_ids)
    assert all(writer.entries[identifier]["epistemic_type"] == AUTOBIOGRAPHY_EPISTEMIC_TYPE
               for identifier in record.created_memory_ids)
    assert all(writer.entries[identifier]["summary_level"] == AUTOBIOGRAPHY_SUMMARY_LEVEL
               for identifier in record.created_memory_ids)
    assert writer.entries[record.created_memory_ids[0]]["source_memory_ids"] == ["m1"]

    # Originals are retired, never deleted, and keep their original content.
    for item in entries:
        row = writer.entries[item.memory_id]
        assert row["status"] == "superseded"
        assert row["content"] == item.content
        assert row["archived_at"] == CLOCK

    with pytest.raises(AutobiographyError) as repeated:
        org.confirm(record.preview_id)
    assert repeated.value.code == "ALREADY_CONFIRMED"
    assert repeated.value.status == 409
    assert writer.commit_calls == 1


def test_undo_restores_originals_and_deactivates_summaries():
    writer = InMemoryAutobiographyWriter()
    entries = lifecycle_entries()
    writer.seed(entries)
    org = organizer(writer, summarizer=RecordingSummarizer(),
                    clock=Clock(CLOCK, CLOCK + DAY, CLOCK + 2 * DAY))

    preview = org.preview(entries)
    record = org.confirm(preview.preview_id)
    assert record.created_at == CLOCK + DAY
    summary_ids = record.created_memory_ids

    reverted = org.undo(record.record_id)

    assert writer.revert_calls == 1
    assert reverted.status == "reverted"
    assert reverted.reverted_at == CLOCK + 2 * DAY
    for item in entries:
        assert writer.entries[item.memory_id]["status"] == "active"
        assert "archived_at" not in writer.entries[item.memory_id]
    assert all(writer.entries[identifier]["status"] == "reverted" for identifier in summary_ids)

    with pytest.raises(AutobiographyError) as repeated:
        org.undo(record.record_id)
    assert repeated.value.code == "ALREADY_UNDONE"
    with pytest.raises(AutobiographyError) as unknown:
        org.undo("missing-record")
    assert unknown.value.code == "UNDO_NOT_AVAILABLE"


def test_confirm_failure_wraps_error_and_keeps_preview_retryable():
    writer = FlakyWriter()
    org = organizer(writer, summarizer=RecordingSummarizer())
    preview = org.preview(lifecycle_entries())

    with pytest.raises(AutobiographyError) as failure:
        org.confirm(preview.preview_id)
    assert failure.value.code == "CONFIRM_FAILED"
    assert failure.value.status == 500
    assert preview.preview_id in org.pending_preview_ids()

    record = org.confirm(preview.preview_id)  # retry after the transient failure
    assert record.status == "committed"
    assert writer.commit_calls == 2
    assert org.pending_preview_ids() == ()


# ---------------------------------------------------------------- empty/errors
def test_empty_selection_returns_empty_preview_and_confirm_rejects():
    writer = CountingWriter()
    summarizer = RecordingSummarizer()
    org = organizer(writer, summarizer=summarizer)

    preview = org.preview([])
    assert preview.is_empty
    assert preview.chapter_count == 0
    assert preview.entry_count == 0
    assert preview.originals == ()
    assert summarizer.requests == []

    with pytest.raises(AutobiographyError) as failure:
        org.confirm(preview.preview_id)
    assert failure.value.code == "EMPTY_PREVIEW"
    assert failure.value.status == 422
    assert writer.commit_calls == 0

    whitespace = org.preview([entry("m1", "   \n ")])
    assert whitespace.is_empty
    assert any("跳过 1 条空白" in warning for warning in whitespace.warnings)


def test_invalid_entries_and_limits_raise_stable_errors():
    org = organizer(CountingWriter(), summarizer=RecordingSummarizer())

    for bad in ({"content": "缺少 id"}, object(), "not-an-entry", 42):
        with pytest.raises(AutobiographyError) as failure:
            org.preview([bad])
        assert failure.value.code == "INVALID_ENTRY"
        assert failure.value.status == 422

    with pytest.raises(AutobiographyError) as too_many:
        organizer(CountingWriter(), summarizer=RecordingSummarizer(),
                  config=AutobiographyConfig(max_entries=2)).preview(
            [entry(f"m{index}", "内容", CLOCK) for index in range(3)])
    assert too_many.value.code == "TOO_MANY_ENTRIES"

    with pytest.raises(AutobiographyError) as grouping:
        org.preview([entry("m1", "内容", CLOCK)], group_by="nope")
    assert grouping.value.code == "INVALID_GROUPING"

    with pytest.raises(AutobiographyError) as scope:
        AutobiographyOrganizer(CountingWriter(), scope=["not-a-mapping"])
    assert scope.value.code == "INVALID_SCOPE"


def test_summarizer_failure_is_wrapped_and_leaves_no_pending_preview():
    writer = CountingWriter()
    org = organizer(writer, summarizer=FailingSummarizer())

    with pytest.raises(AutobiographyError) as failure:
        org.preview(lifecycle_entries())
    assert failure.value.code == "SUMMARIZER_FAILED"
    assert failure.value.status == 502
    assert org.pending_preview_ids() == ()
    assert writer.commit_calls == 0

    with pytest.raises(AutobiographyError) as empty:
        organizer(writer, summarizer=EmptySummarizer()).preview(lifecycle_entries())
    assert empty.value.code == "SUMMARIZER_FAILED"


# -------------------------------------------------------------------- grouping
def test_kind_and_custom_grouping_are_deterministic():
    entries = [
        entry("m1", "喜欢海。", datetime(2023, 1, 1, tzinfo=timezone.utc), kind="preference"),
        entry("m2", "名字是小风。", datetime(2023, 2, 1, tzinfo=timezone.utc), kind="identity"),
        entry("m3", "住在杭州。", datetime(2023, 3, 1, tzinfo=timezone.utc), kind="fact"),
        entry("m4", "想学钢琴。", datetime(2023, 4, 1, tzinfo=timezone.utc), kind="plan"),
    ]
    org = organizer(CountingWriter(), summarizer=RecordingSummarizer())

    by_kind = org.preview(entries, group_by="kind")
    assert [chapter.title for chapter in by_kind.chapters] == ["身份", "偏好", "事实", "计划"]

    def era(item):
        return "早年" if item.created_at.year < 2024 else "近年"

    org = organizer(CountingWriter(), summarizer=RecordingSummarizer())
    by_era = org.preview(entries, group_by=era)
    assert by_era.group_by == "custom"
    assert [chapter.title for chapter in by_era.chapters] == ["早年"]
    assert [chapter.entry_ids for chapter in by_era.chapters] == [("m1", "m2", "m3", "m4")]

    # Same input in the same order yields byte-identical chapters (deterministic ids).
    first = organizer(CountingWriter(), summarizer=DeterministicSummarizer(),
                      clock=lambda: CLOCK).preview(lifecycle_entries())
    second = organizer(CountingWriter(), summarizer=DeterministicSummarizer(),
                       clock=lambda: CLOCK).preview(lifecycle_entries())
    assert first.chapters == second.chapters
    assert first.chapters[0].summary and "旧书店" in first.chapters[0].summary


def test_large_group_splits_into_continuation_chapters():
    entries = [
        entry("m1", "一", datetime(2024, 1, 1, tzinfo=timezone.utc)),
        entry("m2", "二", datetime(2024, 2, 1, tzinfo=timezone.utc)),
        entry("m3", "三", datetime(2024, 3, 1, tzinfo=timezone.utc)),
    ]
    org = organizer(CountingWriter(), summarizer=RecordingSummarizer(),
                    config=AutobiographyConfig(max_entries_per_chapter=2))

    preview = org.preview(entries)

    assert [chapter.title for chapter in preview.chapters] == ["2024年", "2024年（续1）"]
    assert [chapter.entry_ids for chapter in preview.chapters] == [("m1", "m2"), ("m3",)]
    assert [chapter.importance for chapter in preview.chapters] == [0.5, 0.5]


def test_discard_drops_pending_preview_without_writing():
    writer = CountingWriter()
    org = organizer(writer, summarizer=RecordingSummarizer())
    preview = org.preview(lifecycle_entries())

    assert org.discard(preview.preview_id) is True
    assert org.pending_preview_ids() == ()
    assert org.discard(preview.preview_id) is False
    with pytest.raises(AutobiographyError) as failure:
        org.confirm(preview.preview_id)
    assert failure.value.code == "PREVIEW_NOT_FOUND"
    assert writer.commit_calls == 0


# ----------------------------------------------------------------- entry coercion
def test_memory_entry_accepts_mapping_rows_and_retrieval_hits():
    memory_id = uuid4()
    mapped = MemoryEntry.from_mapping({
        "id": memory_id,
        "content": " 用户喜欢海 ",
        "kind": "preference",
        "importance": 1.7,
        "created_at": "2025-03-04T05:06:07Z",
        "is_pinned": 1,
        "subject_key": "user",
    })
    assert mapped.memory_id == str(memory_id)
    assert mapped.kind == "preference"
    assert mapped.importance == 1.0
    assert mapped.created_at == datetime(2025, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
    assert mapped.is_pinned is True

    hit = SimpleNamespace(
        memory_id=None,
        chunk_id=uuid4(),
        content="检索命中的片段",
        epistemic_type="user_asserted",
        time_range=(datetime(2025, 1, 1, tzinfo=timezone.utc),
                    datetime(2025, 1, 2, tzinfo=timezone.utc)),
    )
    coerced = MemoryEntry.coerce(hit)
    assert coerced.memory_id == str(hit.chunk_id)
    assert coerced.created_at == hit.time_range[0]
    assert coerced.epistemic_type == "user_asserted"
    assert MemoryEntry.coerce(mapped) is mapped
