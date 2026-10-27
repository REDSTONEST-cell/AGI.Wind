from types import SimpleNamespace
from uuid import uuid4

from agi_wind.agent.settings import AgentSettings
from agi_wind.providers.protocol import ToolTurn, ToolCall


async def test_idle_tools_are_readonly_bounded_and_never_register_local_tools(monkeypatch):
    from agi_wind.delivery.idle_tools import IdleToolSession
    from agi_wind.agent.runner import ToolSpec
    from agi_wind.agent.tools import MemoryQuery
    calls, traces = [], []
    async def execute(args): calls.append(args.query); return {'items': []}
    monkeypatch.setattr('agi_wind.delivery.idle_tools.StationTools.specs', lambda self: [
        ToolSpec('search_memory', 'memory', MemoryQuery, execute),
        ToolSpec('run_command', 'forbidden', MemoryQuery, execute)])
    service = SimpleNamespace(db=None, agent_settings=lambda: AgentSettings(enabled=True), agent_secret_store=None)
    async def check(): pass
    async def trace(*args): traces.append(args)
    session = IdleToolSession(service, SimpleNamespace(), check, trace, 1)
    class Provider:
        async def tool_turn(self, model, messages, tools, tool_choice):
            assert [t['function']['name'] for t in tools] == ['search_memory']
            return ToolTurn('', '', (ToolCall('one', 'search_memory', '{"query":"偏好"}'),
                                     ToolCall('two', 'search_memory', '{"query":"第二次"}')))
    messages = []
    assert await session.turn(Provider(), None, messages, allow_tools=True) is None
    assert calls == ['偏好'] and session.used == 1
    assert len([item for item in messages if item['role'] == 'tool']) == 2
    assert all('偏好' not in str(item) for item in traces)


async def test_final_turn_disables_tool_execution_and_caps_only_supported_parameters(monkeypatch):
    from agi_wind.delivery.idle_tools import IdleToolSession
    from agi_wind.agent.runner import ToolSpec
    from agi_wind.agent.tools import MemoryQuery
    from agi_wind.core.types import ModelRef
    async def execute(args): raise AssertionError('final turn must not run tools')
    monkeypatch.setattr('agi_wind.delivery.idle_tools.StationTools.specs', lambda self: [
        ToolSpec('search_memory', 'memory', MemoryQuery, execute)])
    service = SimpleNamespace(db=None, agent_settings=lambda: AgentSettings(enabled=True), agent_secret_store=None)
    async def noop(*args): pass
    model = ModelRef(provider_id='test', model_id='test', parameters={'temperature': .2})
    class Provider:
        config = SimpleNamespace(supported_parameters=['temperature', 'max_output_tokens'])
        async def tool_turn(self, bounded_model, messages, tools, tool_choice):
            assert tool_choice == 'none'
            assert bounded_model.parameters == {'temperature': .2, 'max_output_tokens': 512}
            return ToolTurn('final')
    session = IdleToolSession(service, SimpleNamespace(), noop, noop)
    assert await session.turn(Provider(), model, [], allow_tools=False) == 'final'
    assert model.parameters == {'temperature': .2}


async def test_plain_provider_falls_back_without_unsupported_token_parameter():
    from agi_wind.delivery.idle_tools import IdleToolSession
    from agi_wind.core.types import ModelRef
    async def noop(*args): pass
    service = SimpleNamespace(db=None, agent_settings=lambda: AgentSettings(enabled=False), agent_secret_store=None)
    class Provider:
        config = SimpleNamespace(supported_parameters=['temperature'])
        async def complete(self, model, messages):
            assert model.parameters == {'temperature': .2}
            return 'plain'
    model = ModelRef(provider_id='test', model_id='test', parameters={'temperature': .2})
    assert await IdleToolSession(service, SimpleNamespace(), noop, noop).turn(Provider(), model, []) == 'plain'


async def test_unknown_and_denied_tools_get_matching_errors_without_execution(monkeypatch):
    from agi_wind.delivery.idle_tools import IdleToolSession
    from agi_wind.agent.runner import ToolSpec
    from agi_wind.agent.tools import MemoryQuery
    from agi_wind.core.errors import AppError
    import json
    async def execute(args): raise AssertionError('denied tool must not execute')
    async def deny(args): raise AppError('TOOL_NOT_ALLOWED', 'denied', 403)
    monkeypatch.setattr('agi_wind.delivery.idle_tools.StationTools.specs', lambda self: [
        ToolSpec('search_memory', 'memory', MemoryQuery, execute, deny)])
    service = SimpleNamespace(db=None, agent_settings=lambda: AgentSettings(enabled=True), agent_secret_store=None)
    async def noop(*args): pass
    class Provider:
        async def tool_turn(self, *args, **kwargs):
            return ToolTurn(tool_calls=(ToolCall('one', 'run_command', '{}'),
                ToolCall('two', 'search_memory', '{"query":"test"}')))
    messages = []
    session = IdleToolSession(service, SimpleNamespace(), noop, noop)
    assert await session.turn(Provider(), None, messages) is None
    results = [item for item in messages if item['role'] == 'tool']
    assert [item['tool_call_id'] for item in results] == ['one', 'two']
    assert all(json.loads(item['content'])['error'] == 'TOOL_NOT_ALLOWED' for item in results)
    assert session.used == 2


async def test_session_accepts_three_call_policy_and_missing_agent_settings():
    from agi_wind.delivery.idle_tools import IdleToolSession
    async def noop(*args): pass
    session = IdleToolSession(SimpleNamespace(db=None), SimpleNamespace(), noop, noop, max_calls=3)
    assert session.max_calls == 3
    class Provider:
        async def complete(self, model, messages): return 'plain'
    assert await session.turn(Provider(), None, []) == 'plain'
