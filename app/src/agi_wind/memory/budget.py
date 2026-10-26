"""Conservative, atomic daily memory budgets. Amounts are estimates in USD, not invoices."""
from datetime import datetime, timezone
import math
from sqlalchemy import select
from .models import memory_usage
from ..archive.repository import lock
from ..core.errors import AppError


class MemoryBudget:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    @staticmethod
    def _day():
        return datetime.now(timezone.utc).date()

    def _cost(self, prompt, output, input_rate=None, output_rate=None):
        return (prompt * (self.settings.memory_input_price_per_million if input_rate is None else input_rate)
                + output * (self.settings.memory_output_price_per_million if output_rate is None else output_rate)) / 1_000_000

    def reserve(self, scope, job_id, attempt, prompt_tokens, completion_tokens):
        prompt_tokens, completion_tokens = max(0, int(prompt_tokens)), max(0, int(completion_tokens))
        amount = self._cost(prompt_tokens, completion_tokens)
        with self.db.engine.begin() as conn:
            # One account lock covers every companion and memory process. No provider call
            # occurs while this transaction or lock is held.
            lock(conn, scope.account_id)
            existing = conn.execute(select(memory_usage).where(memory_usage.c.job_id == job_id,
                memory_usage.c.attempt == attempt)).mappings().first()
            if existing:
                if existing['status'] != 'reserved':
                    raise AppError('MEMORY_USAGE_ALREADY_SETTLED', '该次记忆调用已结算', 409)
                return dict(existing)
            rows = conn.execute(select(memory_usage).where(memory_usage.c.account_id == scope.account_id,
                memory_usage.c.billing_day == self._day())).mappings().all()
            tokens = sum(row['prompt_tokens'] + row['completion_tokens'] for row in rows)
            cost = sum(row['reserved_usd'] if row['status'] == 'reserved' else row['used_usd'] for row in rows)
            if (self.settings.memory_daily_budget_usd <= 0 or self.settings.memory_daily_token_limit <= 0
                or cost + amount > self.settings.memory_daily_budget_usd + 1e-12
                or tokens + prompt_tokens + completion_tokens > self.settings.memory_daily_token_limit):
                raise AppError('MEMORY_BUDGET_EXCEEDED', '记忆每日预算已用尽，任务延后处理', 429, True)
            row = conn.execute(memory_usage.insert().values(account_id=scope.account_id,
                companion_id=scope.companion_id, job_id=job_id, attempt=attempt, billing_day=self._day(),
                reserved_usd=amount, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                input_rate=self.settings.memory_input_price_per_million,
                output_rate=self.settings.memory_output_price_per_million).returning(memory_usage)).mappings().one()
            return dict(row)

    def settle(self, reservation_id, usage):
        with self.db.engine.begin() as conn:
            row = conn.execute(select(memory_usage).where(memory_usage.c.id == reservation_id)
                .with_for_update()).mappings().one()
            if row['status'] != 'reserved':
                return
            measured = bool(usage and not usage.get('usage_estimated', True)
                and isinstance(usage.get('prompt_tokens'), int) and isinstance(usage.get('completion_tokens'), int)
                and usage['prompt_tokens'] >= 0 and usage['completion_tokens'] >= 0)
            # Missing usage may mean a paid timeout. Keep the full reserved upper bound,
            # rather than refunding an unobservable request and overspending on retries.
            prompt = usage['prompt_tokens'] if measured else row['prompt_tokens']
            output = usage['completion_tokens'] if measured else row['completion_tokens']
            cost = self._cost(prompt, output, row['input_rate'], row['output_rate']) if measured else row['reserved_usd']
            if not math.isfinite(cost):
                cost = row['reserved_usd']
            conn.execute(memory_usage.update().where(memory_usage.c.id == reservation_id).values(
                used_usd=cost, prompt_tokens=prompt, completion_tokens=output,
                usage_estimated=not measured, status='settled'))

    def stats(self, account_id):
        with self.db.engine.connect() as conn:
            rows = conn.execute(select(memory_usage).where(memory_usage.c.account_id == account_id,
                memory_usage.c.billing_day == self._day())).mappings().all()
        return {'billing_day_utc': self._day().isoformat(), 'currency': 'USD', 'cost_is_estimate': True,
            'estimated_usd': round(sum(row['used_usd'] for row in rows if row['status'] == 'settled'), 8),
            'reserved_usd': round(sum(row['reserved_usd'] for row in rows if row['status'] == 'reserved'), 8),
            'tokens': sum(row['prompt_tokens'] + row['completion_tokens'] for row in rows),
            'unknown_usage_calls': sum(row['status'] == 'settled' and row['usage_estimated'] for row in rows),
            'daily_limit_usd': self.settings.memory_daily_budget_usd,
            'daily_token_limit': self.settings.memory_daily_token_limit}


async def budgeted_extract(db, settings, scope, job, extractor, offered):
    budget, reservation = None, None
    if settings is not None:
        budget = MemoryBudget(db, settings)
        estimate = getattr(extractor, 'reservation_tokens', lambda events: (64000, 4096))(offered)
        reservation = budget.reserve(scope, job['id'], job.get('attempts', 1), *estimate)
    try:
        candidates, usage = await extractor.extract(scope, offered)
    except BaseException as error:
        if reservation:
            budget.settle(reservation['id'], getattr(error, 'memory_usage', None))
        raise
    else:
        if reservation:
            budget.settle(reservation['id'], usage)
        return candidates, usage
