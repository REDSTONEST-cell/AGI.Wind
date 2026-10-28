from uuid import UUID, uuid4

import httpx
from sqlalchemy import select
from test_client import client_app

from agi_wind.archive import models as m
from agi_wind.archive.service import ArchiveService
from agi_wind.core.settings import Settings
from agi_wind.core.types import SceneScope
from agi_wind.integrations.qq_groups import ensure_group_binding, ensure_principal
from agi_wind.jobs.worker import Worker
from agi_wind.memory import models as mm
from agi_wind.memory.extraction import ExtractedCandidate, ExtractedSourceRef
from agi_wind.memory.jobs import (MEMBER_EXTRACT, _load_batch, _load_member_batch,
                                  enqueue_member_extraction, run_member_extraction)
from agi_wind.memory.members import MemberMemoryWriter
from agi_wind.memory.retrieval import MemoryService

BOT = "10002"
OWNER = "10001"
GROUP = "123456789"


def setup_room(db, actor, seeded_scope):
    binding = ensure_group_binding(db, actor, companion_id=seeded_scope.companion_id,
                                   bot_qq=BOT, group_id=GROUP, title="测试群")
    owner = ensure_principal(db, actor, external_user_id=OWNER, nickname="主人", is_owner=True)
    member_a = ensure_principal(db, actor, external_user_id="700000001", nickname="甲", card="甲名片")
    member_b = ensure_principal(db, actor, external_user_id="700000002", nickname="乙")
    return binding, owner, member_a, member_b


def group_scope(actor, seeded_scope, binding):
    return SceneScope(account_id=actor.account_id, user_id=actor.user_id,
                      companion_id=seeded_scope.companion_id,
                      conversation_id=binding["conversation_id"], scene="group", room_id=binding["id"])


def candidate(event_id, text, predicate="提到", value=""):
    return ExtractedCandidate(kind="preference", subject_key="user:self", predicate=predicate,
                              value=value or text[:20], content=text,
                              epistemic_type="user_asserted",
                              source_refs=[ExtractedSourceRef(event_id=event_id, quote=text)])


