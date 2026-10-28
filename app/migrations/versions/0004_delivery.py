from alembic import op
from nagi.delivery.models import DELIVERY_TABLES
from nagi.archive.models import metadata
revision = "0004_delivery"
down_revision = "0003_memory"
def upgrade(): metadata.create_all(op.get_bind(), tables=[metadata.tables[name] for name in DELIVERY_TABLES])
def downgrade(): metadata.drop_all(op.get_bind(), tables=[metadata.tables[name] for name in reversed(DELIVERY_TABLES)])
