"""Unit-level contract tests for bounded memory cards and the archive flow.

These tests use the repository PostgreSQL fixture (``db``) because the card
service is a SQLAlchemy component. Network, LLM and FastAPI are never touched.
"""

from uuid import uuid4

import pytest

from nagi.archive import models as m
from nagi.core.errors import AppError
from nagi.memory import models as mm
from nagi.memory.cards import (
    ARCHIVE_REASON_FORGET,
    ARCHIVE_REASON_MERGE,
    DEFAULT_BUDGET_CHARS,
    EMPTY_CARD,
    MIN_BUDGET_CHARS,
    PIN_MARKER,
    TABLE_DEFINITION,
    MemoryCardError,
    MemoryCardService,
    ensure_archive_table,
    memory_archive,
)


@pytest.fixture
def cards(db):
    ensure_archive_table(db.engine)
    try:
        yield MemoryCardService(db)
    finally:
        memory_archive.drop(db.engine, checkfirst=True)


def add_memory(
    db,
    scope,
    content,
    *,
    kind="fact",
    importance=0.5,
    pinned=False,
    epistemic_type="extracted",
    with_source_event=False,
):
    memory_id = uuid4()
    event_id = None
    with db.engine.begin() as conn:
        conn.execute(
            mm.memory_items.insert().values(
                id=memory_id,
                account_id=scope.account_id,
                companion_id=scope.companion_id,
                user_id=getattr(scope, "user_id", None),
                kind=kind,
                content=content,
                epistemic_type=epistemic_type,
                importance=importance,
                is_pinned=pinned,
            )
        )
        if with_source_event:
            event_id = uuid4()
            conn.execute(
                m.events.insert().values(
                    id=event_id,
                    account_id=scope.account_id,
                    companion_id=scope.companion_id,
                    conversation_id=scope.conversation_id,
                    role="user",
                    kind="message",
                    content={"text": content},
                    content_sha256="a" * 64,
                )
            )
            conn.execute(
                mm.source_refs.insert().values(
                    memory_id=memory_id,
                    event_id=event_id,
                    json_pointer="/text",
                )
            )
    return memory_id, event_id


def archive_rows(db, **filters):
    return db.all("memory_archive", **filters)


def test_render_pinned_then_importance_within_budget(cards, db, seeded_scope):
    pinned, _ = add_memory(
        db, seeded_scope, "星星一样的猫", kind="fact", importance=0.1, pinned=True
    )
    important, _ = add_memory(
        db, seeded_scope, "喜欢海边日落", kind="preference", importance=0.9
    )
    low, _ = add_memory(db, seeded_scope, "去年搬家", kind="episode", importance=0.2)

    text = cards.render(seeded_scope, 400)

    assert len(text) <= 400
    assert text.startswith("记忆卡片")
    assert PIN_MARKER in text
    assert "【事实】" in text and "【偏好】" in text and "【经历】" in text
    assert text.index("星星一样的猫") < text.index("喜欢海边日落") < text.index("去年搬家")
    # A single render must not expose any of the source ids.
    assert str(pinned) not in text and str(important) not in text and str(low) not in text


def test_render_respects_small_budget_and_empty_scope(cards, db, seeded_scope, actor):
    for index in range(8):
        add_memory(
            db,
            seeded_scope,
            f"第{index}条比较长的记忆内容用于挤压容量预算",
            kind="fact",
            importance=0.4 + index / 100,
        )

    text = cards.render(seeded_scope, 120)
    assert len(text) <= 120
    assert text.count("- ") < 8

    empty_scope = db.seed(actor)
    empty = cards.render(empty_scope, DEFAULT_BUDGET_CHARS)
    assert EMPTY_CARD in empty
    assert len(empty) <= DEFAULT_BUDGET_CHARS


def test_render_never_includes_dates_or_anniversaries(cards, db, seeded_scope):
    add_memory(db, seeded_scope, "喜欢雨天散步", kind="preference")
    add_memory(db, seeded_scope, "养了一只橘猫", kind="fact")
    add_memory(db, seeded_scope, "一起看过海", kind="episode")
    add_memory(db, seeded_scope, "和邻居关系很好", kind="relationship")

    text = cards.render(seeded_scope, 800)

    for marker in ("周年", "纪念日", "生日", "2026-", "2025-", "2024-"):
        assert marker not in text
    import re

    assert re.search(r"\d{4}[-年]\d{1,2}", text) is None
    assert re.search(r"\d{1,2}月\d{1,2}日", text) is None


