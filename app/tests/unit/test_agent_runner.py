import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import Field, ValidationError

from agi_wind.agent.settings import AgentSettings
from agi_wind.agent.runner import AgentRunner, ToolSpec
from agi_wind.providers.protocol import ToolCall, ToolTurn
from agi_wind.core.types import DTO, ModelRef
from agi_wind.core.errors import AppError


class Query(DTO):
    query: str = Field(min_length=1, max_length=500)


MODEL = ModelRef(provider_id='fake', model_id='fake')


class Scripted:
    def __init__(self, turns): self.turns, self.requests = iter(turns), []
    async def tool_turn(self, model, messages, tools, tool_choice='auto'):
        self.requests.append({'messages': json.loads(json.dumps(messages)), 'tools': tools, 'choice': tool_choice})
        return next(self.turns)


def call(name='search_memory', arguments='{"query":"称呼"}', id=None):
    return ToolCall(id or str(uuid4()), name, arguments)


async def test_tools_feed_results_back_keep_reasoning_and_hide_planning_text():
    seen, trace, reasoning = [], [], []
    async def search(args): seen.append(args.query); return {'items': ['小风']}
    async def log(event): trace.append(event)
    async def think(text): reasoning.append(text)
    provider = Scripted([ToolTurn('先去查一下', '需要来源', (call(),)), ToolTurn('哥哥，小风是你希望的称呼', '有来源了')])
    runner = AgentRunner(AgentSettings(enabled=True), [ToolSpec('search_memory', '查记忆', Query, search)], on_event=log, on_reasoning=think)
    result = await runner.run(provider, MODEL, [{'role': 'user', 'content': '我叫什么'}])
    assert result.text == '哥哥，小风是你希望的称呼' and seen == ['称呼']
    second = provider.requests[1]['messages']
    assert second[-2]['reasoning_content'] == '需要来源'
    assert second[-1]['role'] == 'tool' and '小风' in second[-1]['content']
    assert len([e for e in trace if e['status'] == 'completed']) == 1
    assert reasoning == ['需要来源', '有来源了']


async def test_invalid_arguments_never_execute_and_model_can_recover():
    seen = []
    async def search(args): seen.append(args); return {}
    provider = Scripted([ToolTurn(tool_calls=(call(arguments='{"query":"x","account_id":"stolen"}'),)), ToolTurn('没有查询权限')])
    result = await AgentRunner(AgentSettings(enabled=True), [ToolSpec('search_memory', '', Query, search)]).run(provider, MODEL, [])
    assert seen == [] and result.text == '没有查询权限'
    assert 'TOOL_ARGUMENTS_INVALID' in provider.requests[-1]['messages'][-1]['content']


async def test_unknown_tool_never_executes():
    provider = Scripted([ToolTurn(tool_calls=(call('shell'),)), ToolTurn('不执行命令')])
    result = await AgentRunner(AgentSettings(enabled=True), []).run(provider, MODEL, [])
    assert result.text == '不执行命令'
    assert 'TOOL_NOT_ALLOWED' in provider.requests[-1]['messages'][-1]['content']


async def test_budget_uses_one_tools_disabled_final_turn_and_bounds_calls():
    seen = []
    async def search(args): seen.append(args.query); return {'ok': True}
    provider = Scripted([ToolTurn(tool_calls=(call(), call())), ToolTurn('已经查到的结果')])
    settings = AgentSettings(enabled=True, max_tool_calls=1, max_rounds=1)
    result = await AgentRunner(settings, [ToolSpec('search_memory', '', Query, search)]).run(provider, MODEL, [])
    assert seen == ['称呼'] and result.calls_used == 1
    assert provider.requests[-1]['choice'] == 'none'
    assert len([m for m in provider.requests[-1]['messages'] if m['role'] == 'tool']) == 2


