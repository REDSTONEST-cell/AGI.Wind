import json
from io import BytesIO
import pytest
from PIL import Image
from sqlalchemy import select
from nagi.core.types import ModelRef
from nagi.core.errors import AppError


class VisualProvider:
    def __init__(self, db, plan=None, reflect_fail=False):
        self.db, self.calls, self.reflect_fail = db, [], reflect_fail
        self.plan = plan or {'save':{'name':'红猫','description':'可爱','tags':['猫']},'mimic':True,'send_sticker':None}

    async def stream(self, model, messages):
        from nagi.providers.protocol import ProviderChunk
        self.calls.append(messages)
        if len(self.calls)%2:
            assert self.db.count('stickers') == 0 or len(self.calls)>1
            yield ProviderChunk('reasoning', 'PRIVATE_REASONING')
            yield ProviderChunk('text', '一只红猫。')
        else:
            assert self.db.all('events', role='assistant')[-1]['content']['text'] == '一只红猫。'
            yield ProviderChunk('reasoning', 'REFLECT_REASONING')
            if self.reflect_fail: raise RuntimeError('private upstream')
            yield ProviderChunk('text', json.dumps(self.plan, ensure_ascii=False))


async def setup_turn(db, actor, scope, tmp_path, provider, **kwargs):
    from nagi.archive.assets import AssetStore
    from nagi.providers.vision import VisionAdapter
    from nagi.delivery.service import GenerationService
    store = AssetStore(db, tmp_path/'assets')
    stream=BytesIO(); Image.new('RGB',(3,3),'red').save(stream,'PNG')
    asset=await store.put(actor, BytesIO(stream.getvalue()), 'image/png')
    async def observe(model, messages): return '红色的猫'
    model=ModelRef(provider_id='fake', model_id='synthetic')
    vision=VisionAdapter(db,store,observe,model)
    service=GenerationService(db,provider,model=model,vision=vision,random_value=lambda:0,**kwargs)
    return service,asset


async def test_visual_turn_persists_real_order_and_excludes_reasoning_from_context(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from nagi.memory.context import ContextBuilder
    provider=VisualProvider(db)
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,provider)
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]; request.input.text=''
    accepted=await service.submit(actor,request)
    assert db.count('stickers') == 0
    assert await service.run(accepted.generation_id)
    events=service.replay(actor,accepted.generation_id)
    kinds=[e.type for e in events]
    assert kinds.index('text.completed') < next(i for i,e in enumerate(events) if e.type=='reasoning.delta' and e.payload['stage']=='reflect') < kinds.index('sticker.sent') < kinds.index('generation.completed')
    output=db.all('events',role='assistant')[0]['content']
    assert output['text']=='一只红猫。' and output['state']=='complete'
    assert output['attachments'][0]['asset_id']==str(asset['id'])
    assert [s['tool'] for s in output['trace'] if s['type']=='tool']==['observe_image','reply','reflect','save_sticker','send_sticker']
    assert all(s['status']=='completed' for s in output['trace'] if s['type']=='tool')
    assert db.count('stickers')==1 and db.count('assets')==1
    assert 'PRIVATE_REASONING' not in json.dumps(provider.calls[-1],ensure_ascii=False)
    context=await ContextBuilder(db).build(seeded_scope,request.input,service.model)
    assert 'PRIVATE_REASONING' not in json.dumps(context.recent_events)


@pytest.mark.parametrize('failure', ['exception','invalid_json','unknown_action'])
async def test_reflection_failure_keeps_reply_and_saves_without_mimic(db, actor, seeded_scope, chat_request_factory, tmp_path, failure):
    provider=VisualProvider(db, reflect_fail=failure=='exception')
    if failure=='invalid_json': provider.plan='not a plan'
    if failure=='unknown_action': provider.plan={'execute':'anything'}
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,provider)
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]
    accepted=await service.submit(actor,request)
    assert await service.run(accepted.generation_id)
    output=db.all('events',role='assistant')[0]['content']
    assert output['text']=='一只红猫。' and not output.get('attachments')
    assert db.count('stickers')==1
    assert any(s.get('status')=='failed' for s in output['trace'])


async def test_mimic_cooldown_three_image_turns(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider=VisualProvider(db)
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,provider)
    sent=[]
    for _ in range(4):
        request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]
        accepted=await service.submit(actor,request); assert await service.run(accepted.generation_id)
        sent.append(any(e.type=='sticker.sent' for e in service.replay(actor,accepted.generation_id)))
    assert sent==[True,False,False,True]


