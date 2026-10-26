import json
import httpx
import pytest
from agi_wind.core.settings import ProviderSettings
from agi_wind.core.types import ModelRef


async def test_memory_completion_is_bounded_and_reports_real_tokens():
    from agi_wind.providers.openai_compatible import OpenAICompatibleProvider
    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'choices':[{'message':{'content':'{}'}}],
            'usage':{'prompt_tokens':41,'completion_tokens':7}})
    provider = OpenAICompatibleProvider(ProviderSettings(base_url='https://api.deepseek.com',credential_ref='unused'),
        'synthetic', httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        result = await provider.complete_memory(ModelRef(provider_id='fake',model_id='deepseek-flash'), [], max_tokens=512)
        assert result['text'] == '{}'
        assert result['usage']['prompt_tokens'] == 41
        assert bodies[0]['thinking'] == {'type':'disabled'}
        assert bodies[0]['max_tokens'] == 512 and bodies[0]['stream'] is False
    finally:
        await provider.aclose()


async def test_metered_extractor_counts_repairs_even_when_schema_stays_invalid():
    from agi_wind.memory.metering import MeteredExtractor
    from agi_wind.core.errors import AppError
    class Fake:
        async def complete_memory(self, model, messages, **kwargs):
            return {'text':'invalid', 'usage':{'prompt_tokens':10,'completion_tokens':2,'usage_estimated':False}}
    extractor = MeteredExtractor(Fake(),ModelRef(provider_id='fake',model_id='m'))
    with pytest.raises(AppError) as caught:
        await extractor.extract(None, [])
    assert caught.value.memory_usage['prompt_tokens'] == 20
    assert caught.value.memory_usage['completion_tokens'] == 4
    assert caught.value.memory_usage['usage_estimated'] is False
