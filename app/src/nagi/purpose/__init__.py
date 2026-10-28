"""Purpose-driven proactive messages and conversation closure.

A companion may start a conversation instead of only replying, but only for a small,
explainable set of purposes (care, share, invite, curiosity, follow-up) and only when the
host's gates allow it. The package is deliberately free of database and web-framework
imports:

* ``settings`` - bounded :class:`PurposeSettings` plus pure quiet-hour helpers.
* ``engine`` - pure gate/selection logic producing at most one :class:`PurposeDecision`.
* ``closure`` - pure records for a thread deliberately left open for a later follow-up.
* ``service`` - :class:`PurposeService` wiring a host-provided ``PurposeStore`` to the
  engine, including an in-memory reference store.
"""
from .closure import (
    CLOSURE_PURPOSE_NAMES,
    ClosurePurpose,
    ClosureRecord,
    as_closure,
    build_closure,
    closure_material,
    consume_closure,
    from_decision,
    is_expired,
)
from .engine import (
    GUIDANCE,
    PRIORITY,
    PURPOSES,
    PurposeDecision,
    PurposeName,
    blocking_reason,
    decide,
    gate_reason,
)
from .service import (
    InMemoryPurposeStore,
    PurposeCheck,
    PurposeDeps,
    PurposeService,
    PurposeStore,
)
from .settings import PurposeSettings, in_quiet_hours, parse_clock

__all__ = [
    "CLOSURE_PURPOSE_NAMES",
    "GUIDANCE",
    "InMemoryPurposeStore",
    "PRIORITY",
    "PURPOSES",
    "ClosurePurpose",
    "ClosureRecord",
    "PurposeCheck",
    "PurposeDecision",
    "PurposeDeps",
    "PurposeName",
    "PurposeService",
    "PurposeSettings",
    "PurposeStore",
    "as_closure",
    "blocking_reason",
    "build_closure",
    "closure_material",
    "consume_closure",
    "decide",
    "from_decision",
    "gate_reason",
    "in_quiet_hours",
    "is_expired",
    "parse_clock",
]
