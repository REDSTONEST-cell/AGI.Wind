from uuid import uuid4

import pytest

from nagi.core.errors import AppError
from nagi.core.types import ChatInput, MemoryHit, ModelRef, SourceRef


def test_selection_limits_per_source_and_total_budget():
    from nagi.memory.context import select_memories

    event = uuid4()
    hits = [
        MemoryHit(content="x" * 80, score=1, epistemic_type="user_asserted", source_refs=[SourceRef(event_id=event if n < 5 else uuid4())])
        for n in range(30)
    ]
    selected = select_memories(hits, token_count=len, budget=1000)
    assert len(selected) == 12
    assert sum(h.source_refs[0].event_id == event for h in selected) == 2
    assert sum(len(h.content) for h in selected) <= 1000


def test_current_input_is_never_silently_truncated():
    from nagi.memory.context import check_current_input

    with pytest.raises(AppError) as error:
        check_current_input("长" * 12001, token_count=len, budget=12000)
    assert error.value.status == 413


async def test_long_chinese_persona_fits_configured_budget_and_over_budget_is_explicit(db, actor, seeded_scope):
    from nagi.identity.service import IdentityService
    from nagi.memory.context import ContextBuilder

    long_description = "她" * 3000  # ~9000 UTF-8 bytes, the shape that produced CONTEXT_TOO_LONG
    await IdentityService(db).update(actor, seeded_scope.companion_id, 1, {"core": {"name": "云浅月", "description": long_description}})
    model = ModelRef(provider_id="fake", model_id="synthetic")
    bundle = await ContextBuilder(db, total_budget=48000, input_budget=12000).build(
        seeded_scope, ChatInput(text="今天还好吗"), model
    )
    assert bundle.persona_snapshot.core["description"] == long_description
    assert bundle.token_estimate <= 48000
    with pytest.raises(AppError) as error:
        await ContextBuilder(db, total_budget=2000, input_budget=12000).build(seeded_scope, ChatInput(text="你好"), model)
    assert error.value.code == "CONTEXT_TOO_LONG"
    assert error.value.status == 413
