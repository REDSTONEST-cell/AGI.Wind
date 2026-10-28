"""Cross-module regressions. Synthetic data/providers only; no paid API or QQ send."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from agi_wind.archive import models as m
from agi_wind.integrations.qq_groups import ensure_group_binding
from test_client import client_app
from test_idle_cognition import make_service, active_settings, cognition, run_turn


async def test_realtime_http_rejects_unbound_call_instead_of_crashing(db, actor, seeded_scope, tmp_path, chat_request_factory):
    app=client_app(db,actor,tmp_path)
    body=chat_request_factory(conversation_id=seeded_scope.conversation_id,output={'voice':'off','realtime':True})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test',
        headers={'Authorization':'Bearer synthetic-private-token'}) as client:
        response=await client.post('/api/v1/chat/requests',json=body.model_dump(mode='json'))
    assert response.status_code == 409
    assert db.count('requests') == 0


def test_background_memory_visits_private_and_each_enabled_group(db, actor, seeded_scope):
    from agi_wind.memory.runtime import _scopes
    one=ensure_group_binding(db,actor,companion_id=seeded_scope.companion_id,bot_qq='10002',group_id='123456789')
    two=ensure_group_binding(db,actor,companion_id=seeded_scope.companion_id,bot_qq='10002',group_id='223456789')
    scopes=_scopes(db,actor)
    assert {scope.room_id for scope in scopes if getattr(scope,'scene',None)=='group'} == {one['id'],two['id']}
    assert len([scope for scope in scopes if getattr(scope,'scene','owner_private')=='owner_private']) == 1


async def test_idle_does_not_deliver_after_memory_revision_changes(db, actor, seeded_scope, chat_request_factory):
    clock=[datetime(2026,10,27,4,0,tzinfo=timezone.utc)]
    provider=cognition()
    async def changed(model,messages):
        with db.engine.begin() as conn:
            conn.execute(m.companions.update().where(m.companions.c.id==seeded_scope.companion_id).values(
                memory_revision=m.companions.c.memory_revision+1))
        return json.dumps({'action':'reach_out','message':'synthetic stale message'})
    provider.complete=changed
    service,_,idle=make_service(db,provider,active_settings(),clock)
    await run_turn(db,actor,seeded_scope,chat_request_factory,service)
    clock[0]+=timedelta(seconds=31)
    await idle.tick()
    assert db.count('proactive_messages') == 0


async def test_global_stop_cancels_long_idle_proactive_request(db, actor, seeded_scope, tmp_path):
    entered=asyncio.Event()
    class Provider:
        async def stream(self,*args):
            entered.set()
            await asyncio.Future()
            yield 'never'
    app=client_app(db,actor,tmp_path,Provider())
    proactive=app.state.proactive
    task=asyncio.create_task(proactive.send(actor,seeded_scope.companion_id,force=True))
    try:
        await asyncio.wait_for(entered.wait(),2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
            headers={'Authorization':'Bearer synthetic-private-token'}) as client:
            response=await client.post('/api/v1/agent/stop')
            assert response.status_code == 200
        await asyncio.sleep(0)
        assert task.done(), 'Stop left a proactive model request running'
        assert db.count('proactive_messages') == 0
    finally:
        task.cancel(); await asyncio.gather(task,return_exceptions=True)


def test_owner_summary_sources_exclude_group_members_and_anonymous(db, actor, seeded_scope):
    from agi_wind.memory.consolidation import _rows
    from agi_wind.integrations.qq_groups import ensure_principal
    from agi_wind.archive.service import ArchiveService
    binding=ensure_group_binding(db,actor,companion_id=seeded_scope.companion_id,bot_qq='10002',group_id='123456789')
    principal=ensure_principal(db,actor,external_user_id='999999999',nickname='synthetic',is_owner=False)
    archive=ArchiveService(db)
    for sender in (principal['id'],None):
        archive.record_external_message(actor,binding['conversation_id'],text='synthetic private fact',sender_principal_id=sender)
    with db.engine.connect() as conn:
        rows=_rows(conn,seeded_scope,m.now()-timedelta(hours=1),m.now()+timedelta(hours=1))
    assert rows == []


async def test_observed_owner_group_message_schedules_owner_not_member_extraction(db, actor, seeded_scope, tmp_path):
    from types import SimpleNamespace
    from agi_wind.integrations.qq import QQSettings
    from agi_wind.integrations.qq_groups import group_message
    from test_qq_groups import group_event, BOT, OWNER, GROUP
    app=client_app(db,actor,tmp_path)
    app.state.settings.qq=QQSettings(enabled=True,owner_qq=OWNER,bot_qq=BOT,
        conversation_id=seeded_scope.conversation_id,group_enabled=True,allowed_groups=[GROUP])
    bridge=app.state.qq_bridge
    bridge.connection=SimpleNamespace(connected=True)
    await bridge.respond_group(bridge.connection,group_message(group_event(123,mention=False,sender_qq=OWNER),app.state.settings.qq))
    assert len(db.all('outbox',event_type='memory.extract')) == 1
    assert not db.all('outbox',event_type='memory.extract.member')


@pytest.mark.parametrize('source_device',['qq-private:10002:10001','web'])
async def test_regular_proactive_uses_the_same_qq_target_as_after_reply(db, actor, seeded_scope, chat_request_factory, source_device):
    from types import SimpleNamespace
    from agi_wind.integrations.qq import QQSettings
    clock=[datetime(2026,10,27,4,0,tzinfo=timezone.utc)]
    provider=cognition(reply='[情绪:平静]\nsynthetic message')
    settings=active_settings()
    settings.qq=QQSettings(enabled=True,owner_qq='10001',bot_qq='10002',conversation_id=seeded_scope.conversation_id)
    service,proactive,_=make_service(db,provider,settings,clock)
    sent=[]
    async def deliver(target,content,result,check):
        await check(); sent.append(target); return {'sent':True}
    service.qq_bridge=SimpleNamespace(proactive_available=lambda target:True,deliver_proactive=deliver)
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id)
    request.device_id=source_device
    accepted=await service.submit(actor,request); await service.run(accepted.generation_id)
    provider.reply=json.dumps({'action':'reach_out','message':'synthetic message'})
    result=await proactive.send(actor,seeded_scope.companion_id,force=True,trigger='manual')
    assert result['sent'] and sent[0]['kind']=='private'
    assert db.all('requests',generation_id=result['generation_id'])[0]['device_id']=='proactive'


async def test_offline_qq_does_not_block_web_proactive_message(db, actor, seeded_scope):
    from types import SimpleNamespace
    from agi_wind.integrations.qq import QQSettings
    settings=active_settings()
    settings.qq=QQSettings(enabled=True,owner_qq='10001',bot_qq='10002',conversation_id=seeded_scope.conversation_id)
    service,proactive,_=make_service(db,cognition(),settings,[m.now()])
    service.qq_bridge=SimpleNamespace(proactive_available=lambda target:False)
    with db.engine.connect() as conn:
        conversation=conn.execute(select(m.conversations).where(m.conversations.c.id==seeded_scope.conversation_id)).mappings().one()
    result=await proactive.deliver(actor,conversation,'synthetic mirror',trigger='manual')
    assert result['sent'] and result['qq_synced'] is False
    assert db.count('proactive_messages')==1


async def test_stop_cancels_only_proactive_send_and_keeps_tick_alive(db, actor, seeded_scope, tmp_path):
    entered=asyncio.Event()
    class Provider:
        async def stream(self,*args):
            entered.set(); await asyncio.Future(); yield 'never'
    app=client_app(db,actor,tmp_path,Provider())
    proactive=app.state.proactive
    app.state.settings.proactive=active_settings().proactive
    proactive.due=lambda *args:(True,'ok')
    task=asyncio.create_task(proactive.tick())
    try:
        await asyncio.wait_for(entered.wait(),2)
        assert proactive.cancel_actor(actor)==1
        assert await asyncio.wait_for(task,2)==0
        assert not proactive._inflight
    finally:
        task.cancel(); await asyncio.gather(task,return_exceptions=True)


async def test_idle_media_obeys_live_sticker_send_permission(db, actor, seeded_scope, monkeypatch):
    from types import SimpleNamespace
    from uuid import uuid4
    from agi_wind.agent.settings import AgentSettings
    from agi_wind.archive.stickers import StickerService
    provider=cognition()
    service,_,idle=make_service(db,provider,active_settings(),[m.now()])
    service.agent_settings=lambda:AgentSettings(enabled=True,sticker_send_enabled=False)
    sticker_id=uuid4()
    monkeypatch.setattr(StickerService,'list',lambda *args,**kwargs:[{'id':sticker_id,'attachment':{'kind':'sticker','asset_id':str(uuid4())}}])
    media=await idle._media(actor,seeded_scope,'test',SimpleNamespace(sticker_id=sticker_id),
        SimpleNamespace(tools=SimpleNamespace(selected_stickers={sticker_id})))
    assert not media


@pytest.mark.parametrize('source_device',['qq-private:10002:10001','web'])
async def test_chat_to_idle_tools_to_qq_sticker_archive_and_logs(db, actor, seeded_scope, chat_request_factory, tmp_path, source_device):
    """One real service path across modules; only vendor and OneBot I/O are fake."""
    from types import SimpleNamespace
    from io import BytesIO
    from PIL import Image
    from agi_wind.archive.assets import AssetStore
    from agi_wind.archive.stickers import StickerService
    from agi_wind.agent.settings import AgentSettings
    from agi_wind.integrations.qq import QQBridge,QQSettings
    from agi_wind.providers.protocol import ToolCall,ToolTurn
    assets=AssetStore(db,tmp_path/'assets')
    data=BytesIO(); Image.new('RGB',(3,3),'blue').save(data,format='PNG'); data.seek(0)
    asset=await assets.put(actor,data,'image/png')
    sticker=StickerService(db).save_from_asset(actor,seeded_scope.companion_id,asset['id'],name='合成咖啡')
    provider=cognition()
    provider.config=SimpleNamespace(supported_parameters=('max_tokens',))
    calls=[]
    async def tool_turn(model,messages,definitions,tool_choice='auto'):
        calls.append(tool_choice)
        if len(calls)==1:
            return ToolTurn(text='[情绪:平静]\n我记下了')
        if len(calls)==2:
            return ToolTurn(tool_calls=(ToolCall('memory','search_memory','{"query":"合成咖啡"}'),
                ToolCall('stickers','search_stickers','{"query":"合成咖啡"}')))
        results={item['tool_call_id']:item['content'] for item in messages if item['role']=='tool'}
        assert '合成咖啡' in results['memory']
        assert str(sticker['id']) in results['stickers']
        assert tool_choice=='none'
        return ToolTurn(text=json.dumps({'action':'reach_out','thought':'synthetic private thought',
            'message':'给你看这个','sticker_id':str(sticker['id'])}))
    provider.tool_turn=tool_turn
    clock=[datetime(2026,10,27,4,0,tzinfo=timezone.utc)]
    settings=active_settings(); settings.data_dir=tmp_path
    settings.qq=QQSettings(enabled=True,owner_qq='10001',bot_qq='10002',
        conversation_id=seeded_scope.conversation_id,reply_delay_seconds=0)
    service,_,idle=make_service(db,provider,settings,clock)
    service.agent_settings=lambda:AgentSettings(enabled=True)
    bridge=QQBridge(None,settings,None,service,assets,actor,asyncio.Event())
    delivered=[]
    class Connection:
        connected=True
        async def call(self,action,params):
            delivered.append((action,params)); return {'message_id':len(delivered)}
    bridge.connection=Connection(); service.qq_bridge=bridge
    request=chat_request_factory(text='我喜欢合成咖啡',conversation_id=seeded_scope.conversation_id)
    request.device_id=source_device
    accepted=await service.submit(actor,request); assert await service.run(accepted.generation_id)
    clock[0]+=timedelta(seconds=31); await idle.tick()
    run=db.all('idle_cognition_runs')[0]
    assert run['state']=='sent'
    assert len(calls)==3 and provider.complete_calls==0
    assert [params['message'][0]['type'] for _,params in delivered]==['text','image']
    assert all(action=='send_private_msg' and params['user_id']==10001 for action,params in delivered)
    event=db.all('events',id=run['message_event_id'])[0]
    assert event['content']['attachments'][0]['asset_id']==str(asset['id'])
    assert 'synthetic private thought' not in json.dumps(event['content'])
    traces=db.all('conversation_live_events',type='idle_cognition.tool')
    assert {(row['payload']['name'],row['payload']['status']) for row in traces}=={
        ('search_memory','started'),('search_memory','completed'),('search_stickers','started'),('search_stickers','completed')}
    assert len(db.all('asset_refs',event_id=event['id']))==1