def test_render_scope_isolation(cards, db, seeded_scope, actor):
    add_memory(db, seeded_scope, "只属于第一个角色的记忆")
    other_scope = db.seed(actor)
    add_memory(db, other_scope, "只属于第二个角色的记忆")

    first = cards.render(seeded_scope, DEFAULT_BUDGET_CHARS)
    second = cards.render(other_scope, DEFAULT_BUDGET_CHARS)

    assert "只属于第一个角色的记忆" in first and "只属于第二个角色的记忆" not in first
    assert "只属于第二个角色的记忆" in second and "只属于第一个角色的记忆" not in second


def test_forget_archives_without_leaving_active_row_then_undo_restores(cards, db, seeded_scope):
    memory_id, event_id = add_memory(
        db, seeded_scope, "我养了一只猫", kind="fact", with_source_event=True
    )

    result = cards.forget(memory_id, seeded_scope)

    assert result["memory_id"] == str(memory_id)
    assert db.all("memory_items", id=memory_id) == []
    archived = archive_rows(db, source_memory_id=memory_id)
    assert len(archived) == 1
    assert archived[0]["kind"] == "fact"
    assert archived[0]["content"] == "我养了一只猫"
    assert archived[0]["scope"] == "owner_private"
    assert archived[0]["metadata_json"]["reason"] == ARCHIVE_REASON_FORGET
    assert cards.render(seeded_scope, DEFAULT_BUDGET_CHARS).count("è") == 0

    restored = cards.undo(archived[0]["id"])

    assert restored["restored"] is True
    row = db.all("memory_items", id=memory_id)[0]
    assert row["content"] == "我养了一只猫"
    assert row["kind"] == "fact"
    assert row["status"] == "active"
    assert row["archived_at"] is None
    refs = db.all("source_refs", memory_id=memory_id)
    assert len(refs) == 1 and refs[0]["event_id"] == event_id

    with pytest.raises(MemoryCardError) as error:
        cards.undo(archived[0]["id"])
    assert error.value.code == "ALREADY_RESTORED"


def test_forget_rejects_foreign_scope_and_unknown_ids(cards, db, seeded_scope, actor):
    other_scope = db.seed(actor)
    memory_id, _ = add_memory(db, other_scope, "别人的记忆")

    with pytest.raises(MemoryCardError) as error:
        cards.forget(memory_id, seeded_scope)
    assert error.value.code == "MEMORY_NOT_FOUND"
    assert error.value.status == 404

    with pytest.raises(MemoryCardError) as error:
        cards.forget(uuid4())
    assert error.value.code == "MEMORY_NOT_FOUND"

    with pytest.raises(MemoryCardError) as error:
        cards.undo(uuid4())
    assert error.value.code == "ARCHIVE_NOT_FOUND"


def test_merge_creates_one_episode_and_archives_sources(cards, db, seeded_scope):
    first, _ = add_memory(db, seeded_scope, "养了一只猫", kind="fact", importance=0.6)
    second, _ = add_memory(
        db, seeded_scope, "喜欢喝咖啡", kind="preference", importance=0.9
    )

    merged = cards.merge(
        [first, second], title="共同记忆", content="猫和咖啡的日常", scope=seeded_scope
    )

    assert merged["memory_id"]
    row = db.all("memory_items", id=merged["memory_id"])[0]
    assert row["kind"] == "episode"
    assert "猫和咖啡的日常" in row["content"]
    assert "共同记忆" in row["content"]
    assert row["status"] == "active"
    assert db.all("memory_items", id=first) == []
    assert db.all("memory_items", id=second) == []

    archived = archive_rows(db, source_memory_id=first) + archive_rows(
        db, source_memory_id=second
    )
    assert len(archived) == 2
    assert all(row["metadata_json"]["reason"] == ARCHIVE_REASON_MERGE for row in archived)
    assert len(merged["archive_ids"]) == 2

    hits = cards.full_search("猫和咖啡", seeded_scope)
    assert any(hit["source"] == "active" and hit["memory_id"] == merged["memory_id"] for hit in hits)


