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


async def test_autonomous_native_reasoning_is_bounded_and_does_not_change_dialogue():
    bodies=[]
    def response(request):
        body=json.loads(request.content); bodies.append(body)
        if body['stream']:
            return httpx.Response(200,text='data: {"choices":[{"delta":{"reasoning_content":"native only"}}]}\n\ndata: {"choices":[{"delta":{"content":"{\\"action\\":\\"silent\\"}"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(200,json={'choices':[{'message':{'reasoning_content':'native tool turn','content':'{"action":"silent"}'},'finish_reason':'stop'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        provider=OpenAICompatibleProvider(ProviderSettings(base_url='https://api.deepseek.com',credential_ref='TEST'),'synthetic',client=client)
        model=ModelRef(provider_id='deepseek',model_id='deepseek-flash')
        chunks=[item async for item in provider.stream_cognition(model,[])]
        assert [(item.kind,item.text) for item in chunks]==[('reasoning','native only'),('text','{"action":"silent"}')]
        assert bodies[-1]['thinking']=={'type':'enabled'} and bodies[-1]['reasoning_effort']=='low'
        assert bodies[-1]['max_tokens']==4096
        turn=await provider.cognition_turn(model,[],[])
        assert turn.reasoning=='native tool turn' and bodies[-1]['max_tokens']==4096
        _=[item async for item in provider.stream(model,[])]
        assert 'reasoning_effort' not in bodies[-1] and 'max_tokens' not in bodies[-1]
        provider.config=ProviderSettings(base_url='https://other.invalid',credential_ref='TEST')
        _=[item async for item in provider.stream_cognition(model,[])]
        assert 'thinking' not in bodies[-1] and 'max_tokens' not in bodies[-1]
