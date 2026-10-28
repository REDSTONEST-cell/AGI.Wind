import pytest
from uuid import uuid4
from sqlalchemy import select
from nagi.agent.settings import AgentSettings
from nagi.providers.protocol import ToolCall, ToolTurn
from nagi.delivery.service import GenerationService
from nagi.core.types import ModelRef
from nagi.archive import models as m


class ToolProvider:
    def __init__(self): self.messages = []
    async def tool_turn(self, model, messages, tools, tool_choice='auto'):
        self.messages.append(list(messages))
        if len(self.messages) == 1:
            assert 'search_memory' in [item['function']['name'] for item in tools]
            return ToolTurn('我先去查一下', '查询已有经历', (ToolCall('memory-1','search_memory','{"query":"香菜"}'),))
        return ToolTurn('哥哥，你说过不吃香菜。', '依据原始事件回答')
    async def stream(self, *args):
        raise AssertionError('legacy stream must not be used when enabled')
        yield ''


async def test_agent_calls_real_scoped_memory_and_emits_trace_only_to_logs(db,actor,seeded_scope,chat_request_factory):
    from nagi.archive.service import ArchiveService
    archive = ArchiveService(db)
    await archive.accept(actor, chat_request_factory('我不吃香菜',conversation_id=seeded_scope.conversation_id))
    # Remove the synthetic pending generation so the next test turn can run normally.
    with db.engine.begin() as conn: conn.execute(m.generations.update().values(state='completed'))
    provider = ToolProvider()
    service = GenerationService(db,provider,model=ModelRef(provider_id='fake',model_id='m'))
    policy = AgentSettings(enabled=True)
    service.agent_settings = lambda: policy
    service.memory_extraction_enabled = False
    accepted = await service.submit(actor,chat_request_factory('记得我的饮食偏好吗',conversation_id=seeded_scope.conversation_id))
    assert await service.run(accepted.generation_id)
    output = next(row for row in db.all('events',role='assistant') if row['generation_id']==accepted.generation_id)
    assert output['content']['text'] == '哥哥，你说过不吃香菜。'
    assert '我先去查一下' not in output['content']['text']
    assert '查询已有经历' in output['content']['reasoning']
    tool_result = provider.messages[1][-1]
    assert tool_result['role']=='tool' and '香菜' in tool_result['content']
    events = service.replay(actor,accepted.generation_id)
    assert any(e.type=='tool.started' and e.payload['tool']=='search_memory' for e in events)
    assert any(e.type=='tool.completed' and e.payload['status']=='completed' for e in events)
    assert events[-1].type=='generation.completed'


async def test_no_tool_provider_fails_explicitly_without_running_any_legacy_fallback(db,actor,seeded_scope,chat_request_factory):
    class Plain:
        async def stream(self,*args):
            raise AssertionError('must not silently fallback')
            yield ''
    service = GenerationService(db,Plain(),model=ModelRef(provider_id='fake',model_id='m'))
    service.agent_settings=lambda: AgentSettings(enabled=True)
    accepted=await service.submit(actor,chat_request_factory(conversation_id=seeded_scope.conversation_id))
    assert not await service.run(accepted.generation_id)
    assert db.all('generations')[0]['error_code']=='AGENT_MODEL_UNSUPPORTED'


