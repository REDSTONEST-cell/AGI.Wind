from uuid import uuid4
import pytest
from agi_wind.core.errors import AppError
from agi_wind.core.types import MemoryHit, SourceRef

def test_selection_limits_per_source_and_total_budget():
    from agi_wind.memory.context import select_memories
    event = uuid4()
    hits = [MemoryHit(content="x"*80, score=1, epistemic_type="user_asserted", source_refs=[SourceRef(event_id=event if n<5 else uuid4())]) for n in range(30)]
    selected = select_memories(hits, token_count=len, budget=1000)
    assert len(selected) == 12
    assert sum(h.source_refs[0].event_id == event for h in selected) == 2
    assert sum(len(h.content) for h in selected) <= 1000

def test_current_input_is_never_silently_truncated():
    from agi_wind.memory.context import check_current_input
    with pytest.raises(AppError) as error:
        check_current_input("长"*12001, token_count=len, budget=12000)
    assert error.value.status == 413
