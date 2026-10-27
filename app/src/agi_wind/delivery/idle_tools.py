"""One model turn at a time for bounded, read-only idle research.

Only temporary model history receives tool output. The caller owns run/revision
authorization via check_async and receives traces containing tool name/status only.
"""
import asyncio

from pydantic import ValidationError

from ..agent.runner import bounded_json
from ..agent.search import WebSearch
from ..agent.settings import AgentSettings
from ..agent.tools import StationTools
from ..core.errors import AppError
from .reasoning import ReasoningBuffer


_FIELDS = {
    'search_memory': 'memory_search_enabled',
    'search_stickers': 'sticker_search_enabled',
    'web_search': 'web_search_enabled',
    'web_fetch': 'web_search_enabled',
}


def _bounded_model(provider, model):
    if model is None:
        return None
    supported = getattr(getattr(provider, 'config', None), 'supported_parameters', ())
    parameters = dict(model.parameters)
    for name in ('max_output_tokens', 'max_completion_tokens', 'max_tokens'):
        if name in supported:
            existing = parameters.get(name, 512)
            parameters[name] = min(512, existing) if type(existing) is int and existing > 0 else 512
            break
    return model.model_copy(update={'parameters': parameters})


class IdleToolSession:
    def __init__(self, service, scope, check_async, trace_async, max_calls=2, enabled_getter=lambda:True, on_reasoning=None):
        self.check, self.trace = check_async, trace_async
        settings = getattr(service, 'agent_settings', None)
        self.settings = settings if callable(settings) else lambda: AgentSettings(enabled=False)
        self.max_calls = max(0, min(3, int(max_calls)))
        self.used, self.seen = 0, set()
        self.enabled=enabled_getter
        self.on_reasoning=on_reasoning
        self.last_reasoning=''

        async def no_send(*args):
            raise AppError('TOOL_NOT_ALLOWED', '独处思考不能发送内容', 403)

        search = WebSearch(self.settings(), getattr(service, 'agent_secret_store', None))
        self.tools = StationTools(service.db, scope, self.settings, search, no_send)

    async def turn(self, provider, model, messages, allow_tools=True, prefer_stream=False):
        await self.check()
        native_turn = getattr(provider, 'cognition_turn', None)
        if not callable(native_turn):
            model = _bounded_model(provider, model)
            native_turn = getattr(provider, 'tool_turn', None)
        policy = self.settings()
        specs = {spec.name: spec for spec in self.tools.specs() if spec.name in _FIELDS} if policy.enabled else {}
        if not callable(native_turn):
            if (prefer_stream or not callable(getattr(provider,'complete',None))) and callable(getattr(provider,'stream',None)):
                stream = getattr(provider,'stream_cognition',provider.stream)
                parts, size = [], 0
                thinking = ReasoningBuffer(self.on_reasoning) if self.on_reasoning else None
                async for chunk in stream(model,messages):
                    await self.check()
                    if not isinstance(chunk,str) and chunk.kind=='reasoning':
                        if thinking:
                            await thinking.add(chunk.text)
                        continue
                    if thinking:
                        await thinking.flush()
                    value = chunk if isinstance(chunk,str) else chunk.text
                    size += len(value)
                    if size>6000:
                        raise AppError('COGNITION_INVALID_OUTPUT','自主任务输出过大',502)
                    parts.append(value)
                if thinking:
                    await thinking.flush()
                return ''.join(parts)
            text = await provider.complete(model, messages)
            await self.check()
            return text
        enabled = allow_tools and self.enabled() and self.used < self.max_calls
        turn = await native_turn(model, messages,
            [spec.definition() for spec in specs.values()], tool_choice='auto' if enabled else 'none')
        await self.check()
        self.last_reasoning=turn.reasoning
        if turn.reasoning and self.on_reasoning:
            await self.on_reasoning(turn.reasoning)
        if not turn.tool_calls:
            return turn.text
        assistant = {'role': 'assistant', 'content': turn.text or None, 'tool_calls': [
            {'id': call.id, 'type': 'function', 'function': {'name': call.name, 'arguments': call.arguments}}
            for call in turn.tool_calls]}
        if turn.reasoning:
            assistant['reasoning_content'] = turn.reasoning
        messages.append(assistant)
        for call in turn.tool_calls:
            await self.check()
            status = 'failed'
            try:
                if not enabled or self.used >= self.max_calls:
                    raise AppError('TOOL_BUDGET_EXCEEDED', '独处工具次数已用完', 422)
                if call.id in self.seen:
                    raise AppError('TOOL_CALL_REUSED', '重复调用标识已拦截', 422)
                self.seen.add(call.id)
                self.used += 1
                spec = specs.get(call.name)
                if spec is None or not self.enabled():
                    raise AppError('TOOL_NOT_ALLOWED', '该工具不允许用于独处思考', 403)
                self.tools.check(_FIELDS[call.name])
                if not isinstance(call.arguments, str) or len(call.arguments) > 6000:
                    raise ValueError('invalid arguments')
                args = spec.arguments.model_validate_json(call.arguments)
                await self.trace(call.name,'started')
                async with asyncio.timeout(5):
                    if spec.authorize:
                        await spec.authorize(args)
                    await self.check()
                    self.tools.check(_FIELDS[call.name])
                    value = await spec.execute(args)
                    await self.check()
                    self.tools.check(_FIELDS[call.name])
                result = bounded_json(value, min(3000, self.settings().max_result_chars))
                status = 'completed'
            except (ValidationError, ValueError, TypeError):
                result = '{"error":"TOOL_ARGUMENTS_INVALID"}'
            except TimeoutError:
                result = '{"error":"TOOL_TIMEOUT"}'
            except AppError as error:
                if error.code in ('STALE_CONTEXT', 'GENERATION_TERMINAL', 'AGENT_DISABLED'):
                    raise
                result = bounded_json({'error': error.code}, 300)
            except Exception:
                result = '{"error":"TOOL_FAILED"}'
            await self.check()
            messages.append({'role': 'tool', 'tool_call_id': call.id, 'content': result})
            await self.trace(call.name, status)
        return None
