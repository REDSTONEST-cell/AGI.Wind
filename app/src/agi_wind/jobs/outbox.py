from sqlalchemy.dialects.postgresql import insert
from ..archive.models import outbox

def enqueue(conn, kind, aggregate_id, payload, dedupe_key):
    return conn.execute(insert(outbox).values(event_type=kind,aggregate_id=aggregate_id,payload=payload,dedupe_key=dedupe_key).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(outbox.c.id)).scalar()
