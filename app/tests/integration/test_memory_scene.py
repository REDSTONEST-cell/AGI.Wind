from uuid import uuid4

from sqlalchemy import select

from agi_wind.archive import models as m
from agi_wind.archive.service import ArchiveService
from agi_wind.core.types import ChatInput, ModelRef, SceneScope, SourceRef
from agi_wind.integrations.qq_groups import ensure_group_binding, ensure_principal
from agi_wind.memory import models as mm
from agi_wind.memory.extraction import MemoryCandidate, MemoryWriter
from agi_wind.memory.retrieval import MemoryService
from agi_wind.memory.jobs import _load_batch

BOT = "334193096"
OWNER = "3548782171"
GROUP_A = "123456789"
GROUP_B = "987654321"


def insert_memory(db, actor, companion_id, content, *, pool="owner", room=None, visibility="owner_private",
                  principal_id=None):
    memory_id = uuid4()
    with db.engine.begin() as conn:
        conn.execute(mm.memory_items.insert().values(
            id=memory_id, account_id=actor.account_id, companion_id=companion_id, user_id=actor.user_id,
            kind="preference", content=content, epistemic_type="user_asserted", status="active",
            pool_kind=pool, subject_principal_id=principal_id,
            visibility=visibility, visibility_room_id=room, origin_room_id=room))
    return memory_id


def room(app, actor, companion_id, group_id):
    return ensure_group_binding(app, actor, companion_id=companion_id, bot_qq=BOT, group_id=group_id)


def scopes(actor, companion_id, conversation_id, room_id=None):
    owner = SceneScope(account_id=actor.account_id, user_id=actor.user_id,
                       companion_id=companion_id, conversation_id=conversation_id)
    group = SceneScope(account_id=actor.account_id, user_id=actor.user_id,
                       companion_id=companion_id, conversation_id=conversation_id,
                       scene="group", room_id=room_id)
    return owner, group


async def test_group_scene_retrieves_only_this_rooms_public_facts(db, actor, seeded_scope):
    binding_a = room(db, actor, seeded_scope.companion_id, GROUP_A)
    binding_b = room(db, actor, seeded_scope.companion_id, GROUP_B)
    member_a = ensure_principal(db, actor, external_user_id="736357841", nickname="甲")
    member_b = ensure_principal(db, actor, external_user_id="736357842", nickname="乙")
    private = insert_memory(db, actor, seeded_scope.companion_id, "主人私密咖啡", pool="owner")
    owner_a = insert_memory(db, actor, seeded_scope.companion_id, "主人群A咖啡", pool="owner",
                            room=binding_a["id"], visibility="group_public")
    owner_b = insert_memory(db, actor, seeded_scope.companion_id, "主人群B咖啡", pool="owner",
                            room=binding_b["id"], visibility="group_public")
    member_memory = insert_memory(db, actor, seeded_scope.companion_id, "甲喜欢咖啡", pool="member",
                                  room=binding_a["id"], visibility="group_public",
                                  principal_id=member_a["id"])
    insert_memory(db, actor, seeded_scope.companion_id, "乙喜欢咖啡", pool="member",
                  room=binding_b["id"], visibility="group_public", principal_id=member_b["id"])
    private_scope, group_a = scopes(actor, seeded_scope.companion_id, binding_a["conversation_id"], binding_a["id"])
    owner_hits = {hit.memory_id for hit in await MemoryService(db).search(private_scope, "咖啡")}
    # Owner private windows recall the owner's own facts, never member pools or raw group chat.
    assert private in owner_hits and owner_a in owner_hits and owner_b in owner_hits
    assert member_memory not in owner_hits
    group_hits = {hit.memory_id for hit in await MemoryService(db).search(group_a, "咖啡")}
    assert member_memory in group_hits and owner_a in group_hits
    assert private not in group_hits and owner_b not in group_hits


