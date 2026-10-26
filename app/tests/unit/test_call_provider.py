import json
import httpx
from agi_wind.core.settings import ProviderSettings
from agi_wind.core.types import ModelRef
from agi_wind.providers.openai_compatible import OpenAICompatibleProvider


async def test_call_disables_thinking_only_for_known_official_deepseek():
    bodies = []
    def response(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"你好"}}]}\n\ndata: [DONE]\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider = OpenAICompatibleProvider(ProviderSettings(base_url='https://api.deepseek.com',credential_ref='TEST'), 'synthetic', client=client)
        model = ModelRef(provider_id='deepseek',model_id='deepseek-flash')
        assert [chunk.text async for chunk in provider.stream_realtime(model,[])] == ['你好']
        assert bodies[-1]['thinking'] == {'type':'disabled'}
        assert [chunk.text async for chunk in provider.stream(model,[])] == ['你好']
        assert 'thinking' not in bodies[-1]
        provider.config = ProviderSettings(base_url='https://other.invalid',credential_ref='TEST')
        _ = [chunk async for chunk in provider.stream_realtime(model,[])]
        assert 'thinking' not in bodies[-1]
