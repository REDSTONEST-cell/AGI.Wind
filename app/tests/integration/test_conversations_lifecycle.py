from uuid import uuid4

import httpx

from test_client import client_app

BEARER = {"Authorization": "Bearer synthetic-private-token"}


def chat_body(conversation_id):
    return {
        "request_id": str(uuid4()),
        "conversation_id": conversation_id,
        "input": {"text": "归档后的会话不应继续回答"},
        "device_id": "lifecycle-test",
    }


async def test_single_active_conversation_archive_restore_hide_and_read_only_history(db, actor, seeded_scope, tmp_path):
    app = client_app(db, actor, tmp_path)
    companion = str(seeded_scope.companion_id)
    first = str(seeded_scope.conversation_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        active = (await client.get("/api/v1/conversations", params={"companion_id": companion, "state": "active"}, headers=BEARER)).json()
        assert [row["id"] for row in active] == [first]

        created = await client.post("/api/v1/conversations", headers=BEARER, json={"companion_id": companion, "title": "第二段对话"})
        assert created.status_code == 201, created.text
        second = created.json()
        assert second["state"] == "active" and second["id"] == second["conversation_id"]

        active = (await client.get("/api/v1/conversations", params={"companion_id": companion, "state": "active"}, headers=BEARER)).json()
        assert [row["id"] for row in active] == [second["id"]]
        archived = (await client.get("/api/v1/conversations", params={"companion_id": companion, "state": "archived"}, headers=BEARER)).json()
        assert [row["id"] for row in archived] == [first]

        # The archived conversation still exposes its history but accepts no new turn.
        assert (await client.get(f"/api/v1/conversations/{first}/events", headers=BEARER)).status_code == 200
        rejected = await client.post("/api/v1/chat/requests", headers=BEARER, json=chat_body(first))
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "CONVERSATION_NOT_ACTIVE"

        # Restoring an old conversation archives the current one, preserving one active row.
        restored = await client.patch(f"/api/v1/conversations/{first}", headers=BEARER, json={"state": "active"})
        assert restored.status_code == 200 and restored.json()["state"] == "active"
        active = (await client.get("/api/v1/conversations", params={"companion_id": companion, "state": "active"}, headers=BEARER)).json()
        assert [row["id"] for row in active] == [first]
        archived = (await client.get("/api/v1/conversations", params={"companion_id": companion, "state": "archived"}, headers=BEARER)).json()
        assert [row["id"] for row in archived] == [second["id"]]

        # Hiding only removes the item from the archive list; records stay recoverable.
        hidden = await client.patch(f"/api/v1/conversations/{second['id']}", headers=BEARER, json={"state": "hidden"})
        assert hidden.status_code == 200 and hidden.json()["state"] == "hidden"
        archived = (await client.get("/api/v1/conversations", params={"companion_id": companion, "state": "archived"}, headers=BEARER)).json()
        assert second["id"] not in [row["id"] for row in archived]
        hidden_rows = (await client.get("/api/v1/conversations", params={"companion_id": companion, "state": "hidden"}, headers=BEARER)).json()
        assert [row["id"] for row in hidden_rows] == [second["id"]]
        assert (await client.get(f"/api/v1/conversations/{second['id']}/events", headers=BEARER)).status_code == 200

        unhidden = await client.patch(f"/api/v1/conversations/{second['id']}", headers=BEARER, json={"state": "archived"})
        assert unhidden.status_code == 200 and unhidden.json()["state"] == "archived"
        archived = (await client.get("/api/v1/conversations", params={"companion_id": companion, "state": "archived"}, headers=BEARER)).json()
        assert [row["id"] for row in archived] == [second["id"]]

        renamed = await client.patch(f"/api/v1/conversations/{first}", headers=BEARER, json={"title": "改过的标题"})
        assert renamed.status_code == 200 and renamed.json()["title"] == "改过的标题"


async def test_archived_conversation_accepts_replayed_request_idempotently(db, actor, seeded_scope, chat_request_factory, tmp_path):
    app = client_app(db, actor, tmp_path)
    companion = str(seeded_scope.companion_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = str(seeded_scope.conversation_id)
        request = chat_body(first)
        # Accept once while active (the service has no provider, but acceptance is durable).
        accepted = await client.post("/api/v1/chat/requests", headers=BEARER, json=request)
        assert accepted.status_code == 202, accepted.text
        await client.post("/api/v1/conversations", headers=BEARER, json={"companion_id": companion, "title": "下一段"})
        # A retry of the same request ID is still idempotent and must not create a new turn.
        replay = await client.post("/api/v1/chat/requests", headers=BEARER, json=request)
        assert replay.status_code == 202
        assert replay.json()["generation_id"] == accepted.json()["generation_id"]
        # A fresh request for the archived conversation is rejected.
        rejected = await client.post("/api/v1/chat/requests", headers=BEARER, json=chat_body(first))
        assert rejected.status_code == 409
