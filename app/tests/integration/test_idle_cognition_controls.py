import json
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from test_idle_cognition import make_service, active_settings, cognition, run_turn


async def test_review_cancelled_schedules_should_not_consume_daily_thinking_cap(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition()
    service, _, idle = make_service(db, provider, active_settings(), clock)
    for _ in range(7):
        await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    assert provider.complete_calls == 1, 'Cancelled waiting records must not spend actual thinking quota'


async def test_review_cancel_during_round_should_prevent_next_paid_call(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition()
    service, _, idle = make_service(db, provider, active_settings(), clock)
    async def complete(*args):
        provider.complete_calls += 1
        idle.cancel_conversation(seeded_scope.conversation_id, 'user_message')
        return json.dumps({'thought': 'continue', 'steps': ['step'],
                           'action': 'continue' if provider.complete_calls == 1 else 'silent', 'message': ''})
    provider.complete = complete
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    assert provider.complete_calls == 1, 'Cancellation must be checked before the next provider call'


async def test_review_global_stop_should_cancel_pending_idle_run(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from agi_wind.api.app import create_app
    provider = cognition()
    settings = active_settings()
    from pydantic import SecretStr
    settings.access_token = SecretStr('synthetic-review-token')
    settings.data_dir = tmp_path
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    service, _, _ = make_service(db, provider, settings, clock)
    app = create_app(settings, db, actor, service)
    service.memory_extraction_enabled = False
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/v1/agent/stop', headers={'Authorization': 'Bearer synthetic-review-token'})
    assert response.status_code == 200
    assert db.all('idle_cognition_runs')[0]['state'] == 'cancelled', 'Stop must cancel after-reply pending work'


async def test_steps_count_actual_operations_instead_of_model_written_lists(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition(script=[
        json.dumps({'steps': ['a', 'b', 'excess'], 'action': 'continue'}),
        json.dumps({'steps': ['c', 'd', 'excess'], 'action': 'reach_out', 'message': '想起来一件事'})])
    service, _, idle = make_service(db, provider, active_settings(after_reply_max_steps=2), clock)
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    run = db.all('idle_cognition_runs')[0]
    assert provider.complete_calls == 2 and run['state'] == 'sent'
    assert run['steps_used'] == 2
    assert [(s['round'], s['step']) for s in run['steps']] == [(1, 1), (2, 1)]


async def test_cancel_interrupts_inflight_model_and_tick_keeps_working(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition()
    service, _, idle = make_service(db, provider, active_settings(), clock)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def complete(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    provider.complete = complete
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    tick = asyncio.create_task(idle.tick())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        idle.cancel_conversation(seeded_scope.conversation_id, 'stopped')
        await asyncio.wait_for(cancelled.wait(), 1)
        assert await asyncio.wait_for(tick, 1) == 1
        assert db.all('idle_cognition_runs')[0]['state'] == 'cancelled'
        assert db.count('proactive_messages') == 0
    finally:
        tick.cancel()
        await asyncio.gather(tick, return_exceptions=True)


async def test_started_failure_spends_daily_quota_but_pending_does_not(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition(script=['invalid json'])
    service, _, idle = make_service(db, provider, active_settings(after_reply_max_runs_per_day=1), clock)
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    assert provider.complete_calls == 1
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    assert provider.complete_calls == 1


async def test_stop_between_final_guard_and_delivery_does_not_send(db, actor, seeded_scope, chat_request_factory):
    clock = [datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)]
    provider = cognition(script=[json.dumps({'action': 'reach_out', 'message': '追话'})])
    service, proactive, idle = make_service(db, provider, active_settings(), clock)
    deliver = proactive.deliver
    async def stop_then_deliver(*args, **kwargs):
        idle.cancel_conversation(seeded_scope.conversation_id, 'stopped')
        return await deliver(*args, **kwargs)
    proactive.deliver = stop_then_deliver
    await run_turn(db, actor, seeded_scope, chat_request_factory, service)
    clock[0] += timedelta(seconds=31)
    await idle.tick()
    assert db.count('proactive_messages') == 0
