from uuid import uuid4
import pytest
from pydantic import ValidationError


def test_request_rejects_extra_identity_and_image_overflow():
    from nagi.core.types import ChatRequest
    value = dict(request_id=str(uuid4()), conversation_id=str(uuid4()), input={"text": "你好"}, device_id="test")
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({**value, "account_id": str(uuid4())})
    value["input"]["asset_ids"] = [str(uuid4()) for _ in range(5)]
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(value)


def test_audio_requires_real_positive_metadata():
    from nagi.core.events import AudioSegment
    with pytest.raises(ValidationError):
        AudioSegment.model_validate(dict(generation_id=str(uuid4()), epoch=1, segment_id=str(uuid4()), segment_index=0, text="你好", voice_id="test", voice_version="1", asset_id=str(uuid4()), sample_rate=0, channels=1, duration_ms=1))


def test_source_offsets_are_paired_and_ordered():
    from nagi.core.types import SourceRef
    for offsets in ({"start_offset": 1}, {"start_offset": 3, "end_offset": 2}):
        with pytest.raises(ValidationError):
            SourceRef(event_id=uuid4(), json_pointer="/text", **offsets)


def test_wire_schema_validates_python_payload():
    from nagi.core.types import ChatRequest
    import jsonschema
    request = ChatRequest(request_id=uuid4(), conversation_id=uuid4(), input={"text": "测试"}, device_id="browser")
    payload = request.model_dump(mode="json")
    jsonschema.validate(payload, ChatRequest.model_json_schema())
    assert set(payload) == {"request_id", "conversation_id", "input", "output", "device_id"}
