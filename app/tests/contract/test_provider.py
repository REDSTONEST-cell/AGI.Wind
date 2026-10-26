import json
from uuid import uuid4
import httpx
import pytest
from agi_wind.core.errors import AppError
from agi_wind.core.types import ModelRef
from agi_wind.core.settings import ProviderSettings

async def test_stream_payload_filters_parameters_and_decodes_deltas():
    from agi_wind.providers.openai_compatible import OpenAICompatibleProvider
    captured = []
    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type":"text/event-stream"}, text='data: {"choices":[{"delta":{"content":"你好"}}]}\n\ndata: [DONE]\n\n')
    config = ProviderSettings(base_url="https://vendor.invalid/v1", credential_ref="UNUSED", supported_parameters=["max_output_tokens"])
    provider = OpenAICompatibleProvider(config, credential="synthetic-secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    model = ModelRef(provider_id="test", model_id="model-a", parameters={"max_output_tokens":32})
    result = [delta async for delta in provider.stream(model, [{"role":"user","content":"测试"}])]
    assert [(chunk.kind, chunk.text) for chunk in result] == [("text", "你好")]
    assert captured[0]["max_tokens"] == 32 and "max_output_tokens" not in captured[0]
    with pytest.raises(AppError) as error:
        await anext(provider.stream(model.model_copy(update={"parameters":{"unsupported":1}}), []))
    assert error.value.status == 422
    await provider.aclose()

async def test_provider_error_is_redacted_and_stream_truncation_is_failure():
    from agi_wind.providers.openai_compatible import OpenAICompatibleProvider
    config = ProviderSettings(base_url="https://vendor.invalid/v1", credential_ref="UNUSED")
    for response in [httpx.Response(401, text="synthetic-secret private upstream path"), httpx.Response(200, text='data: {"choices":[{"delta":{"content":"半句"}}]}\n\n')]:
        provider = OpenAICompatibleProvider(config, "synthetic-secret", httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)))
        with pytest.raises(AppError) as error:
            _ = [delta async for delta in provider.stream(ModelRef(provider_id="test",model_id="model"), [])]
        assert "synthetic-secret" not in str(error.value)
        assert error.value.code in ("PROVIDER_UNAVAILABLE", "PROVIDER_STREAM_INTERRUPTED")
        await provider.aclose()


async def test_reasoning_alias_and_content_are_separate_ordered_chunks():
    from agi_wind.providers.openai_compatible import OpenAICompatibleProvider
    config = ProviderSettings(base_url='https://vendor.invalid/v1', credential_ref='UNUSED')
    lines = [json.dumps({'choices':[{'delta':delta}]}) for delta in (
        {'reasoning_content':'先思考', 'content':'回答'}, {'reasoning':'再思考'}, {'content':'完成'})]
    response = httpx.Response(200, text=''.join('data: '+line+'\n\n' for line in lines)+'data: [DONE]\n\n')
    provider = OpenAICompatibleProvider(config, 'synthetic', httpx.AsyncClient(transport=httpx.MockTransport(lambda req:response)))
    try:
        chunks = [chunk async for chunk in provider.stream(ModelRef(provider_id='fake', model_id='test'), [])]
        assert [(chunk.kind, chunk.text) for chunk in chunks] == [('reasoning','先思考'),('text','回答'),('reasoning','再思考'),('text','完成')]
    finally: await provider.aclose()
