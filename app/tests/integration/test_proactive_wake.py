"""Scheduled wakes use the owner's room and may remain silent. No real API/QQ."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select

from agi_wind.archive import models as m
from agi_wind.delivery.proactive import ProactiveService
from agi_wind.integrations.qq import QQSettings
from agi_wind.integrations.qq_groups import ensure_group_binding
from test_proactive import ProactiveProvider, active_settings, old_user_event, service_with_provider


def setup_wake(db, actor, scope, raw):
    settings = active_settings(min_idle_minutes=5, min_gap_minutes=30, quiet_start='00:00', quiet_end='00:00')
    settings.qq = QQSettings(enabled=True, owner_qq='10001', bot_qq='10002',
                             conversation_id=scope.conversation_id)
    provider = ProactiveProvider(raw)
    provider.text = raw
    proactive = service_with_provider(db, provider, settings)
    now = [datetime.now(ZoneInfo('Asia/Shanghai'))]
    proactive._now_fn = lambda: now[0]
    old_user_event(db, actor, scope, now[0] - timedelta(hours=3))
    return proactive, provider, now


async def test_silent_wake_has_no_message_and_cooldown_survives_restart(db, actor, seeded_scope):
    proactive, provider, now = setup_wake(db, actor, seeded_scope,
                                         json.dumps({'action':'silent','message':'do not leak'}))
    before = db.count('events')
    result = await proactive.send(actor, seeded_scope.companion_id)
    assert result == {'sent':False, 'reason':'silent'}
    assert db.count('events') == before and db.count('proactive_messages') == 0
    restarted = ProactiveService(db, proactive.settings, proactive.service, now_fn=lambda:now[0])
    assert restarted.due(actor, seeded_scope.companion_id) == (False, 'wake_gap')
    assert await restarted.tick() == 0 and len(provider.calls) == 1


async def test_schedule_ignores_newer_group_and_sends_owner_qq(db, actor, seeded_scope, tmp_path):
    import asyncio
    from agi_wind.integrations.qq import QQBridge
    proactive, _, _ = setup_wake(db, actor, seeded_scope,
                                 json.dumps({'action':'reach_out','message':'给哥哥的话'}))
    group = ensure_group_binding(db, actor, companion_id=seeded_scope.companion_id,
                                 bot_qq='10002', group_id='123456789')
    sent=[]
    proactive.settings.data_dir=tmp_path
    proactive.settings.qq.reply_delay_seconds=0
    bridge=QQBridge(None,proactive.settings,None,proactive.service,None,actor,asyncio.Event())
    class Connection:
        connected=True
        async def call(self,action,params):
            sent.append((action,params)); return {'message_id':1}
    bridge.connection=Connection()
    proactive.service.qq_bridge=bridge
    assert await proactive.tick() == 1
    event = db.all('events',role='assistant')[0]
    assert event['conversation_id'] == seeded_scope.conversation_id != group['conversation_id']
    assert event['content']['text'] == '给哥哥的话'
    assert sent[0][0]=='send_private_msg' and sent[0][1]['user_id']==10001
    assert event['content']['qq_delivery']['qq_synced'] is True


async def test_schedule_cannot_archive_into_group_even_if_caller_passes_one(db, actor, seeded_scope):
    proactive, _, _ = setup_wake(db, actor, seeded_scope, '{}')
    group = ensure_group_binding(db,actor,companion_id=seeded_scope.companion_id,
                                 bot_qq='10002',group_id='123456789')
    with db.engine.connect() as conn:
        conversation=conn.execute(select(m.conversations).where(m.conversations.c.id==group['conversation_id'])).mappings().one()
    result=await proactive.deliver(actor,conversation,'must not appear',trigger='schedule')
    assert result == {'sent':False,'reason':'owner_private_only'}
    assert db.count('proactive_messages')==0


async def test_invalid_wake_output_is_not_sent_or_retried_every_tick(db, actor, seeded_scope):
    proactive, provider, _ = setup_wake(db,actor,seeded_scope,'这不是有效的唤醒决定')
    result=await proactive.send(actor,seeded_scope.companion_id)
    assert result == {'sent':False,'reason':'invalid_output'}
    assert await proactive.tick()==0 and len(provider.calls)==1
    assert db.count('proactive_messages')==0


async def test_manual_wake_api_can_return_silent(db, actor, seeded_scope, tmp_path):
    import httpx
    from test_client import client_app
    provider=ProactiveProvider()
    provider.text='{"action":"silent"}'
    app=client_app(db,actor,tmp_path,provider)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                                 headers={'Authorization':'Bearer synthetic-private-token'}) as client:
        response=await client.post('/api/v1/proactive/send',json={'companion_id':str(seeded_scope.companion_id)})
    assert response.status_code==200 and response.json()=={'sent':False,'reason':'silent'}


async def test_failed_qq_mirror_is_saved_as_failed_not_reported_as_delivered(db, actor, seeded_scope):
    proactive, _, _ = setup_wake(db,actor,seeded_scope,'{"action":"reach_out","message":"hello"}')
    async def deliver(target,content,result,check):
        await check(); return {'sent':False,'reason':'qq_binding_changed'}
    proactive.service.qq_bridge=SimpleNamespace(proactive_available=lambda target:True,deliver_proactive=deliver)
    result=await proactive.send(actor,seeded_scope.companion_id)
    assert result['sent'] and result['qq_synced'] is False
    content=db.all('events',id=result['event_id'])[0]['content']
    assert content['qq_delivery']=={'qq_synced':False,'qq_reason':'qq_binding_changed'}


async def test_silent_wakes_have_a_daily_model_call_cap(db, actor, seeded_scope):
    proactive, provider, now=setup_wake(db,actor,seeded_scope,'{"action":"silent"}')
    proactive.settings.proactive.max_per_day=1
    assert (await proactive.send(actor,seeded_scope.companion_id))['reason']=='silent'
    now[0]+=timedelta(minutes=31)
    assert proactive.due(actor,seeded_scope.companion_id)==(False,'wake_cap')
    assert len(provider.calls)==1


def test_historical_misrouted_group_schedule_does_not_spend_private_quota(db, actor, seeded_scope):
    proactive,_,_=setup_wake(db,actor,seeded_scope,'')
    group=ensure_group_binding(db,actor,companion_id=seeded_scope.companion_id,
                              bot_qq='10002',group_id='123456789')
    proactive.archive.create_proactive_message(actor,group['conversation_id'],'legacy wrong route',trigger='schedule')
    assert proactive.stats(actor,seeded_scope.companion_id)['sent_today']==0
