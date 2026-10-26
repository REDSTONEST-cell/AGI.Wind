from uuid import UUID, uuid4
from contextlib import nullcontext
from datetime import datetime, timezone
from sqlalchemy import select
from . import models as m
from .repository import authorized_conversation, digest, lock
from ..core.errors import AppError
from ..core.types import AcceptedRequest


class ArchiveService:
    def __init__(self, db): self.db = db

    async def accept(self, actor, request, model_ref=None, runtime="unconfigured"):
        with self.db.engine.begin() as conn:
            return self._accept(conn, actor, request, model_ref, runtime)

    def _accept(self, conn, actor, request, model_ref=None, runtime="unconfigured", reuse_event_id=None):
        conversation = authorized_conversation(conn, actor, request.conversation_id)
        lock(conn, conversation["companion_id"])
        lock(conn, request.request_id)
        state = conversation.get("state", "active")
        content = request.input.model_dump(mode="json")
        options = request.output.model_dump(mode="json")
        # Preserve pre-call request hashes for ordinary chat retries across upgrades.
        if not options.get('realtime'):
            options.pop('realtime', None)
        input_hash = digest({"conversation_id": request.conversation_id, "input": content, "output": options})
        existing = conn.execute(select(m.requests).where(m.requests.c.id == request.request_id)).mappings().first()
        if existing:
            if existing["account_id"] != actor.account_id:
                raise AppError("FORBIDDEN", "无权访问此请求", 403)
            if existing["input_hash"] != input_hash:
                raise AppError("REQUEST_CONFLICT", "同一请求 ID 对应不同内容", 409)
            generation = conn.execute(select(m.generations).where(m.generations.c.id == existing["generation_id"])).mappings().one()
            return self.accepted(request.request_id, generation["id"], generation["epoch"])
        if state != "active":
            raise AppError("CONVERSATION_NOT_ACTIVE", "这段对话已归档，只能查看；请恢复或开始新对话", 409)
        archived_event = None
        if reuse_event_id is not None:
            archived_event = conn.execute(select(m.events).where(
                m.events.c.id == reuse_event_id, m.events.c.conversation_id == request.conversation_id,
                m.events.c.account_id == actor.account_id, m.events.c.role == "user",
                m.events.c.visibility == "visible")).mappings().first()
            if not archived_event:
                raise AppError("EXTERNAL_EVENT_MISSING", "待回复的群消息不存在", 409)
            if archived_event["request_id"] is not None:
                raise AppError("EXTERNAL_EVENT_USED", "这条群消息已经生成过回答", 409)
        for asset_id in request.input.asset_ids:
            owner = conn.execute(select(m.asset_owners).where(m.asset_owners.c.asset_id == asset_id, m.asset_owners.c.account_id == actor.account_id)).first()
            if not owner: raise AppError("ASSET_UNAVAILABLE", "附件不存在或无权访问", 403)
        companion = conn.execute(select(m.companions).where(m.companions.c.id == conversation["companion_id"])).mappings().one()
        generation_id, event_id = uuid4(), reuse_event_id or uuid4()
        conn.execute(m.requests.insert().values(id=request.request_id, account_id=actor.account_id, conversation_id=request.conversation_id, input_hash=input_hash, input=content, output=options, device_id=request.device_id, generation_id=generation_id))
        from ..identity.models import runtime_bindings
        selected_model = conn.scalar(select(runtime_bindings.c.model_ref).where(runtime_bindings.c.companion_id == companion["id"], runtime_bindings.c.runtime == "model_selection"))
        conn.execute(m.generations.insert().values(id=generation_id, request_id=request.request_id, memory_revision=companion["memory_revision"], persona_version=companion["active_persona_version"], model_ref=selected_model or (model_ref.model_dump(mode="json") if model_ref else {}), runtime=runtime))
        if reuse_event_id is not None:
            # Reuse the already archived group message; never duplicate the same words.
            conn.execute(m.events.update().where(m.events.c.id == reuse_event_id).values(request_id=request.request_id, generation_id=generation_id))
        else:
            conn.execute(m.events.insert().values(id=event_id, account_id=actor.account_id, companion_id=companion["id"], conversation_id=request.conversation_id, request_id=request.request_id, generation_id=generation_id, role="user", kind="user_input", content=content, content_sha256=digest(content)))
            for asset_id in request.input.asset_ids:
                conn.execute(m.asset_refs.insert().values(asset_id=asset_id, event_id=event_id))
        conn.execute(m.outbox.insert().values(event_type="generation.requested", aggregate_id=generation_id, payload={"generation_id": str(generation_id)}, dedupe_key="generation:" + str(request.request_id)))
        from ..delivery.stream import emit, emit_live
        emit(conn, generation_id, "accepted", {"request_id":str(request.request_id)}, 1)
        emit_live(conn, request.conversation_id, "generation.accepted", {"request_id":str(request.request_id), "generation_id":str(generation_id), "epoch":1})
        return self.accepted(request.request_id, generation_id, 1)

    def record_external_message(self, actor, conversation_id, *, text, asset_ids=None, sender_principal_id=None,
                                external_message_id=None, reply_to_external_id=None, occurred_at=None):
        """Archive an externally delivered message without creating any generation task."""
        asset_ids = list(asset_ids or [])
        with self.db.engine.begin() as conn:
            conversation = authorized_conversation(conn, actor, conversation_id)
            lock(conn, conversation["companion_id"])
            if external_message_id is not None:
                existing = conn.execute(select(m.events).where(
                    m.events.c.conversation_id == conversation_id,
                    m.events.c.external_message_id == external_message_id)).mappings().first()
                if existing:
                    return dict(existing)
            for asset_id in asset_ids:
                owner = conn.execute(select(m.asset_owners).where(m.asset_owners.c.asset_id == asset_id, m.asset_owners.c.account_id == actor.account_id)).first()
                if not owner: raise AppError("ASSET_UNAVAILABLE", "附件不存在或无权访问", 403)
            content = {"text": text, "asset_ids": [str(asset_id) for asset_id in asset_ids]}
            if reply_to_external_id is not None:
                content["reply_to_external_id"] = str(reply_to_external_id)
            event_id = uuid4()
            row = conn.execute(m.events.insert().values(
                id=event_id, account_id=actor.account_id, companion_id=conversation["companion_id"],
                conversation_id=conversation_id, role="user", kind="user_input", content=content,
                content_sha256=digest(content), sender_principal_id=sender_principal_id,
                external_message_id=external_message_id,
                occurred_at=occurred_at or m.now()).returning(m.events)).mappings().one()
            for asset_id in asset_ids:
                conn.execute(m.asset_refs.insert().values(asset_id=asset_id, event_id=event_id))
            return dict(row)

    def submit_external_reply(self, actor, conversation_id, event_id, request_id, *, output=None, device_id="qq-group", model_ref=None, runtime="direct_vendor"):
        """Create the generation for an already archived message, reusing its event."""
        with self.db.engine.begin() as conn:
            event = conn.execute(select(m.events).where(
                m.events.c.id == event_id, m.events.c.conversation_id == conversation_id,
                m.events.c.account_id == actor.account_id)).mappings().one()
            from ..core.types import ChatInput, ChatRequest, OutputOptions
            request = ChatRequest(request_id=request_id, conversation_id=conversation_id,
                input=ChatInput(text=event["content"].get("text", ""), asset_ids=[UUID(key) for key in event["content"].get("asset_ids") or []]),
                output=OutputOptions.model_validate(output or {}), device_id=device_id)
            return self._accept(conn, actor, request, model_ref, runtime, reuse_event_id=event_id)

    @staticmethod
    def accepted(request_id, generation_id, epoch):
        return AcceptedRequest(request_id=request_id, generation_id=generation_id, epoch=epoch, events_url=f"/api/v1/generations/{generation_id}/events")

    async def append_output(self, generation_id, delta, final_state=None, connection=None):
        if final_state not in (None, "complete", "partial", "cancelled", "failed"):
            raise ValueError("invalid output state")
        with (self.db.engine.begin() if connection is None else nullcontext(connection)) as conn:
            lock(conn, generation_id)
            generation = conn.execute(select(m.generations).where(m.generations.c.id == generation_id).with_for_update()).mappings().one()
            if generation["state"] in ("completed", "cancelled", "failed", "interrupted"):
                raise AppError("GENERATION_TERMINAL", "回答已终止", 409)
            previous = conn.execute(select(m.events).where(m.events.c.generation_id == generation_id, m.events.c.role == "assistant")).mappings().first()
            content = {**(previous['content'] if previous else {}), "text": (previous["content"].get("text", "") if previous else "") + delta, "state": final_state or "partial"}
            if previous:
                result = conn.execute(m.events.update().where(m.events.c.id == previous["id"]).values(content=content, content_sha256=digest(content)).returning(m.events)).mappings().one()
            else:
                request = conn.execute(select(m.requests).where(m.requests.c.id == generation["request_id"])).mappings().one()
                conversation = conn.execute(select(m.conversations).where(m.conversations.c.id == request["conversation_id"])).mappings().one()
                result = conn.execute(m.events.insert().values(account_id=conversation["account_id"], companion_id=conversation["companion_id"], conversation_id=conversation["id"], request_id=request["id"], generation_id=generation_id, role="assistant", kind="assistant_output", content=content, content_sha256=digest(content)).returning(m.events)).mappings().one()
        return dict(result)

    def create_proactive_message(self, actor, conversation_id, text, model_ref=None, runtime="proactive", trigger="schedule"):
        """Archive an unsolicited assistant message without inventing a user turn."""
        text = str(text or "").strip()
        if not text:
            raise AppError("EMPTY_PROACTIVE_MESSAGE", "主动消息为空", 422)
        request_id, generation_id, event_id = uuid4(), uuid4(), uuid4()
        content = {"text": text, "state": "complete", "proactive": True, "trigger": trigger}
        with self.db.engine.begin() as conn:
            conversation = authorized_conversation(conn, actor, conversation_id)
            lock(conn, conversation["companion_id"])
            if conversation.get("state", "active") != "active":
                raise AppError("CONVERSATION_NOT_ACTIVE", "这段对话已归档，不能主动发消息", 409)
            companion = conn.execute(select(m.companions).where(m.companions.c.id == conversation["companion_id"])).mappings().one()
            conn.execute(
                m.requests.insert().values(
                    id=request_id,
                    account_id=actor.account_id,
                    conversation_id=conversation_id,
                    input_hash=digest({"proactive_event_id": str(event_id)}),
                    input={"text": ""},
                    output={},
                    device_id="proactive",
                    generation_id=generation_id,
                    state="accepted",
                )
            )
            conn.execute(
                m.generations.insert().values(
                    id=generation_id,
                    request_id=request_id,
                    memory_revision=companion["memory_revision"],
                    persona_version=companion["active_persona_version"],
                    model_ref=(model_ref.model_dump(mode="json") if model_ref else {}),
                    runtime=runtime,
                    state="completed",
                    finished_at=m.now(),
                )
            )
            conn.execute(
                m.events.insert().values(
                    id=event_id,
                    account_id=actor.account_id,
                    companion_id=companion["id"],
                    conversation_id=conversation_id,
                    request_id=request_id,
                    generation_id=generation_id,
                    role="assistant",
                    kind="assistant_output",
                    content=content,
                    content_sha256=digest(content),
                )
            )
            from ..delivery.stream import emit_live
            emit_live(
                conn,
                conversation_id,
                "proactive.message",
                {
                    "event_id": str(event_id),
                    "conversation_id": str(conversation_id),
                    "text": text,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "trigger": trigger,
                },
            )
        return {
            "event_id": event_id,
            "generation_id": generation_id,
            "conversation_id": conversation_id,
            "text": text,
        }