async def test_timeout_is_a_bounded_tool_result_without_private_exception():
    async def slow(args): await asyncio.sleep(0.1); return {}
    provider = Scripted([ToolTurn(tool_calls=(call(),)), ToolTurn('查询超时了')])
    settings = AgentSettings(enabled=True, tool_timeout_seconds=0.01)
    result = await AgentRunner(settings, [ToolSpec('search_memory', '', Query, slow)]).run(provider, MODEL, [])
    assert result.text == '查询超时了'
    assert 'TOOL_TIMEOUT' in provider.requests[-1]['messages'][-1]['content']


async def test_repeated_call_id_cannot_repeat_side_effect():
    seen = []
    async def run(args): seen.append(1); return {'sent': True}
    repeated = call(id='same-id')
    provider = Scripted([ToolTurn(tool_calls=(repeated,)), ToolTurn(tool_calls=(repeated,)), ToolTurn('完成')])
    result = await AgentRunner(AgentSettings(enabled=True), [ToolSpec('search_memory', '', Query, run)]).run(provider, MODEL, [])
    assert len(seen) == 1 and result.text == '完成'


async def test_disabled_and_stale_checks_stop_before_tool_side_effect():
    seen = []
    async def search(args): seen.append(1); return {}
    count = 0
    async def check():
        nonlocal count
        count += 1
        if count >= 2: raise AppError('STALE_CONTEXT', '来源已失效', 409)
    provider = Scripted([ToolTurn(tool_calls=(call(),))])
    with pytest.raises(AppError, match='来源已失效'):
        await AgentRunner(AgentSettings(enabled=True), [ToolSpec('search_memory', '', Query, search)], check_current=check).run(provider, MODEL, [])
    assert seen == []
    with pytest.raises(AppError) as error:
        await AgentRunner(AgentSettings(), []).run(provider, MODEL, [])
    assert error.value.code == 'AGENT_DISABLED'


async def test_cancelled_tool_is_not_swallowed():
    async def cancelled(args): raise asyncio.CancelledError()
    provider = Scripted([ToolTurn(tool_calls=(call(),))])
    with pytest.raises(asyncio.CancelledError):
        await AgentRunner(AgentSettings(enabled=True), [ToolSpec('search_memory', '', Query, cancelled)]).run(provider, MODEL, [])


async def test_large_tool_result_is_valid_bounded_json():
    async def large(args): return {'text': '鱼' * 10000}
    provider = Scripted([ToolTurn(tool_calls=(call(),)), ToolTurn('收到')])
    await AgentRunner(AgentSettings(enabled=True, max_result_chars=1000), [ToolSpec('search_memory', '', Query, large)]).run(provider, MODEL, [])
    result = provider.requests[-1]['messages'][-1]['content']
    assert len(result) <= 1000 and json.loads(result)['truncated'] is True


async def test_round_and_step_metadata_is_exposed_on_reasoning_and_tools():
    steps, trace = [], []
    async def search(args): return {'items': []}
    async def log(event): trace.append(event)
    provider = Scripted([ToolTurn('先查一下', '需要来源', (call(),)), ToolTurn('查完了', '有结果了')])
    runner = AgentRunner(AgentSettings(enabled=True, max_rounds=3), [ToolSpec('search_memory', '', Query, search)],
                         on_event=log, on_step=lambda meta: steps.append(dict(meta)))
    result = await runner.run(provider, MODEL, [{'role': 'user', 'content': '你好'}])
    assert result.rounds_used == 2 and result.steps_used == 3
    reasoning_steps = [meta for meta in steps if meta['kind'] == 'reasoning']
    assert [(meta['round'], meta['step']) for meta in reasoning_steps] == [(1, 1), (2, 3)]
    running = [event for event in trace if event['status'] == 'running']
    assert running and running[0]['round'] == 1 and running[0]['step'] == 2
    completed = [event for event in trace if event['status'] != 'running']
    assert completed[0]['round'] == running[0]['round']


def test_settings_bounds_and_no_arbitrary_search_credential_url():
    assert AgentSettings().enabled is False
    with pytest.raises(ValidationError): AgentSettings(max_rounds=99)
    with pytest.raises(ValidationError): AgentSettings(search_url='https://secret@host.invalid/search')
    with pytest.raises(ValidationError): AgentSettings(search_url='file:///etc/passwd')
