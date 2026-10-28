from alembic import context
from sqlalchemy import create_engine
from nagi.archive.models import metadata

def migrate(connection):
    context.configure(connection=connection, target_metadata=metadata, version_table_schema=connection.exec_driver_sql("SELECT current_schema()").scalar())
    with context.begin_transaction(): context.run_migrations()

connection = context.config.attributes.get("connection")
if connection is not None:
    migrate(connection)
else:
    import os
    url = os.environ.get("AGI_DATABASE_URL")
    if not url: raise RuntimeError("AGI_DATABASE_URL required; secrets are never stored in alembic.ini")
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as connection: migrate(connection)