async def test_vision_failure_is_honest_and_does_not_call_reply(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider=VisualProvider(db)
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,provider)
    async def fail(*args): raise AppError('PROVIDER_UNAVAILABLE','不可用',503)
    service.vision.complete=fail
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]
    accepted=await service.submit(actor,request)
    assert not await service.run(accepted.generation_id)
    assert provider.calls==[] and db.count('stickers')==0
    assert db.all('generations')[0]['error_code']=='VISION_FAILED'
    assert db.all('events',role='assistant')[0]['content']['trace'][0]['status']=='failed'


async def test_failed_reply_marks_tool_failed_preserving_partial_trace(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from nagi.providers.protocol import ProviderChunk
    class FailingReply:
        async def stream(self,model,messages):
            yield ProviderChunk('text','半句')
            raise AppError('PROVIDER_UNAVAILABLE','厂商失败',503)
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,FailingReply())
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]
    accepted=await service.submit(actor,request)
    assert not await service.run(accepted.generation_id)
    output=db.all('events',role='assistant')[0]['content']
    assert output['text']=='半句' and output['state']=='partial'
    assert next(step for step in output['trace'] if step.get('tool')=='reply')['status']=='failed'


async def test_reflection_timeout_saves_and_never_mimics(db, actor, seeded_scope, chat_request_factory, tmp_path):
    import asyncio
    from nagi.providers.protocol import ProviderChunk
    class HangingReflect(VisualProvider):
        async def stream(self,model,messages):
            if not self.calls:
                self.calls.append(messages); yield ProviderChunk('text','一只红猫。')
            else:
                await asyncio.sleep(1); yield ProviderChunk('text','{}')
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,HangingReflect(db),reflection_timeout=0.01)
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]
    accepted=await service.submit(actor,request); assert await service.run(accepted.generation_id)
    output=db.all('events',role='assistant')[0]['content']
    assert output['text']=='一只红猫。' and output['state']=='complete'
    assert db.count('stickers')==1 and not output.get('attachments')


async def test_reasoning_limit_and_no_save_plan(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from nagi.providers.protocol import ProviderChunk
    class LongReasoning:
        def __init__(self): self.calls=0
        async def stream(self,model,messages):
            self.calls+=1
            if self.calls==1:
                yield ProviderChunk('reasoning','想'*64010)
                yield ProviderChunk('text','答')
            else: yield ProviderChunk('text','{"save":null,"mimic":false,"send_sticker":null}')
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,LongReasoning())
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]
    accepted=await service.submit(actor,request); assert await service.run(accepted.generation_id)
    content=db.all('events',role='assistant')[0]['content']
    assert len(content['reasoning'])==64000 and content['reasoning_truncated'] is True
    assert db.count('stickers')==0 and not content.get('attachments')


async def test_cross_service_interrupt_blocks_late_observation(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from uuid import uuid4
    from nagi.delivery.service import GenerationService
    provider=VisualProvider(db)
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,provider)
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]
    accepted=await service.submit(actor,request)
    other=GenerationService(db,None)
    async def cancel(model,messages):
        await other.interrupt(actor,accepted.generation_id,uuid4())
        return '不应归档的迟到描述'
    service.vision.complete=cancel
    assert not await service.run(accepted.generation_id)
    assert not db.all('events',kind='vision_observation')
    assert db.count('stickers')==0 and provider.calls==[]
    assert not any(s.get('status')=='running' for s in db.all('events',role='assistant')[0]['content']['trace'])


async def test_delete_image_turn_also_deletes_bound_observation_and_media(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from nagi.archive.deletions import DeletionService, DeletionLedger
    provider=VisualProvider(db,plan={'save':None,'mimic':False,'send_sticker':None})
    service,asset=await setup_turn(db,actor,seeded_scope,tmp_path,provider)
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id); request.input.asset_ids=[asset['id']]
    accepted=await service.submit(actor,request); assert await service.run(accepted.generation_id)
    user=db.all('events',role='user')[0]
    ledger=DeletionLedger(tmp_path/'governance');ledger.initialize()
    await DeletionService(db,ledger,tmp_path/'assets').delete_event(actor,user['id'])
    assert not db.all('events',kind='vision_observation')
    assert db.count('assets')==0 and not list((tmp_path/'assets').rglob('*.png'))
