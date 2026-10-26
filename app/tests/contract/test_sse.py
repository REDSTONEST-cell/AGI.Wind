from uuid import uuid4
from agi_wind.core.events import StreamEvent

def test_sse_uses_integer_sequence_and_json_escapes_newlines():
    from agi_wind.delivery.stream import encode_sse
    event = StreamEvent(sequence=12, type="text.delta", generation_id=uuid4(),epoch=1,payload={"text":"你好\n世界"})
    frame = encode_sse(event)
    assert frame.startswith("id: 12\nevent: text.delta\ndata: ")
    assert frame.endswith("\n\n") and len(frame.splitlines()) == 4
