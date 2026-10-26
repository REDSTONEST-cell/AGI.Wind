import json
import httpx
import pytest
from agi_wind.core.settings import ProviderSettings
from agi_wind.core.types import ModelRef
from agi_wind.core.errors import AppError
from agi_wind.providers.openai_compatible import OpenAICompatibleProvider


async def test_native_tool_turn_preserves_tool_ids_and_reasoning():
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': None, 'reasoning_content': '先查证', 'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'search_memory', 'arguments': '{"query":"生日"}'}}]}, 'finish_reason': 'tool_calls'}]})
    provider = OpenAICompatibleProvider(ProviderSettings(base_url='https://vendor.invalid/v1', credential_ref='UNUSED'), 'test-secret', httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        turn = await provider.tool_turn(ModelRef(provider_id='fake', model_id='m'), [], [{'type': 'function', 'function': {'name': 'search_memory'}}])
        assert turn.reasoning == '先查证' and turn.tool_calls[0].id == 'call-1'
        assert requests[0]['stream'] is False and requests[0]['tool_choice'] == 'auto'
        assert 'test-secret' not in json.dumps(requests)
    finally: await provider.aclose()


@pytest.mark.parametrize('message', [
    {'tool_calls': [{'id': 'x', 'function': {'name': 'bad', 'arguments': '{}'}}] * 9},
    {'content': None, 'tool_calls': [{'id': '', 'function': {'name': 'bad', 'arguments': '{}'}}]},
    {'content': 42},
])
async def test_invalid_tool_responses_fail_closed(message):
    provider = OpenAICompatibleProvider(ProviderSettings(base_url='https://vendor.invalid/v1', credential_ref='UNUSED'), 'synthetic', httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,json={'choices':[{'message':message}]}))))
    try:
        with pytest.raises(AppError): await provider.tool_turn(ModelRef(provider_id='fake',model_id='m'),[],[])
    finally: await provider.aclose()
