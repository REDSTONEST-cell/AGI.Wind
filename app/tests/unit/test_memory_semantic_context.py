from uuid import uuid4
from nagi.core.types import MemoryHit, SourceRef
from nagi.memory.context import select_memories


def test_historical_hits_are_never_injected():
    hit = MemoryHit(content="合成旧偏好", score=1, epistemic_type="user_asserted",
                    source_refs=[SourceRef(event_id=uuid4())], status="historical")
    assert select_memories([hit]) == []