def test_merge_undo_restores_source(cards, db, seeded_scope):
    first, _ = add_memory(db, seeded_scope, "养了一只猫", kind="fact")
    second, _ = add_memory(db, seeded_scope, "喜欢喝咖啡", kind="preference")

    merged = cards.merge([first, second], content="猫和咖啡", scope=seeded_scope)
    archive_id = merged["archive_ids"][0]

    cards.undo(archive_id)

    restored = db.all("memory_items", id=first) + db.all("memory_items", id=second)
    assert len(restored) == 1
    assert restored[0]["kind"] in ("fact", "preference")
    assert restored[0]["status"] == "active"
    assert db.all("memory_items", id=merged["memory_id"]) != []


def test_merge_rejects_cross_scope_and_too_few(cards, db, seeded_scope, actor):
    mine, _ = add_memory(db, seeded_scope, "我的记忆")
    other_scope = db.seed(actor)
    theirs, _ = add_memory(db, other_scope, "别人的记忆")

    with pytest.raises(MemoryCardError) as error:
        cards.merge([mine, theirs])
    assert error.value.code == "MERGE_SCOPE_MISMATCH"
    assert error.value.status == 409

    with pytest.raises(MemoryCardError) as error:
        cards.merge([mine])
    assert error.value.code == "INVALID_MERGE"


def test_full_search_covers_active_and_archived_with_scope(cards, db, seeded_scope, actor):
    add_memory(db, seeded_scope, "喜欢雨天散步")
    forgotten, _ = add_memory(db, seeded_scope, "喜欢海边日落", kind="episode")
    cards.forget(forgotten, seeded_scope)
    other_scope = db.seed(actor)
    add_memory(db, other_scope, "喜欢海边公寓")

    active_hits = cards.full_search("雨天", seeded_scope)
    archived_hits = cards.full_search("海边", seeded_scope)
    other_hits = cards.full_search("海边", other_scope)

    assert [hit["source"] for hit in active_hits] == ["active"]
    assert any(hit["source"] == "archived" for hit in archived_hits)
    assert all("日落" in hit["content"] for hit in archived_hits)
    assert len(other_hits) == 1 and "公寓" in other_hits[0]["content"]

    with pytest.raises(MemoryCardError) as error:
        cards.full_search("   ")
    assert error.value.code == "INVALID_QUERY"


def test_status_counts_and_defaults(cards, db, seeded_scope):
    first, _ = add_memory(db, seeded_scope, "钉住的记忆", kind="fact", pinned=True)
    second, _ = add_memory(db, seeded_scope, "普通记忆", kind="preference")
    cards.forget(second, seeded_scope)

    status = cards.status(seeded_scope)

    assert status["enabled"] is True
    assert status["table"] == "memory_archive"
    assert status["active"] == 1
    assert status["archived"] == 1
    assert status["pinned"] == 1
    assert status["kinds"] == {"fact": 1}
    assert status["sections"]["事实"] == 1
    assert db.all("memory_items", id=first)[0]["is_pinned"] is True


def test_invalid_scope_budget_and_session_factory_argument(cards, db, seeded_scope):
    with pytest.raises(MemoryCardError) as error:
        cards.render(None)
    assert error.value.code == "INVALID_SCOPE"

    with pytest.raises(MemoryCardError) as error:
        cards.render(seeded_scope, MIN_BUDGET_CHARS - 1)
    assert error.value.code == "INVALID_BUDGET"

    with pytest.raises(MemoryCardError) as error:
        cards.render({"account_id": str(seeded_scope.account_id)})
    assert error.value.code == "INVALID_SCOPE"

    # The service accepts a bare zero-argument session factory as well as Database.
    add_memory(db, seeded_scope, "工厂模式也能渲染")
    factory_service = MemoryCardService(lambda: db.engine.begin())
    assert "工厂模式也能渲染" in factory_service.render(seeded_scope, 400)


def test_table_definition_matches_declared_archive_table():
    assert TABLE_DEFINITION["table"] == "memory_archive"
    declared = {column["name"] for column in TABLE_DEFINITION["columns"]}
    actual = set(memory_archive.c.keys())
    assert declared == actual
    assert {"account_id", "companion_id", "source_memory_id", "kind", "content"} <= actual
    assert {"scope", "importance", "metadata_json", "archived_at"} <= actual
    assert AppError is MemoryCardError.__mro__[1]
