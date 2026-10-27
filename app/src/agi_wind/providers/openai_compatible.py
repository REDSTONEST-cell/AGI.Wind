"""Explicitly configured vendor Chat Completions dialect; no implicit API key discovery."""
import json
from urllib.parse import urlsplit
import httpx
from ..core.errors import AppError
from .protocol import ProviderChunk, ToolCall, ToolTurn

class OpenAICompatibleProvider:
    def __init__(self, config, credential, client=None):
        self.config, self.credential = config, credential
        self.client = client or httpx.AsyncClient(timeout=config.timeout_seconds)

    def payload(self, model, messages, stream):
        if set(model.parameters) - set(self.config.supported_parameters):
            raise AppError("UNSUPPORTED_PARAMETER", "供应商不支持指定参数", 422)
        params = dict(model.parameters)
        if "max_output_tokens" in params: params["max_tokens"] = params.pop("max_output_tokens")
        return {"model": model.model_id, "messages": messages, "stream": stream, **params}

    async def stream_realtime(self, model, messages):
        async for chunk in self.stream(model, messages, realtime=True):
            yield chunk

    def _cognition_options(self, body):
        endpoint = urlsplit(self.config.base_url)
        if (endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
                and endpoint.port in (None,443) and body['model'] in ('deepseek-flash','deepseek-v4-pro')):
            # Autonomous work uses native reasoning, with a bounded low-effort
            # budget. These provider-owned options do not alter dialogue settings.
            body['thinking'] = {'type':'enabled'}
            body['reasoning_effort'] = 'low'
            key = 'max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
            body[key] = min(4096, int(body.get(key,4096)))
        return body

    async def stream_cognition(self, model, messages):
        async for chunk in self.stream(model, messages, cognition=True):
            yield chunk

    async def stream(self, model, messages, *, realtime=False, cognition=False):
        body = self.payload(model, messages, True)
        if cognition:
            self._cognition_options(body)
        endpoint = urlsplit(self.config.base_url)
        if (realtime and endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
                and endpoint.port in (None, 443) and model.model_id in ('deepseek-flash','deepseek-v4-pro')):
            # Provider-owned protocol option, scoped to calls; ordinary chat settings stay intact.
            # https://api-docs.deepseek.com/guides/thinking_mode/
            body['thinking'] = {'type':'disabled'}
            body.pop('reasoning_effort', None)
        try:
            async with self.client.stream("POST", self.config.base_url.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + self.credential}, json=body) as response:
                if response.status_code != 200:
                    raise AppError("PROVIDER_UNAVAILABLE", "厂商请求失败", 503)
                complete = False
                async for line in response.aiter_lines():
                    if not line.startswith("data:"): continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        complete = True
                        break
                    data = json.loads(value)
                    if "error" in data: raise AppError("PROVIDER_UNAVAILABLE", "厂商返回错误", 503)
                    choices = data.get("choices", [])
                    if choices:
                        delta = choices[0].get('delta', {})
                        reasoning = delta.get('reasoning_content') or delta.get('reasoning')
                        if isinstance(reasoning, str) and reasoning: yield ProviderChunk('reasoning', reasoning)
                        text = delta.get('content')
                        if isinstance(text, str) and text: yield ProviderChunk('text', text)
                if not complete:
                    raise AppError("PROVIDER_STREAM_INTERRUPTED", "厂商输出中途断开", 503)
        except AppError: raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise AppError("PROVIDER_UNAVAILABLE", "厂商连接或响应不可用", 503) from None

    async def complete(self, model, messages):
        body = self.payload(model, messages, False)
        try:
            response = await self.client.post(self.config.base_url.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + self.credential}, json=body)
            response.raise_for_status()
            value = response.json()["choices"][0]["message"]["content"]
            if not isinstance(value, str): raise ValueError("missing text")
            return value
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
            raise AppError("PROVIDER_UNAVAILABLE", "厂商请求或响应不可用", 503) from None

    async def complete_memory(self, model, messages, *, max_tokens=2048):
        """Bounded extraction/summary call with usage; ordinary chat behavior is unchanged."""
        body = self.payload(model, messages, False)
        limit = max(256, min(8192, int(max_tokens)))
        key = 'max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
        body[key] = min(limit, int(body.get(key, limit)))
        endpoint = urlsplit(self.config.base_url)
        if (endpoint.scheme == 'https' and endpoint.hostname == 'api.deepseek.com'
                and endpoint.port in (None, 443) and model.model_id in ('deepseek-flash','deepseek-v4-pro')):
            body['thinking'] = {'type':'disabled'}
            body.pop('reasoning_effort', None)
        try:
            response = await self.client.post(self.config.base_url.rstrip('/') + '/chat/completions',
                headers={'Authorization':'Bearer ' + self.credential}, json=body)
            response.raise_for_status()
            payload = response.json()
            choice = payload['choices'][0]
            value = choice['message']['content']
            if not isinstance(value, str) or choice.get('finish_reason') in ('length','content_filter'):
                raise ValueError('incomplete memory response')
            usage = payload.get('usage') or {}
            prompt, output = usage.get('prompt_tokens'), usage.get('completion_tokens')
            known = type(prompt) is int and type(output) is int and prompt >= 0 and output >= 0
            return {'text':value, 'usage':{'prompt_tokens':prompt if known else 0,
                'completion_tokens':output if known else 0, 'usage_estimated':not known}}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError):
            raise AppError('PROVIDER_UNAVAILABLE', '记忆模型请求或响应不可用', 503) from None

    async def cognition_turn(self, model, messages, tools, tool_choice='auto'):
        return await self.tool_turn(model,messages,tools,tool_choice,cognition=True)

    async def tool_turn(self, model, messages, tools, tool_choice='auto', *, cognition=False):
        """Native function calling, using one bounded non-stream response per round.

        Keep the legacy streaming path unchanged. No beta URL/strict-mode assumptions.
        """
        body = self.payload(model, messages, False)
        if cognition:
            self._cognition_options(body)
        if tools:
            body.update(tools=tools, tool_choice=tool_choice)
        try:
            async with self.client.stream('POST', self.config.base_url.rstrip('/') + '/chat/completions', headers={'Authorization': 'Bearer ' + self.credential}, json=body) as response:
                if response.status_code != 200:
                    raise AppError('AGENT_PROVIDER_UNAVAILABLE', '模型工具调用失败，请检查模型是否支持工具调用', 503)
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > 512 * 1024: raise ValueError('oversized response')
            payload = json.loads(raw)
            choice = payload['choices'][0]
            if choice.get('finish_reason') in ('length', 'content_filter'): raise ValueError('incomplete response')
            message = choice['message']
            text = message.get('content')
            reasoning = message.get('reasoning_content') or message.get('reasoning') or ''
            if text is not None and not isinstance(text, str): raise ValueError('invalid text')
            if not isinstance(reasoning, str) or len(reasoning) > 64000 or len(text or '') > 48000: raise ValueError('oversized text')
            raw_calls = message.get('tool_calls') or []
            if not isinstance(raw_calls, list) or len(raw_calls) > 8: raise ValueError('invalid calls')
            calls, identifiers = [], set()
            for call in raw_calls:
                function = call['function']
                identifier, name, arguments = call['id'], function['name'], function['arguments']
                if (call.get('type', 'function') != 'function' or not isinstance(identifier, str) or not identifier or len(identifier) > 200 or identifier in identifiers
                    or not isinstance(name, str) or not name or len(name) > 80 or not isinstance(arguments, str) or len(arguments) > 6000):
                    raise ValueError('invalid tool call')
                identifiers.add(identifier)
                calls.append(ToolCall(identifier, name, arguments))
            return ToolTurn(text or '', reasoning, tuple(calls))
        except AppError: raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise AppError('AGENT_RESPONSE_INVALID', '模型工具响应无效或不完整', 503) from None

    async def aclose(self): await self.client.aclose()