async def test_member_writer_isolates_sender_room_and_owner_facts(db, actor, seeded_scope):
    binding, owner, member_a, member_b = setup_room(db, actor, seeded_scope)
    service = ArchiveService(db)
    conversation_id = binding["conversation_id"]
    event_a = service.record_external_message(actor, conversation_id, text="我喜欢咖啡",
        sender_principal_id=member_a["id"], external_message_id="101")
    event_b = service.record_external_message(actor, conversation_id, text="我讨厌咖啡",
        sender_principal_id=member_b["id"], external_message_id="102")
    service.record_external_message(actor, conversation_id, text="主人说喜欢茶",
        sender_principal_id=owner["id"], external_message_id="103")
    anonymous = service.record_external_message(actor, conversation_id, text="匿名路过",
        sender_principal_id=None, external_message_id="104")
    with db.engine.connect() as conn:
        binding_row = dict(conn.execute(select(m.channel_bindings).where(
            m.channel_bindings.c.id == binding["id"])).mappings().one())
        batch = _load_member_batch(conn, binding_row, 10)
    # Only named, non-owner senders enter the member-pool batch.
    assert {row["id"] for row in batch} == {event_a["id"], event_b["id"]}
    assert anonymous["id"] not in {row["id"] for row in batch}
    from sqlalchemy import select as sa_select
    with db.engine.connect() as conn:
        revision = conn.scalar(sa_select(m.companions.c.memory_revision).where(
            m.companions.c.id == seeded_scope.companion_id))
    scope = group_scope(actor, seeded_scope, binding)
    writer = MemberMemoryWriter(db)
    with db.engine.begin() as conn:
        summary = writer.commit_candidates(conn, scope, binding_row,
            [candidate(event_a["id"], "我喜欢咖啡", "喜欢", "咖啡"),
             candidate(event_b["id"], "我讨厌咖啡", "喜欢", "讨厌咖啡")],
            [{k: row[k] for k in ("id", "content_sha256", "role", "kind", "recorded_at", "content", "principal_id")}
             for row in batch], revision)
    assert summary["stored"] == 2
    rows = {row["content"]: row for row in db.all("memory_items")}
    assert rows["我喜欢咖啡"]["pool_kind"] == "member"
    assert rows["我喜欢咖啡"]["subject_principal_id"] == member_a["id"]
    assert rows["我喜欢咖啡"]["visibility"] == "group_public"
    assert rows["我喜欢咖啡"]["visibility_room_id"] == binding["id"]
    assert rows["我喜欢咖啡"]["origin_room_id"] == binding["id"]
    assert rows["我讨厌咖啡"]["subject_principal_id"] == member_b["id"]
    # Retrieval: the group scene sees both members, the owner's private scene sees neither.
    group_hits = await MemoryService(db).search(scope, "咖啡")
    assert {hit.content for hit in group_hits} == {"我喜欢咖啡", "我讨厌咖啡"}
    owner_scope = SceneScope(account_id=actor.account_id, user_id=actor.user_id,
                             companion_id=seeded_scope.companion_id,
                             conversation_id=seeded_scope.conversation_id)
    owner_hits = await MemoryService(db).search(owner_scope, "咖啡")
    assert not {hit.content for hit in owner_hits} & {"我喜欢咖啡", "我讨厌咖啡"}
    # A candidate quoting two different members cannot be attributed to one person.
    with db.engine.begin() as conn:
        revision = conn.scalar(sa_select(m.companions.c.memory_revision).where(
            m.companions.c.id == seeded_scope.companion_id))
        ambiguous = ExtractedCandidate(kind="preference", subject_key="user:self", predicate="喜欢",
            value="咖啡", content="我们都喜欢咖啡", epistemic_type="user_asserted",
            source_refs=[ExtractedSourceRef(event_id=event_a["id"], quote="我喜欢咖啡"),
                         ExtractedSourceRef(event_id=event_b["id"], quote="我讨厌咖啡")])
        result = writer.commit_candidates(conn, scope, binding_row, [ambiguous], batch, revision)
    assert result["quarantined"] == 1
    assert db.all("quarantine")[0]["reason"] == "AMBIGUOUS_MEMBER_SUBJECT"
    # Same member changing a value never silently supersedes the old fact.
    later = service.record_external_message(actor, conversation_id, text="我现在喜欢茶",
        sender_principal_id=member_a["id"], external_message_id="105")
    with db.engine.connect() as conn:
        batch = _load_member_batch(conn, binding_row, 10)
        revision = conn.scalar(sa_select(m.companions.c.memory_revision).where(
            m.companions.c.id == seeded_scope.companion_id))
    with db.engine.begin() as conn:
        summary = writer.commit_candidates(conn, scope, binding_row,
            [candidate(later["id"], "我现在喜欢茶", "喜欢", "茶")], batch, revision)
    assert summary["stored"] == 1
    changed = next(row for row in db.all("memory_items") if row["content"] == "我现在喜欢茶")
    assert changed["status"] == "disputed"
    assert changed["subject_principal_id"] == member_a["id"]


class FakeMemberExtractor:
    def __init__(self):
        self.calls = 0

    def reservation_tokens(self, events):
        return (2000, 300)

    async def extract(self, scope, events):
        self.calls += 1
        candidates = []
        for event in events:
            text = event["text"].strip()
            if not text:
                continue
            candidates.append(candidate(UUID(event["event_id"]), text, "提到", text[:20]))
        return candidates[:8], {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5}


