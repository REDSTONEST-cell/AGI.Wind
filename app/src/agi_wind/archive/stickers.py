"""Scoped library metadata; binary images remain in the authoritative asset store."""
from contextlib import nullcontext
from sqlalchemy import select
from . import models as m
from .repository import lock
from ..identity.service import authorize
from ..core.errors import AppError


def attachment(asset, kind='image', name='', **extra):
    metadata = asset['metadata'] or {}
    value = {'asset_id': str(asset['id']), 'kind': kind, 'name': name,
             'mime': asset['mime'], 'width': metadata.get('width'),
             'height': metadata.get('height')}
    if kind == 'audio' or str(asset['mime']).startswith('audio/'):
        value.update(duration_ms=metadata.get('duration_ms'),
                     sample_rate=metadata.get('sample_rate'),
                     channels=metadata.get('channels'))
    value.update(extra)
    return value


class StickerService:
    def __init__(self, db): self.db = db

    def get(self, actor, sticker_id):
        with self.db.engine.connect() as conn:
            row = conn.execute(select(m.stickers).where(m.stickers.c.id==sticker_id, m.stickers.c.account_id==actor.account_id)).mappings().first()
            if not row: raise AppError('NOT_FOUND', '表情包不存在', 404)
            authorize(conn, actor, row['companion_id'])
            return dict(row)

    def list(self, actor, companion_id, *, active_only=False, allow_quarantine=False, connection=None):
        with (self.db.engine.connect() if connection is None else nullcontext(connection)) as conn:
            if allow_quarantine:
                row = conn.execute(select(m.companions).where(m.companions.c.id==companion_id, m.companions.c.account_id==actor.account_id)).first()
                if not row: raise AppError('FORBIDDEN', '无权访问角色', 403)
            else: authorize(conn, actor, companion_id)
            query = select(m.stickers).where(m.stickers.c.account_id==actor.account_id, m.stickers.c.companion_id==companion_id)
            if active_only: query = query.where(m.stickers.c.state=='active')
            rows = list(conn.execute(query.order_by(m.stickers.c.created_at.desc(), m.stickers.c.id)).mappings())
            assets = {row['id']:row for row in conn.execute(select(m.assets).where(m.assets.c.id.in_([row['asset_id'] for row in rows]))).mappings()}
            return [{**dict(row), 'attachment':attachment(assets[row['asset_id']], 'sticker', row['name'])} for row in rows]

    def save_from_asset(self, actor, companion_id, asset_id, *, name='', description='', tags=None, source='uploaded', source_event_id=None, connection=None):
        with (self.db.engine.begin() if connection is None else nullcontext(connection)) as conn:
            lock(conn, companion_id)
            authorize(conn, actor, companion_id)
            asset = conn.execute(select(m.assets).join(m.asset_owners).where(m.assets.c.id==asset_id, m.asset_owners.c.account_id==actor.account_id)).mappings().first()
            if not asset or asset['kind']!='image': raise AppError('ASSET_UNAVAILABLE', '图片不存在或无权访问', 403)
            if source_event_id:
                event = conn.execute(select(m.events.c.id).where(m.events.c.id==source_event_id, m.events.c.companion_id==companion_id, m.events.c.account_id==actor.account_id, m.events.c.visibility=='visible')).first()
                if not event: raise AppError('ASSET_UNAVAILABLE', '来源消息不可用', 403)
            existing = conn.execute(select(m.stickers).where(m.stickers.c.companion_id==companion_id, m.stickers.c.asset_id==asset_id)).mappings().first()
            values = dict(name=(name or asset['metadata'].get('original_filename') or '图片')[:200], description=description[:2000], tags=(tags or [])[:20])
            if existing:
                # A manual edit/upload upgrades ownership of metadata; later captures cannot overwrite it.
                if source=='captured' and existing['source']=='uploaded': return dict(existing)
                if source=='uploaded': values.update(source='uploaded', source_event_id=None)
                return dict(conn.execute(m.stickers.update().where(m.stickers.c.id==existing['id']).values(**values).returning(m.stickers)).mappings().one())
            return dict(conn.execute(m.stickers.insert().values(account_id=actor.account_id, companion_id=companion_id, asset_id=asset_id, source=source, source_event_id=source_event_id, **values).returning(m.stickers)).mappings().one())

    def update(self, actor, sticker_id, changes=None, *, delete=False):
        with self.db.engine.begin() as conn:
            row = conn.execute(select(m.stickers).where(m.stickers.c.id==sticker_id, m.stickers.c.account_id==actor.account_id)).mappings().first()
            if not row: raise AppError('NOT_FOUND', '表情包不存在', 404)
            lock(conn, row['companion_id']); authorize(conn, actor, row['companion_id'])
            if delete:
                conn.execute(m.stickers.delete().where(m.stickers.c.id==sticker_id))
                return {'deleted':True}
            values = dict(changes or {})
            if 'name' in values or 'tags' in values: values.update(source='uploaded', source_event_id=None)
            return dict(conn.execute(m.stickers.update().where(m.stickers.c.id==sticker_id).values(**values).returning(m.stickers)).mappings().one())
