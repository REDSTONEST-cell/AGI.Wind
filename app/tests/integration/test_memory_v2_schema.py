from sqlalchemy import inspect, select
from agi_wind.memory import models as mm


def test_structured_memory_schema_and_legacy_defaults(db, seeded_scope):
    columns = {item['name'] for item in inspect(db.engine).get_columns('memory_items')}
    assert {'subject_key', 'predicate', 'value', 'valid_from', 'valid_to', 'is_pinned', 'summary_level'} <= columns
    with db.engine.begin() as conn:
        key = conn.execute(mm.memory_items.insert().values(account_id=seeded_scope.account_id,
            companion_id=seeded_scope.companion_id, user_id=seeded_scope.user_id,
            kind='fact', content='合成旧版记忆', epistemic_type='user_asserted').returning(mm.memory_items.c.id)).scalar_one()
        row = conn.execute(select(mm.memory_items).where(mm.memory_items.c.id == key)).mappings().one()
    assert row['is_pinned'] is False
    assert row['subject_key'] == row['predicate'] == row['value'] == row['summary_level'] == ''
    assert row['valid_to'] is None


def test_derived_memory_storage_is_separate_from_portable_archive(db):
    from agi_wind.archive.exports import ARCHIVE_TABLE_ORDER
    tables = set(inspect(db.engine).get_table_names())
    assert {'memory_vectors', 'chunk_vectors', 'memory_usage'} <= tables
    assert not {'memory_vectors', 'chunk_vectors', 'memory_usage'} & set(ARCHIVE_TABLE_ORDER)


def test_deleting_authoritative_memory_cascades_derived_vector(db, seeded_scope):
    with db.engine.begin() as conn:
        mid = conn.execute(mm.memory_items.insert().values(account_id=seeded_scope.account_id,
            companion_id=seeded_scope.companion_id, user_id=seeded_scope.user_id,
            kind='fact', content='合成删除验证', epistemic_type='user_asserted').returning(mm.memory_items.c.id)).scalar_one()
        sid = conn.execute(mm.embedding_spaces.insert().values(fingerprint='synthetic-delete-space',config={}).returning(mm.embedding_spaces.c.id)).scalar_one()
        conn.execute(mm.memory_vectors.insert().values(memory_id=mid,space_id=sid,
            content_hash='synthetic',vector=b'1234',revision=1))
        conn.execute(mm.memory_items.delete().where(mm.memory_items.c.id == mid))
        assert conn.execute(select(mm.memory_vectors)).first() is None
