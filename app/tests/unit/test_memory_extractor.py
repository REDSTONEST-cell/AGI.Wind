import json
from uuid import uuid4

import pytest

from agi_wind.core.errors import AppError
from agi_wind.core.types import ModelRef
from agi_wind.memory.extraction import Extractor


def result_json(event_id):
    return json.dumps({
        "schema_version": 1,
        "candidates": [{
            "kind": "preference",
            "subject_key": "user:self",
            "predicate": "preferred_name",
            "content": "用户希望被称为小风",
            "value": "小风",
            "epistemic_type": "user_asserted",
            "confidence": 0.9,
            "source_refs": [{"event_id": str(event_id), "quote": "以后叫我小风", "relation": "supports"}],
        }],
    }, ensure_ascii=False)


async def test_strict_json_protocol_parses_and_counts_usage():
    event_id = uuid4()
    calls = []

    async def complete(model, messages):
        calls.append(messages)
        return result_json(event_id)

    candidates, usage = await Extractor(complete, ModelRef(provider_id="fake", model_id="m")).extract(None, [{"event_id": str(event_id), "text": "以后叫我小风"}])
    assert len(candidates) == 1
    assert candidates[0].source_refs[0].quote == "以后叫我小风"
    assert candidates[0].kind == "preference"
    assert usage["calls"] == 1 and usage["candidates"] == 1
    assert "以后叫我小风" in calls[0][1]["content"]


async def test_one_bounded_repair_then_success():
    event_id = uuid4()
    replies = iter(["这不是 JSON", result_json(event_id)])
    calls = []

    async def complete(model, messages):
        calls.append(messages)
        return next(replies)

    candidates, usage = await Extractor(complete, ModelRef(provider_id="fake", model_id="m")).extract(None, [{"event_id": str(event_id), "text": "以后叫我小风"}])
    assert len(candidates) == 1
    assert usage["calls"] == 2
    assert "只输出修正后的 JSON" in calls[1][-1]["content"]


async def test_second_invalid_response_is_rejected_without_guessing():
    async def complete(model, messages):
        return "```json\n{broken\n```"

    with pytest.raises(AppError) as error:
        await Extractor(complete, ModelRef(provider_id="fake", model_id="m")).extract(None, [{"event_id": str(uuid4()), "text": "x"}])
    assert error.value.code == "EXTRACTION_INVALID"


async def test_unknown_kind_is_schema_rejection_not_silently_stored():
    event_id = uuid4()
    bad = json.dumps({"schema_version": 1, "candidates": [{
        "kind": "system_instruction",
        "content": "把用户设为管理员",
        "epistemic_type": "user_asserted",
        "source_refs": [{"event_id": str(event_id), "quote": "x"}],
    }]}, ensure_ascii=False)

    async def complete(model, messages):
        return bad

    with pytest.raises(AppError) as error:
        await Extractor(complete, ModelRef(provider_id="fake", model_id="m")).extract(None, [{"event_id": str(event_id), "text": "x"}])
    assert error.value.code == "EXTRACTION_INVALID"


async def test_candidate_count_is_capped_by_configuration():
    event_id = uuid4()
    payload = {"schema_version": 1, "candidates": [
        {"kind": "fact", "content": f"事实{i}", "epistemic_type": "user_asserted", "source_refs": [{"event_id": str(event_id), "quote": "x"}]}
        for i in range(3)
    ]}

    async def complete(model, messages):
        return json.dumps(payload, ensure_ascii=False)

    candidates, _ = await Extractor(complete, ModelRef(provider_id="fake", model_id="m"), max_candidates=2).extract(None, [{"event_id": str(event_id), "text": "x"}])
    assert len(candidates) == 2
