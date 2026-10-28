"""Token and cost quota policy for local enforcement.

All limits are daily and account-scoped. ``0`` means unlimited; ``enabled=False``
disables the accounting gate entirely (the ledger may still record usage).
"""

from pydantic import Field

from ..core.types import DTO


class QuotaSettings(DTO):
    """Daily quota limits. Token counts are estimates unless the provider measured them."""

    enabled: bool = False
    daily_total_tokens: int = Field(default=0, ge=0)
    daily_web_tokens: int = Field(default=0, ge=0)
    daily_qq_tokens: int = Field(default=0, ge=0)
    daily_memory_tokens: int = Field(default=0, ge=0)
    daily_cost_cny: float = Field(default=0.0, ge=0)
    # 0 disables the warning band; 80 means warn at 80% of any configured limit.
    warning_percent: int = Field(default=80, ge=0, le=100)
    # True blocks the request once a configured limit is reached; False only warns.
    hard_stop: bool = True
