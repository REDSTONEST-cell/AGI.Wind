from nagi.core.types import ModelRef
from nagi.memory.extraction import Extractor


async def test_server_summary_prompt_survives_bounded_repair():
    prompts = []

    async def complete(model, messages):
        prompts.append(messages[0]["content"])
        return "invalid" if len(prompts) == 1 else '{"schema_version":1,"candidates":[]}'

    extractor = Extractor(complete, ModelRef(provider_id="test", model_id="test"), system_prompt="trusted summary policy")
    candidates, usage = await extractor.extract(None, [])
    assert candidates == [] and usage["calls"] == 2
    assert prompts == ["trusted summary policy", "trusted summary policy"]
