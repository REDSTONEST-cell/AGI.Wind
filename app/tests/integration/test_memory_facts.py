"""Structured fact safety, exercised against the isolated PostgreSQL schema fixture."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agi_wind.archive import models as m
from agi_wind.archive.repository import digest, lock
from agi_wind.memory import models as mm
from agi_wind.memory.extraction import ExtractedCandidate, ExtractedSourceRef, MemoryWriter


def event(db, scope, text, *, role="user", at=None):
    with db.engine.begin() as conn:
        return dict(conn.execute(m.events.insert().values(
            id=uuid4(), account_id=scope.account_id, companion_id=scope.companion_id,
            conversation_id=scope.conversation_id, role=role, kind="message",
            content={"text": text}, content_sha256=digest({"text": text}),
            **({"recorded_at": at} if at else {}),
        ).returning(m.events)).mappings().one())


def candidate(source, value="咖啡", content=None, **kw):
    return ExtractedCandidate(
        kind="preference", subject_key=kw.pop("subject_key", "user:self"),
        predicate=kw.pop("predicate", "drink"), value=value,
        content=content or f"用户喜欢{value}", epistemic_type=kw.pop("epistemic_type", "user_asserted"),
        source_refs=[ExtractedSourceRef(event_id=source["id"], quote=source["content"]["text"],
                                        relation=kw.pop("relation", "supports"))], **kw,
    )


def commit(db, scope, candidates, sources, **kw):
    with db.engine.begin() as conn:
        lock(conn, scope.companion_id)
        revision = conn.execute(m.companions.select().where(m.companions.c.id == scope.companion_id)).mappings().one()["memory_revision"]
        return MemoryWriter(db).commit_candidates(conn, scope, candidates, sources, revision, **kw)


def test_semantic_fields_and_paraphrase_reinforcement(db, seeded_scope):
    first = event(db, seeded_scope, "我喜欢咖啡")
    commit(db, seeded_scope, [candidate(first, subject_key=" User:Self ", predicate=" Drink ")], [first])
    original = db.all("memory_items")[0]
    assert (original["subject_key"], original["predicate"], original["value"]) == ("user:self", "drink", "咖啡")
    second = event(db, seeded_scope, "咖啡一直是我的最爱")
    result = commit(db, seeded_scope, [candidate(second, content="用户最爱的饮品为咖啡")], [second])
    assert result["stored"] == 0 and result["reinforced"] == 1
    assert db.count("memory_items") == 1 and db.count("source_refs") == 2
    assert db.all("memory_items")[0]["id"] == original["id"]


def test_explicit_preference_change_closes_history_and_bumps_once(db, seeded_scope):
    first_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    second_at = first_at + timedelta(days=2)
    first = event(db, seeded_scope, "我喜欢咖啡", at=first_at)
    commit(db, seeded_scope, [candidate(first)], [first])
    old = db.all("memory_items")[0]
    changed = event(db, seeded_scope, "我不再喝咖啡，改喝茶", at=second_at)
    result = commit(db, seeded_scope, [candidate(changed, "茶")], [changed])
    old = db.all("memory_items", id=old["id"])[0]
    new = next(row for row in db.all("memory_items") if row["id"] != old["id"])
    assert old["status"] == "superseded" and old["valid_to"] == second_at
    assert old["archived_at"] is not None and old["valid_from"] == first_at
    assert new["status"] == "active" and new["valid_from"] == second_at
    assert db.all("memory_relations")[0]["relation_type"] == "supersedes"
    assert result["memory_revision"] == 3


@pytest.mark.parametrize("role,epistemic,text,relation", [
    ("user", "user_asserted", "我喜欢茶", "supports"),
    ("user", "user_asserted", "我喜欢茶", "correction"),
    ("assistant", "assistant_generated", "我不再喝咖啡，改喝茶", "correction"),
    ("user", "inferred", "我不再喝咖啡，改喝茶", "correction"),
    ("user", "user_asserted", "假设我不再喝咖啡，改喝茶", "correction"),
    ("user", "user_asserted", "我没改喝茶", "correction"),
    ("user", "user_asserted", "我想改喝茶", "correction"),
    ("user", "user_asserted", "我改喝茶吗？", "correction"),
    ("user", "user_asserted", "我改叫小张，我喜欢茶", "correction"),
    ("user", "user_asserted", "我不再喝咖啡，朋友喜欢茶", "correction"),
    ("user", "user_asserted", "我不再喝咖啡，改喝茶", "contradicts"),
])
def test_ambiguous_or_lower_authority_change_never_supersedes_user(db, seeded_scope, role, epistemic, text, relation):
    first = event(db, seeded_scope, "我喜欢咖啡")
    commit(db, seeded_scope, [candidate(first)], [first])
    old = db.all("memory_items")[0]
    second = event(db, seeded_scope, text, role=role)
    commit(db, seeded_scope, [candidate(second, "茶", epistemic_type=epistemic, relation=relation)], [second])
    assert db.all("memory_items", id=old["id"])[0]["status"] == "active"
    pending = next(row for row in db.all("memory_items") if row["id"] != old["id"])
    assert pending["status"] == "disputed"
    assert db.all("memory_relations")[0]["relation_type"] == "contradicts"


@pytest.mark.parametrize("status", ["hidden", "expired", "superseded", "disputed", "dormant"])
def test_semantic_paraphrase_cannot_revive_inactive_memory(db, seeded_scope, status):
    first = event(db, seeded_scope, "我喜欢咖啡")
    commit(db, seeded_scope, [candidate(first)], [first])
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.update().values(status=status))
    second = event(db, seeded_scope, "咖啡是最爱")
    result = commit(db, seeded_scope, [candidate(second, content="最爱咖啡")], [second])
    assert result["ignored"] == 1 and db.count("memory_items") == 1
    assert db.all("memory_items")[0]["status"] == status


def test_missing_semantic_value_and_distinct_subjects_do_not_merge(db, seeded_scope):
    first = event(db, seeded_scope, "我喜欢咖啡，朋友也喜欢")
    candidates = [candidate(first, value="", content="用户喜欢咖啡"),
                  candidate(first, value="", content="用户偏爱咖啡"),
                  candidate(first, subject_key="user:friend-a"),
                  candidate(first, subject_key="user:friend:a")]
    result = commit(db, seeded_scope, candidates, [first])
    assert result["stored"] == 4 and result["memory_revision"] == 2


async def test_manual_correction_preserves_slot_but_clears_stale_value(db, actor, seeded_scope):
    from agi_wind.memory.corrections import CorrectionService
    first = event(db, seeded_scope, "我喜欢咖啡")
    commit(db, seeded_scope, [candidate(first)], [first])
    old = db.all("memory_items")[0]
    result = await CorrectionService(db).apply_correction(actor, old["id"], "现在改喝茶", first["id"])
    replacement = db.all("memory_items", id=result["memory_id"])[0]
    assert replacement["subject_key"] == "user:self" and replacement["predicate"] == "drink"
    assert replacement["value"] == ""  # Free-form corrections cannot safely infer a new value.
    assert replacement["valid_from"] is not None
    assert db.all("memory_items", id=old["id"])[0]["valid_to"] == replacement["valid_from"]


def test_pinned_survives_expiry_maintenance_and_access(db, seeded_scope):
    from agi_wind.memory.lifecycle import freshness, extend_after_access, maintain
    first = event(db, seeded_scope, "我喜欢咖啡")
    commit(db, seeded_scope, [candidate(first)], [first])
    now = datetime.now(timezone.utc)
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.update().values(is_pinned=True, created_at=now - timedelta(days=900), expires_at=now - timedelta(days=1)))
    row = db.all("memory_items")[0]
    assert freshness(row, now) == 1.0
    assert extend_after_access(row, 2, now)["expires_at"] is None
    assert maintain(db, seeded_scope.companion_id, now)["expired"] == 0
    assert db.all("memory_items")[0]["status"] == "active"


def test_retention_and_unpin_expiry_policy(db, seeded_scope):
    from agi_wind.memory.lifecycle import pin_values, maintain
    source = event(db, seeded_scope, "我喜欢咖啡")
    with db.engine.begin() as conn:
        lock(conn, seeded_scope.companion_id)
        MemoryWriter(db, retention_days={"preference": 2}).commit_candidates(conn, seeded_scope, [candidate(source)], [source], 1)
    row = db.all("memory_items")[0]
    assert 1 <= row["ttl_days"] <= 3
    now = datetime.now(timezone.utc)
    assert pin_values(row, True, reference=now)["expires_at"] is None
    values = pin_values({**row, "is_pinned": True}, False, reference=now, retention_days={"preference": 5})
    assert values["is_pinned"] is False and now < values["expires_at"] < now + timedelta(days=8)
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.update().values(created_at=now - timedelta(days=3), expires_at=now + timedelta(days=80)))
    assert maintain(db, seeded_scope.companion_id, now, retention_days={"preference": 1})["expired"] == 1


def test_server_summary_level_uses_same_provenance_gate(db, seeded_scope):
    source = event(db, seeded_scope, "今天去海边散步")
    c = candidate(source, value="", predicate="", content="今天去海边散步")
    result = commit(db, seeded_scope, [c], [source], summary_level="daily")
    assert result["stored"] == 1 and db.all("memory_items")[0]["summary_level"] == "daily"
    with pytest.raises(ValueError):
        commit(db, seeded_scope, [c], [source], summary_level="model_chosen")


def test_exact_legacy_duplicate_gains_verified_semantic_fields(db, seeded_scope):
    source = event(db, seeded_scope, "我喜欢咖啡")
    commit(db, seeded_scope, [candidate(source, value="", predicate="", content="用户喜欢咖啡")], [source])
    old_id = db.all("memory_items")[0]["id"]
    commit(db, seeded_scope, [candidate(source)], [source])
    assert db.count("memory_items") == 1
    upgraded = db.all("memory_items", id=old_id)[0]
    assert (upgraded["predicate"], upgraded["value"]) == ("drink", "咖啡")


def test_older_source_cannot_supersede_newer_user_assertion(db, seeded_scope):
    now = datetime.now(timezone.utc)
    current = event(db, seeded_scope, "我喜欢咖啡", at=now)
    commit(db, seeded_scope, [candidate(current)], [current])
    older = event(db, seeded_scope, "我不再喝咖啡，改喝茶", at=now - timedelta(days=1))
    commit(db, seeded_scope, [candidate(older, "茶")], [older])
    coffee = next(row for row in db.all("memory_items") if row["value"] == "咖啡")
    assert coffee["status"] == "active" and coffee["valid_to"] is None
    assert next(row for row in db.all("memory_items") if row["value"] == "茶")["status"] == "disputed"


async def test_hidden_pinned_manual_correction_keeps_hidden_and_pinned(db, actor, seeded_scope):
    from agi_wind.memory.corrections import CorrectionService
    source = event(db, seeded_scope, "我喜欢咖啡")
    commit(db, seeded_scope, [candidate(source)], [source])
    old_id = db.all("memory_items")[0]["id"]
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.update().values(status="hidden", is_pinned=True, expires_at=None))
    result = await CorrectionService(db).apply_correction(actor, old_id, "我改喝茶", source["id"])
    row = db.all("memory_items", id=result["memory_id"])[0]
    assert row["status"] == "hidden" and row["is_pinned"] and row["expires_at"] is None