async def test_member_extraction_job_writes_pool_and_advances_room_cursor(db, actor, seeded_scope, tmp_path):
    binding, owner, member_a, member_b = setup_room(db, actor, seeded_scope)
    service = ArchiveService(db)
    conversation_id = binding["conversation_id"]
    service.record_external_message(actor, conversation_id, text="我喜欢咖啡",
        sender_principal_id=member_a["id"], external_message_id="201")
    service.record_external_message(actor, conversation_id, text="我讨厌咖啡",
        sender_principal_id=member_b["id"], external_message_id="202")
    owner_event = service.record_external_message(actor, conversation_id, text="主人自己的话",
        sender_principal_id=owner["id"], external_message_id="203")
    scope = group_scope(actor, seeded_scope, binding)
    with db.engine.begin() as conn:
        enqueue_member_extraction(conn, scope, generation_id=None, observed=True, delay_seconds=0)
    settings = Settings(data_dir=tmp_path)
    worker = Worker(db)
    jobs = worker.claim("member-test", event_types=[MEMBER_EXTRACT])
    assert len(jobs) == 1 and jobs[0]["event_type"] == MEMBER_EXTRACT
    extractor = FakeMemberExtractor()
    result = await worker.run_job(jobs[0]["id"], "member-test",
        lambda job: run_member_extraction(db, extractor, worker, job, settings=settings))
    assert result["state"] == "completed", result
    assert extractor.calls == 1
    rows = db.all("memory_items")
    assert {row["content"] for row in rows} == {"我喜欢咖啡", "我讨厌咖啡"}
    assert all(row["pool_kind"] == "member" and row["visibility_room_id"] == binding["id"] for row in rows)
    binding_after = db.all("channel_bindings", id=binding["id"])[0]
    assert binding_after["member_extract_cursor_id"] is not None
    job = db.all("outbox", event_type=MEMBER_EXTRACT)[0]
    assert job["state"] == "completed" and job["payload"]["result"]["stored"] == 2
    # Owner-pool batching never sees these member messages.
    owner_scope = SceneScope(account_id=actor.account_id, user_id=actor.user_id,
                             companion_id=seeded_scope.companion_id,
                             conversation_id=seeded_scope.conversation_id)
    with db.engine.connect() as conn:
        companion = conn.execute(select(m.companions).where(
            m.companions.c.id == seeded_scope.companion_id)).mappings().one()
        assert [row["id"] for row in _load_batch(conn, owner_scope, companion, 10)] == [owner_event["id"]]


async def test_memory_api_lists_member_pool_and_group_rooms(db, actor, seeded_scope, tmp_path):
    binding, owner, member_a, member_b = setup_room(db, actor, seeded_scope)
    service = ArchiveService(db)
    service.record_external_message(actor, binding["conversation_id"], text="我喜欢咖啡",
        sender_principal_id=member_a["id"], external_message_id="301")
    member_id = uuid4()
    owner_id = uuid4()
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.insert().values(
            id=member_id, account_id=actor.account_id, companion_id=seeded_scope.companion_id,
            user_id=actor.user_id, kind="preference", content="甲喜欢咖啡", epistemic_type="user_asserted",
            status="active", pool_kind="member", subject_principal_id=member_a["id"],
            origin_room_id=binding["id"], visibility="group_public", visibility_room_id=binding["id"]))
        conn.execute(mm.memory_items.insert().values(
            id=owner_id, account_id=actor.account_id, companion_id=seeded_scope.companion_id,
            user_id=actor.user_id, kind="preference", content="主人私密", epistemic_type="user_asserted",
            status="active", pool_kind="owner", visibility="owner_private"))
    app = client_app(db, actor, tmp_path)
    headers = {"Authorization": "Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        member_rows = (await client.get("/api/v1/memory/items", params={
            "companion_id": str(seeded_scope.companion_id), "pool": "member"}, headers=headers)).json()
        assert [row["content"] for row in member_rows] == ["甲喜欢咖啡"]
        assert member_rows[0]["room_title"] == "测试群"
        assert member_rows[0]["subject_name"] == "甲名片"
        owner_rows = (await client.get("/api/v1/memory/items", params={
            "companion_id": str(seeded_scope.companion_id), "pool": "owner"}, headers=headers)).json()
        assert [row["content"] for row in owner_rows] == ["主人私密"]
        rooms = (await client.get("/api/v1/integrations/qq/groups", params={
            "companion_id": str(seeded_scope.companion_id)}, headers=headers)).json()
        assert len(rooms) == 1 and rooms[0]["group_id"] == GROUP and rooms[0]["title"] == "测试群"
