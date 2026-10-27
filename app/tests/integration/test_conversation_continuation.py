"""Persistent mid-task speech and waits; fake vendor/OneBot, real PostgreSQL."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from agi_wind.archive import models as m
from agi_wind.delivery.idle_cognition import IdleCognitionService
from agi_wind.integrations.qq import QQBridge, QQSettings
from test_idle_cognition import make_service, active_settings, cognition, run_turn


def action(kind, message='', seconds=0):
    return json.dumps({'action':kind,'message':message,'wait_seconds':seconds})


async def setup_task(db,actor,scope,factory,tmp_path,script):
    clock=[datetime.now(timezone.utc)]
    provider=cognition(script=script)
    settings=active_settings(max_per_day=10,after_reply_media_enabled=False)
    settings.data_dir=tmp_path
    settings.qq=QQSettings(enabled=True,owner_qq='3548782171',bot_qq='334193096',
                            conversation_id=scope.conversation_id,reply_delay_seconds=0)
    service,proactive,idle=make_service(db,provider,settings,clock)
    proactive._now_fn=lambda:clock[0]
    bridge=QQBridge(None,settings,None,service,None,actor,asyncio.Event())
    sent=[]
    class Connection:
        connected=True
        async def call(self,name,params):
            sent.append((name,params)); return {'message_id':len(sent)}
    bridge.connection=Connection(); service.qq_bridge=bridge
    await run_turn(db,actor,scope,factory,service)
    clock[0]+=timedelta(seconds=31)
    return service,proactive,idle,provider,clock,sent


async def test_say_wait_restart_resume_same_task_and_qq_without_repeating(db,actor,seeded_scope,chat_request_factory,tmp_path):
    service,proactive,idle,provider,clock,sent=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,
        [action('say','先说一句'),action('wait',seconds=300),action('say','接着说一句'),action('finish')])
    await idle.tick()
    row=db.all('idle_cognition_runs')[0]; task_id=row['id']
    assert row['state']=='pending' and row['due_at']==clock[0]+timedelta(seconds=300)
    assert row['rounds_used']==2 and row['checkpoint']['sent_count']==1
    assert len(sent)==1 and sent[0][0]=='send_private_msg'
    assert '先说一句' not in json.dumps(row['checkpoint'],ensure_ascii=False)
    restarted=IdleCognitionService(db,proactive.settings,service,proactive=proactive,now_fn=lambda:clock[0])
    proactive.idle_cognition=restarted; service.idle_cognition=restarted
    clock[0]+=timedelta(seconds=299)
    assert await restarted.tick()==0 and provider.complete_calls==2
    clock[0]+=timedelta(seconds=2); await restarted.tick()
    row=db.all('idle_cognition_runs')[0]
    assert row['id']==task_id and row['state']=='sent' and row['rounds_used']==4
    assert row['checkpoint']['sent_count']==2 and len(sent)==2
    assert db.count('proactive_messages')==2 and len(db.all('events',role='user'))==1


@pytest.mark.parametrize('interrupt',['message','stop','memory','qq_binding'])
async def test_waiting_task_yields_to_user_stop_or_changed_context(db,actor,seeded_scope,chat_request_factory,tmp_path,interrupt):
    service,proactive,idle,provider,clock,sent=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,
        [action('say','先说一句',300),action('say','不该再发')])
    await idle.tick()
    assert db.all('idle_cognition_runs')[0]['state']=='pending'
    if interrupt=='message':
        await service.submit(actor,chat_request_factory(text='换个话题',conversation_id=seeded_scope.conversation_id))
    elif interrupt=='stop':
        assert idle.cancel_actor(actor)==1
    elif interrupt=='memory':
        with db.engine.begin() as conn:
            conn.execute(m.companions.update().where(m.companions.c.id==seeded_scope.companion_id).values(
                memory_revision=m.companions.c.memory_revision+1))
    else:
        proactive.settings.qq.owner_qq='123456789'
    clock[0]+=timedelta(seconds=301); await idle.tick()
    assert len(sent)==1 and provider.complete_calls==1
    assert db.all('idle_cognition_runs')[0]['state'] in ('cancelled','skipped')


async def test_tools_can_run_after_midtask_speech_and_stay_in_budget(db,actor,seeded_scope,chat_request_factory,tmp_path):
    from agi_wind.agent.settings import AgentSettings
    from agi_wind.providers.protocol import ToolTurn,ToolCall
    service,_,idle,provider,_,sent=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,[])
    service.agent_settings=lambda:AgentSettings(enabled=True)
    turns=[ToolTurn(action('say','我查一下')),ToolTurn(tool_calls=(ToolCall('one','search_memory','{"query":"偏好"}'),)),
           ToolTurn(action('say','查完了')),ToolTurn(action('finish'))]
    calls=[]
    async def turn(model,messages,tools,tool_choice='auto'):
        calls.append(tool_choice)
        if len(calls)==2:
            assert len(sent)==1, 'mid-task speech was held until the entire task finished'
        return turns.pop(0)
    provider.tool_turn=turn
    await idle.tick()
    row=db.all('idle_cognition_runs')[0]
    assert row['state']=='sent' and len(sent)==2
    assert row['checkpoint']['tool_calls_used']==1 and row['rounds_used']==4
    assert any(r['payload']['name']=='search_memory' and r['payload']['status']=='completed'
               for r in db.all('conversation_live_events',type='idle_cognition.tool'))


async def test_round_and_message_budgets_survive_waits(db,actor,seeded_scope,chat_request_factory,tmp_path):
    _,proactive,idle,provider,clock,sent=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,
        [action('say','第一句',30),action('say','第二句',30),action('say','不该第三句')])
    proactive.settings.proactive.continuation_max_messages=2
    await idle.tick(); clock[0]+=timedelta(seconds=31); await idle.tick()
    row=db.all('idle_cognition_runs')[0]
    assert row['state']=='sent' and row['checkpoint']['sent_count']==2
    clock[0]+=timedelta(seconds=1000); await idle.tick()
    assert provider.complete_calls==2 and len(sent)==2


async def test_scheduled_wake_uses_the_same_resumable_executor(db,actor,seeded_scope,tmp_path):
    from test_proactive_wake import setup_wake
    proactive,provider,clock=setup_wake(db,actor,seeded_scope,action('say','醒来先说一句',300))
    proactive.settings.data_dir=tmp_path
    first=await proactive.send(actor,seeded_scope.companion_id,force=True,trigger='manual')
    assert first['sent']
    row=db.all('idle_cognition_runs')[0]
    assert row['state']=='pending' and row['checkpoint']['origin']=='schedule'
    provider.text=action('finish')
    clock[0]+=timedelta(seconds=301)
    await proactive.idle_cognition.tick()
    after=db.all('idle_cognition_runs')[0]
    assert after['id']==row['id'] and after['state']=='sent'
    assert after['rounds_used']==2 and db.count('proactive_messages')==1


async def test_scheduled_wait_resumes_when_after_reply_feature_is_off(db,actor,seeded_scope,tmp_path):
    from test_proactive_wake import setup_wake
    proactive,provider,clock=setup_wake(db,actor,seeded_scope,action('wait',seconds=30))
    proactive.settings.proactive.after_reply_enabled=False
    assert (await proactive.send(actor,seeded_scope.companion_id,force=True,trigger='manual'))['reason']=='waiting'
    provider.text=action('say','稍后再来',0)
    proactive.settings.proactive.continuation_max_messages=1
    clock[0]+=timedelta(seconds=31)
    await proactive.idle_cognition.tick()
    assert db.all('idle_cognition_runs')[0]['state']=='sent' and db.count('proactive_messages')==1


async def test_expired_task_cannot_continue_after_a_wait(db,actor,seeded_scope,chat_request_factory,tmp_path):
    _,_,idle,provider,clock,sent=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,
        [action('say','说完先等',300),action('say','过期消息')])
    await idle.tick(); clock[0]+=timedelta(hours=2)
    await idle.tick()
    assert len(sent)==1 and provider.complete_calls==1
    assert db.all('idle_cognition_runs')[0]['reason']=='task_expired'


async def test_model_round_limit_is_cumulative_and_wait_is_clamped(db,actor,seeded_scope,chat_request_factory,tmp_path):
    _,proactive,idle,provider,clock,sent=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,
        [action('say','第一句',3600),action('say','最后一句',300),action('say','超额消息')])
    proactive.settings.proactive.continuation_max_rounds=2
    await idle.tick()
    assert db.all('idle_cognition_runs')[0]['due_at']==clock[0]+timedelta(seconds=900)
    clock[0]+=timedelta(seconds=901); await idle.tick()
    assert db.all('idle_cognition_runs')[0]['state']=='sent'
    assert db.all('idle_cognition_runs')[0]['rounds_used']==2 and len(sent)==2
    clock[0]+=timedelta(seconds=901); await idle.tick()
    assert provider.complete_calls==2


async def test_crashed_active_segment_does_not_replay_side_effects(db,actor,seeded_scope,chat_request_factory,tmp_path):
    from agi_wind.delivery.models import idle_cognition_runs
    _,_,idle,provider,clock,sent=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,
        [action('say','已发消息',300),action('say','不能重放')])
    await idle.tick()
    with db.engine.begin() as conn:
        conn.execute(idle_cognition_runs.update().values(state='running',lease_until=clock[0]-timedelta(seconds=1)))
    assert await idle.tick()==0
    row=db.all('idle_cognition_runs')[0]
    assert row['state']=='failed' and row['error_code']=='CONTINUATION_INTERRUPTED'
    assert len(sent)==1 and provider.complete_calls==1


async def test_continuation_migration_upgrades_existing_rows(db,actor,seeded_scope,chat_request_factory,tmp_path):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    _,_,_,_,_,_=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,[])
    cfg=Config(str(Path.cwd()/'alembic.ini'))
    with db.engine.begin() as conn:
        cfg.attributes['connection']=conn
        command.downgrade(cfg,'0016_idle_cognition_started')
        command.upgrade(cfg,'head')
    row=db.all('idle_cognition_runs')[0]
    assert row['state']=='pending' and row['checkpoint']=={}


@pytest.mark.parametrize('patch',[{'continuation_enabled':False},{'enabled':False}])
async def test_settings_switch_immediately_cancels_a_saved_wait(db,actor,seeded_scope,chat_request_factory,tmp_path,patch):
    import httpx
    from pydantic import SecretStr
    from agi_wind.api.app import create_app
    service,proactive,idle,_,_,_=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,
        [action('say','说完等待',300)])
    await idle.tick()
    proactive.settings.access_token=SecretStr('synthetic-switch-token')
    app=create_app(proactive.settings,db,actor,service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                                 headers={'Authorization':'Bearer synthetic-switch-token'}) as client:
        response=await client.patch('/api/v1/proactive/settings',json=patch)
        assert response.status_code==200
        assert response.json()['continuation_max_rounds']==6
        invalid=await client.patch('/api/v1/proactive/settings',json={'continuation_max_messages':0})
        assert invalid.status_code==422
    assert db.all('idle_cognition_runs')[0]['state']=='cancelled'


async def test_reducing_round_budget_during_a_model_call_stops_before_wait_or_next_call(db,actor,seeded_scope,chat_request_factory,tmp_path):
    _,proactive,idle,provider,_,sent=await setup_task(db,actor,seeded_scope,chat_request_factory,tmp_path,
        [action('say','最后一句',300),action('say','不该再调用')])
    original=provider.complete
    async def complete(*args):
        raw=await original(*args)
        proactive.settings.proactive=proactive.settings.proactive.model_copy(update={'continuation_max_rounds':1})
        return raw
    provider.complete=complete
    await idle.tick()
    row=db.all('idle_cognition_runs')[0]
    assert row['state']=='sent' and row['rounds_used']==1
    assert len(sent)==1 and provider.complete_calls==1


async def test_manual_start_does_not_exempt_future_continuations_from_quiet_hours(db,actor,seeded_scope):
    from zoneinfo import ZoneInfo
    from test_proactive_wake import setup_wake
    proactive,provider,clock=setup_wake(db,actor,seeded_scope,action('say','先说一句',120))
    clock[0]=datetime(2026,10,27,14,59,tzinfo=ZoneInfo('Asia/Shanghai'))
    proactive.settings.proactive.quiet_start='15:00'; proactive.settings.proactive.quiet_end='16:00'
    assert (await proactive.send(actor,seeded_scope.companion_id,force=True,trigger='manual'))['sent']
    provider.text=action('say','安静时段不应发出')
    clock[0]+=timedelta(seconds=121); await proactive.idle_cognition.tick()
    assert db.count('proactive_messages')==1 and len(provider.calls)==1
    assert db.all('idle_cognition_runs')[0]['state']=='skipped'
