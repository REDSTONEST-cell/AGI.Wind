"""Focused task-7b tests: SQL autobiography writer + owner-private API wiring.

These tests exercise the real PostgreSQL schema (``db`` fixture) and the FastAPI
routes through ASGI transport. No LLM or network calls are involved.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from nagi.api.app import create_app
from nagi.archive import models as m
from nagi.core.errors import AppError
from nagi.core.settings import Settings, apply_persisted_settings, persist_settings
from nagi.core.types import Actor
from nagi.memory import models as mm
from nagi.memory.autobiography import AutobiographyOrganizer, MemoryEntry, SqlAutobiographyWriter

AUTH = {"Authorization": "Bearer synthetic-autobiography"}


def _now():
    return datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _scope_mapping(scope):
    return {"account_id": str(scope.account_id), "user_id": str(scope.user_id),
            "companion_id": str(scope.companion_id), "pool_kind": "owner", "visibility": "owner_private"}


def _organizer(db, scope):
    return AutobiographyOrganizer(SqlAutobiographyWriter(db), scope=_scope_mapping(scope), clock=_now)


def _insert_memory(conn, scope, content, *, memory_id=None, **overrides):
    memory_id = memory_id or uuid.uuid4()
    values = dict(id=memory_id, account_id=scope.account_id, companion_id=scope.companion_id,
                  user_id=scope.user_id, kind="fact", content=content, epistemic_type="explicit",
                  confidence=1.0, status="active", revision=1, importance=0.5, summary_level="",
                  pool_kind="owner", visibility="owner_private")
    values.update(overrides)
    conn.execute(mm.memory_items.insert().values(**values))
    return memory_id


def _seed_memories(db, scope, contents=("我养了一只猫", "我喜欢清晨散步")):
    with db.engine.begin() as conn:
        return [_insert_memory(conn, scope, content) for content in contents]


def _seed_event_and_ref(db, scope, memory_id):
    event_id = uuid.uuid4()
    with db.engine.begin() as conn:
        conn.execute(m.events.insert().values(
            id=event_id, account_id=scope.account_id, companion_id=scope.companion_id,
            conversation_id=scope.conversation_id, role="user", kind="message",
            content={"text": "今天和猫散步"}, content_sha256="a" * 64, visibility="visible"))
        conn.execute(mm.source_refs.insert().values(
            memory_id=memory_id, event_id=event_id, json_pointer="/text"))
    return event_id


def _entries(ids, contents):
    return [MemoryEntry(memory_id=str(memory_id), content=content, kind="fact",
                        created_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc))
            for memory_id, content in zip(ids, contents)]


def test_settings_persist_autobiography_and_framework_flags(tmp_path):
    settings = Settings(access_token="synthetic-autobiography", data_dir=tmp_path)
    settings.memory_autobiography_enabled = True
    settings.memory_framework = {"enabled": False, "adapter": "letta"}
    persist_settings(settings)

    reloaded = apply_persisted_settings(
        Settings(access_token="synthetic-autobiography", data_dir=tmp_path))
    assert reloaded.memory_autobiography_enabled is True
    assert reloaded.memory_framework == {"enabled": False, "adapter": "letta"}


def test_sql_writer_commit_copies_refs_and_undo_restores_originals(db, seeded_scope):
    ids = _seed_memories(db, seeded_scope)
    event_id = _seed_event_and_ref(db, seeded_scope, ids[0])
    organizer = _organizer(db, seeded_scope)

    preview = organizer.preview(_entries(ids, ("我养了一只猫", "我喜欢清晨散步")))
    assert preview.entry_count == 2 and not preview.is_empty

    record = organizer.confirm(preview.preview_id)
    assert record.status == "committed"
    assert len(record.created_memory_ids) == 1
    summary_id = uuid.UUID(record.created_memory_ids[0])

    with db.engine.connect() as conn:
        summary = conn.execute(select(mm.memory_items).where(
            mm.memory_items.c.id == summary_id)).mappings().one()
        originals = {row["id"]: row for row in conn.execute(
            select(mm.memory_items).where(mm.memory_items.c.id.in_(ids))).mappings()}
        relations = conn.execute(select(mm.memory_relations).where(
            mm.memory_relations.c.from_memory_id == summary_id)).mappings().all()
        refs = conn.execute(select(mm.source_refs).where(
            mm.source_refs.c.memory_id == summary_id)).mappings().all()

    assert summary["kind"] == "episode"
    assert summary["epistemic_type"] == "inferred"
    assert summary["summary_level"] == "autobiography"
    assert summary["predicate"] == "autobiography"
    assert summary["status"] == "active"
    assert summary["pool_kind"] == "owner" and summary["visibility"] == "owner_private"
    assert summary["account_id"] == seeded_scope.account_id
    assert summary["companion_id"] == seeded_scope.companion_id
    for original in originals.values():
        # Never deleted: content is preserved and only lifecycle columns move.
        assert original["status"] == "superseded"
        assert original["archived_at"] is not None and original["valid_to"] is not None
    assert originals[ids[0]]["content"] == "我养了一只猫"
    assert originals[ids[0]]["archived_at"] == originals[ids[1]]["archived_at"]
    assert {row["to_memory_id"] for row in relations} == set(ids)
    assert {row["relation_type"] for row in relations} == {"summarizes"}
    assert len(refs) == 1 and refs[0]["event_id"] == event_id

    undone = organizer.undo(record.record_id)
    assert undone.status == "reverted"
    with db.engine.connect() as conn:
        reverted = conn.execute(select(mm.memory_items).where(
            mm.memory_items.c.id == summary_id)).mappings().one()
        restored = conn.execute(select(mm.memory_items).where(
            mm.memory_items.c.id.in_(ids))).mappings().all()
    assert reverted["status"] == "reverted"
    for original in restored:
        assert original["status"] == "active"
        assert original["archived_at"] is None and original["valid_to"] is None


def test_sql_writer_rejects_foreign_originals_without_writes(db, seeded_scope):
    foreign_scope = db.seed(Actor(account_id=uuid.uuid4(), user_id=uuid.uuid4()))
    foreign_ids = _seed_memories(db, foreign_scope, ("别人的记忆",))
    own_ids = _seed_memories(db, seeded_scope)

    organizer = _organizer(db, seeded_scope)
    preview = organizer.preview(_entries(own_ids + foreign_ids, ("我的记忆A", "我的记忆B", "别人的记忆")))
    with pytest.raises(AppError) as failure:
        organizer.confirm(preview.preview_id)
    assert failure.value.code == "FORBIDDEN"

    with db.engine.connect() as conn:
        summaries = conn.scalar(select(func.count()).select_from(mm.memory_items).where(
            mm.memory_items.c.summary_level == "autobiography"))
        statuses = conn.execute(select(mm.memory_items.c.status).where(
            mm.memory_items.c.id.in_(own_ids))).scalars().all()
    assert summaries == 0
    assert statuses and all(status == "active" for status in statuses)


async def test_api_preview_confirm_undo_happy_path(db, actor, seeded_scope, tmp_path):
    ids = _seed_memories(db, seeded_scope)
    settings = Settings(access_token="synthetic-autobiography", data_dir=tmp_path,
                        memory_autobiography_enabled=True)
    app = create_app(settings, db, actor)
    companion_id = str(seeded_scope.companion_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        status = await client.get("/api/v1/memory/autobiography/status",
                                  params={"companion_id": companion_id}, headers=AUTH)
        assert status.status_code == 200, status.text
        assert status.json()["enabled"] is True
        assert status.json()["pending_previews"] == 0
        assert status.json()["group_by"] == "period"

        created = await client.post("/api/v1/memory/autobiography/previews",
                                    json={"companion_id": companion_id}, headers=AUTH)
        assert created.status_code == 201, created.text
        preview = created.json()["preview"]
        assert preview["entry_count"] == 2 and preview["chapters"] and preview["is_empty"] is False

        confirmed = await client.post(
            f"/api/v1/memory/autobiography/previews/{preview['preview_id']}/confirm",
            json={"companion_id": companion_id}, headers=AUTH)
        assert confirmed.status_code == 200, confirmed.text
        record = confirmed.json()["record"]
        assert record["status"] == "committed" and record["created_memory_ids"]

        with db.engine.connect() as conn:
            stored = conn.execute(select(mm.memory_items).where(
                mm.memory_items.c.summary_level == "autobiography")).mappings().all()
        assert len(stored) == 1 and stored[0]["status"] == "active"

        undone = await client.post(
            f"/api/v1/memory/autobiography/records/{record['record_id']}/undo",
            json={"companion_id": companion_id}, headers=AUTH)
        assert undone.status_code == 200, undone.text
        assert undone.json()["record"]["status"] == "reverted"
        with db.engine.connect() as conn:
            originals = conn.execute(select(mm.memory_items.c.status).where(
                mm.memory_items.c.id.in_(ids))).scalars().all()
        assert originals and all(status == "active" for status in originals)


async def test_api_autobiography_disabled_by_default(db, actor, seeded_scope, tmp_path):
    app = create_app(Settings(access_token="synthetic-autobiography", data_dir=tmp_path), db, actor)
    companion_id = str(seeded_scope.companion_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/memory/autobiography/status",
                                    params={"companion_id": companion_id}, headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["enabled"] is False

        blocked = await client.post("/api/v1/memory/autobiography/previews",
                                    json={"companion_id": companion_id}, headers=AUTH)
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["error"]["code"] == "AUTOBIOGRAPHY_DISABLED"

        # PATCH settings turns the feature on through the generic memory-settings flow.
        patched = await client.patch("/api/v1/memory/settings",
                                     json={"autobiography_enabled": True}, headers=AUTH)
        assert patched.status_code == 200, patched.text
        assert patched.json()["autobiography_enabled"] is True

        enabled = await client.get("/api/v1/memory/autobiography/status",
                                   params={"companion_id": companion_id}, headers=AUTH)
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["enabled"] is True


async def test_api_empty_preview_cannot_be_confirmed(db, actor, seeded_scope, tmp_path):
    settings = Settings(access_token="synthetic-autobiography", data_dir=tmp_path,
                        memory_autobiography_enabled=True)
    app = create_app(settings, db, actor)
    companion_id = str(seeded_scope.companion_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/memory/autobiography/previews",
                                    json={"companion_id": companion_id}, headers=AUTH)
        assert created.status_code == 201, created.text
        preview = created.json()["preview"]
        assert preview["is_empty"] is True and preview["entry_count"] == 0
        confirmed = await client.post(
            f"/api/v1/memory/autobiography/previews/{preview['preview_id']}/confirm",
            json={"companion_id": companion_id}, headers=AUTH)
    assert confirmed.status_code == 422, confirmed.text
    assert confirmed.json()["error"]["code"] == "EMPTY_PREVIEW"
