from dataclasses import dataclass
from uuid import uuid4
import pytest


@dataclass
class Limits:
    memory_daily_budget_usd: float = 0.01
    memory_daily_token_limit: int = 1000
    memory_input_price_per_million: float = 1.0
    memory_output_price_per_million: float = 2.0


def test_budget_reservation_is_atomic_and_settlement_is_idempotent(db, seeded_scope):
    from nagi.memory.budget import MemoryBudget
    from nagi.core.errors import AppError
    budget = MemoryBudget(db, Limits())
    job = uuid4()
    first = budget.reserve(seeded_scope, job, 1, 400, 400)
    assert budget.reserve(seeded_scope, job, 1, 400, 400)['id'] == first['id']
    with pytest.raises(AppError, match='预算'):
        budget.reserve(seeded_scope, uuid4(), 1, 200, 200)
    budget.settle(first['id'], {'prompt_tokens': 50, 'completion_tokens': 10, 'usage_estimated': False})
    budget.settle(first['id'], {'prompt_tokens': 500, 'completion_tokens': 500, 'usage_estimated': False})
    state = budget.stats(seeded_scope.account_id)
    assert state['tokens'] == 60
    assert state['estimated_usd'] == pytest.approx(0.00007)
    assert state['reserved_usd'] == 0
    assert budget.reserve(seeded_scope, uuid4(), 1, 400, 400)


def test_zero_budget_blocks_before_provider_and_unknown_usage_keeps_bound(db, seeded_scope):
    from nagi.memory.budget import MemoryBudget
    from nagi.core.errors import AppError
    limits = Limits(memory_daily_budget_usd=0)
    with pytest.raises(AppError):
        MemoryBudget(db, limits).reserve(seeded_scope, uuid4(), 1, 1, 1)
    budget = MemoryBudget(db, Limits())
    reservation = budget.reserve(seeded_scope, uuid4(), 1, 100, 100)
    budget.settle(reservation['id'], None)
    stats = budget.stats(seeded_scope.account_id)
    assert stats['tokens'] == 200
    assert stats['estimated_usd'] == pytest.approx(0.0003)