async def test_agent_sends_one_selected_sticker_transactionally_and_no_legacy_second_send(db,actor,seeded_scope,chat_request_factory,tmp_path):
    import json
    from io import BytesIO
    from PIL import Image
    from nagi.archive.assets import AssetStore
    from nagi.archive.stickers import StickerService
    data=BytesIO(); Image.new('RGB',(4,4),'blue').save(data,'PNG')
    asset=await AssetStore(db,tmp_path/'assets').put(actor,BytesIO(data.getvalue()),'image/png')
    sticker=StickerService(db).save_from_asset(actor,seeded_scope.companion_id,asset['id'],name='开心',tags=['开心'])
    class Sender:
        def __init__(self): self.n=0
        async def tool_turn(self,model,messages,tools,tool_choice='auto'):
            self.n+=1
            if self.n==1: return ToolTurn(tool_calls=(ToolCall('find','search_stickers','{"query":"开心"}'),))
            if self.n==2:
                assert str(sticker['id']) in messages[-1]['content']
                args=json.dumps({'sticker_id':str(sticker['id'])})
                return ToolTurn(tool_calls=(ToolCall('send1','send_sticker',args),ToolCall('send2','send_sticker',args)))
            assert 'STICKER_SEND_LIMIT' in messages[-1]['content']
            return ToolTurn('给哥哥一张开心的')
    service=GenerationService(db,Sender(),model=ModelRef(provider_id='fake',model_id='m'),random_value=lambda:0)
    service.agent_settings=lambda:AgentSettings(enabled=True)
    accepted=await service.submit(actor,chat_request_factory('发个表情包',conversation_id=seeded_scope.conversation_id))
    assert await service.run(accepted.generation_id)
    output=db.all('events',role='assistant')[0]['content']
    assert len(output['attachments'])==1 and output['attachments'][0]['trigger']=='agent'
    assert db.all('stickers')[0]['use_count']==1
    assert len([e for e in service.replay(actor,accepted.generation_id) if e.type=='sticker.sent'])==1


async def test_agent_tool_cannot_send_unselected_sticker_or_search_other_scope(db,actor,seeded_scope,tmp_path):
    from nagi.agent.tools import StationTools,StickerSend,MemoryQuery
    from nagi.agent.search import WebSearch
    from nagi.core.types import Actor
    from nagi.archive.service import ArchiveService
    from nagi.core.types import ChatRequest
    from nagi.core.errors import AppError
    other_actor=Actor(account_id=uuid4(),user_id=uuid4())
    other=db.seed(other_actor)
    await ArchiveService(db).accept(other_actor,ChatRequest(request_id=uuid4(),conversation_id=other.conversation_id,device_id='test',input={'text':'另一个角色的独有私密暗号河豚'}))
    policy=AgentSettings(enabled=True)
    async def send(id): raise AssertionError('unselected sticker must not send')
    station=StationTools(db,seeded_scope,lambda:policy,WebSearch(policy,None),send)
    with pytest.raises(AppError) as error: await station.send_selected(StickerSend(sticker_id=uuid4()))
    assert error.value.code=='STICKER_NOT_SELECTED'
    assert not (await station.memory(MemoryQuery(query='独有私密暗号河豚')))['items']


async def test_memory_tool_trace_does_not_keep_an_untracked_copy_after_source_deletion(db,actor,seeded_scope,archive,chat_request_factory,tmp_path):
    import json
    from nagi.archive.deletions import DeletionLedger,DeletionService
    secret='删除测试独有内容河豚'
    await archive.accept(actor,chat_request_factory(secret,conversation_id=seeded_scope.conversation_id))
    source=db.all('events',role='user')[0]
    with db.engine.begin() as conn: conn.execute(m.generations.update().values(state='completed'))
    class Lookup:
        def __init__(self): self.n=0
        async def tool_turn(self,model,messages,tools,tool_choice='auto'):
            self.n+=1
            if self.n==1:return ToolTurn(tool_calls=(ToolCall('lookup','search_memory','{"query":"河豚"}'),))
            assert secret in messages[-1]['content']
            return ToolTurn('收到')
    service=GenerationService(db,Lookup(),model=ModelRef(provider_id='fake',model_id='m'))
    service.agent_settings=lambda:AgentSettings(enabled=True)
    service.memory_extraction_enabled=False
    accepted=await service.submit(actor,chat_request_factory('回忆一下',conversation_id=seeded_scope.conversation_id))
    assert await service.run(accepted.generation_id)
    ledger=DeletionLedger(tmp_path/'governance');ledger.initialize()
    await DeletionService(db,ledger,tmp_path/'assets').delete_event(actor,source['id'])
    output=next(row for row in db.all('events',role='assistant') if row['generation_id']==accepted.generation_id)
    assert secret not in json.dumps(output['content'],ensure_ascii=False)
    assert secret not in ''.join(e.model_dump_json() for e in service.replay(actor,accepted.generation_id))
