from pathlib import Path
from sqlalchemy import inspect,text
from alembic import command
from alembic.config import Config


def test_preexisting_projection_tables_gain_rates_and_deletion_cascades(db):
    root=Path(__file__).resolve().parents[3]
    cfg=Config(str(root/'app/alembic.ini'))
    cfg.set_main_option('script_location',str(root/'app/migrations'))
    with db.engine.begin() as conn:
        for name in ('input_rate','output_rate','usage_estimated'):
            conn.execute(text('ALTER TABLE memory_usage DROP COLUMN '+name))
        constraint=next(item for item in inspect(conn).get_foreign_keys('memory_vectors') if item['constrained_columns']==['memory_id'])
        name=constraint['name']
        conn.execute(text('ALTER TABLE memory_vectors DROP CONSTRAINT "'+name+'"'))
        conn.execute(text('ALTER TABLE memory_vectors ADD CONSTRAINT "'+name+'" FOREIGN KEY (memory_id) REFERENCES memory_items(id)'))
        cfg.attributes['connection']=conn
        command.stamp(cfg,'0012_memory_completion')
        command.upgrade(cfg,'head')
        columns={item['name'] for item in inspect(conn).get_columns('memory_usage')}
        assert {'input_rate','output_rate','usage_estimated'} <= columns
        for table in ('memory_vectors','chunk_vectors','memory_usage'):
            assert all(item['options'].get('ondelete')=='CASCADE' for item in inspect(conn).get_foreign_keys(table))
