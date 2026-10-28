"""Nagi module 2/3 tables: usage ledger, memory archive, purpose state/runs."""

from alembic import op

from nagi.memory.cards import memory_archive
from nagi.purpose.models import purpose_runs, purpose_state
from nagi.usage.models import usage_ledger

revision = "0019_nagi_modules"
down_revision = "0018_ambient_observations"


def upgrade():
    bind = op.get_bind()
    usage_ledger.create(bind, checkfirst=True)
    memory_archive.create(bind, checkfirst=True)
    purpose_state.create(bind, checkfirst=True)
    purpose_runs.create(bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    purpose_runs.drop(bind, checkfirst=True)
    purpose_state.drop(bind, checkfirst=True)
    memory_archive.drop(bind, checkfirst=True)
    usage_ledger.drop(bind, checkfirst=True)
