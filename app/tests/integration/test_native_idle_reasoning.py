"""Native provider reasoning is the sole reasoning source, never JSON thought/steps."""
import json
from datetime import datetime, timedelta, timezone

from agi_wind.providers.protocol import ToolTurn, ProviderChunk
from test_idle_cognition import cognition, make_service, active_settings, run_turn
from test_proactive_wake import setup_wake


async def test_idle_logs_native_reasoning_and_ignores_legacy_simulated_steps(db, actor, seeded_scope, chat_request_factory):
    clock=[datetime(2026,10,27,4,0,tzinfo=timezone.utc)]
    provider=cognition()
    service,_,idle=make_service(db,provider,active_settings(),clock)
    await run_turn(db,actor,seeded_scope,chat_request_factory,service)
    async def turn(*args,**kwargs):
        return ToolTurn(json.dumps({'thought':'FAKE_THOUGHT','steps':['FAKE_STEP'],'action':'silent'}),'REAL_NATIVE_REASONING')
    provider.tool_turn=turn
    clock[0]+=timedelta(seconds=31)
    await idle.tick()
    logs=db.all('conversation_live_events',type='idle_cognition.step')
    assert [row['payload']['text'] for row in logs]==['REAL_NATIVE_REASONING']
    run=db.all('idle_cognition_runs')[0]
    assert run['steps_used']==1 and run['steps']==[{'round':1,'step':1,'kind':'model'}]
    assert 'FAKE_' not in json.dumps(run,default=str)
    assert all('REAL_NATIVE' not in json.dumps(row['content']) for row in db.all('events'))


async def test_scheduled_wake_native_reasoning_only_goes_to_logs(db, actor, seeded_scope):
    proactive,provider,_=setup_wake(db,actor,seeded_scope,'')
    async def stream(*args,**kwargs):
        yield ProviderChunk('reasoning','NATIVE_SCHEDULED_REASONING')
        yield ProviderChunk('text','{"action":"silent"}')
    provider.stream=stream
    assert (await proactive.send(actor,seeded_scope.companion_id))['reason']=='silent'
    assert [row['payload']['text'] for row in db.all('conversation_live_events',type='idle_cognition.step')]==['NATIVE_SCHEDULED_REASONING']
    assert not db.all('events',role='assistant')
    assert db.all('conversation_live_events',type='idle_cognition.completed')[-1]['payload']['decision']=='silent'
