from uuid import uuid4
import pytest
from nagi.core.types import ModelRef

async def test_interrupt_retry_same_epoch_and_completed_delivery_broadcast(db,actor,seeded_scope,chat_request_factory):
    from nagi.delivery.service import GenerationService
    from test_generation import FakeProvider
    service=GenerationService(db,FakeProvider(),model=ModelRef(provider_id="fake",model_id="test"))
    accepted=await service.submit(actor,chat_request_factory(conversation_id=seeded_scope.conversation_id))
    await service.run(accepted.generation_id)
    command=uuid4()
    first=await service.interrupt(actor,accepted.generation_id,command)
    again=await service.interrupt(actor,accepted.generation_id,command)
    assert first==again and first["epoch"]==2
    assert first["generation_state"]=="completed" and first["delivery_state"]=="cancelled"
    assert any(row["type"]=="delivery.epoch_changed" for row in db.all("conversation_live_events"))
    other=await service.interrupt(actor,accepted.generation_id,uuid4())
    assert other["epoch"]==2

async def test_memory_revision_invalidates_running_generation(db,actor,seeded_scope,chat_request_factory):
    from nagi.delivery.service import GenerationService
    from nagi.identity.service import IdentityService
    class MutatingProvider:
        async def stream(self,model,messages):
            yield "先说半句"
            await IdentityService(db).update(actor,seeded_scope.companion_id,1,{"style":{"tone":"新风格"}})
            yield "不应继续输出的旧上下文"
    service=GenerationService(db,MutatingProvider(),model=ModelRef(provider_id="fake",model_id="test"))
    accepted=await service.submit(actor,chat_request_factory(conversation_id=seeded_scope.conversation_id))
    await service.run(accepted.generation_id)
    text=db.all("events",role="assistant")[0]["content"]["text"]
    assert text=="先说半句"
    assert db.all("generations")[0]["state"] in ("cancelled","failed")