async def test_owner_extraction_ignores_member_voice_and_marks_group_fact_public(db, actor, seeded_scope):
    binding = room(db, actor, seeded_scope.companion_id, GROUP_A)
    conversation_id = binding["conversation_id"]
    owner_principal = ensure_principal(db, actor, external_user_id=OWNER, nickname="主人", is_owner=True)
    member_principal = ensure_principal(db, actor, external_user_id="736357841", nickname="甲")
    service = ArchiveService(db)
    owner_event = service.record_external_message(actor, conversation_id, text="我喜欢咖啡",
        sender_principal_id=owner_principal["id"], external_message_id="501")
    member_event = service.record_external_message(actor, conversation_id, text="我喜欢茶",
        sender_principal_id=member_principal["id"], external_message_id="502")
    _, group_scope = scopes(actor, seeded_scope.companion_id, conversation_id, binding["id"])
    with db.engine.connect() as conn:
        companion = conn.execute(select(m.companions).where(
            m.companions.c.id == seeded_scope.companion_id)).mappings().one()
        rows = _load_batch(conn, group_scope, companion, 10)
    # A group member's message is not owner-pool evidence even inside the same room.
    assert [row["id"] for row in rows] == [owner_event["id"]]
    writer = MemoryWriter(db)
    revision = companion["memory_revision"]
    stored = await writer.validate_and_store(group_scope, MemoryCandidate(
        kind="preference", content="我喜欢咖啡", epistemic_type="user_asserted",
        source_refs=[SourceRef(event_id=owner_event["id"], json_pointer="/text", start_offset=0, end_offset=5)]),
        revision)
    assert stored["status"] == "active"
    memory = db.all("memory_items")[0]
    assert memory["pool_kind"] == "owner" and memory["visibility"] == "group_public"
    assert memory["visibility_room_id"] == binding["id"] and memory["origin_room_id"] == binding["id"]
    quarantined = await writer.validate_and_store(group_scope, MemoryCandidate(
        kind="preference", content="我喜欢茶", epistemic_type="user_asserted",
        source_refs=[SourceRef(event_id=member_event["id"], json_pointer="/text", start_offset=0, end_offset=4)]),
        revision)
    assert quarantined["status"] == "quarantine"
    assert db.count("quarantine") == 1


async def test_group_context_has_speaker_labels_and_no_owner_private_relationship(db, actor, seeded_scope):
    from agi_wind.identity import models as im
    from agi_wind.memory.context import ContextBuilder
    binding = room(db, actor, seeded_scope.companion_id, GROUP_A)
    conversation_id = binding["conversation_id"]
    member = ensure_principal(db, actor, external_user_id="736357841", nickname="小明", card="群名片")
    service = ArchiveService(db)
    service.record_external_message(actor, conversation_id, text="今天聊点什么",
        sender_principal_id=member["id"], external_message_id="601")
    insert_memory(db, actor, seeded_scope.companion_id, "主人私密咖啡", pool="owner")
    insert_memory(db, actor, seeded_scope.companion_id, "群A公开咖啡", pool="owner",
                  room=binding["id"], visibility="group_public")
    with db.engine.begin() as conn:
        conn.execute(im.commitments.insert().values(companion_id=seeded_scope.companion_id,
            user_id=actor.user_id, content="主人私密承诺", status="pending"))
    _, group_scope = scopes(actor, seeded_scope.companion_id, conversation_id, binding["id"])
    owner_scope, _ = scopes(actor, seeded_scope.companion_id, seeded_scope.conversation_id)
    from agi_wind.identity.service import IdentityService
    assert len((await IdentityService(db).snapshot_scene(owner_scope)).commitments) == 1
    assert (await IdentityService(db).snapshot_scene(group_scope)).commitments == []
    context = await ContextBuilder(db).build(group_scope, ChatInput(text="咖啡"),
        ModelRef(provider_id="fake", model_id="test"))
    contents = {hit.content for hit in context.memories}
    assert "群A公开咖啡" in contents and "主人私密咖啡" not in contents
    assert context.persona_snapshot.commitments == []
    assert any(event.get("speaker") == "群名片" for event in context.recent_events)
