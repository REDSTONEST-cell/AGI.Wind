import hashlib
import json
from uuid import uuid4
from sqlalchemy import select, func, text
from . import models as m
from ..core.types import Scope
from ..core.errors import AppError


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def lock(conn, key):
    # Bounded signed advisory key; input never becomes SQL text.
    number = int.from_bytes(hashlib.sha256(str(key).encode()).digest()[:8], "big", signed=True)
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def authorized_conversation(conn, actor, conversation_id):
    row = conn.execute(select(m.conversations).where(m.conversations.c.id == conversation_id)).mappings().first()
    if not row:
        raise AppError("NOT_FOUND", "会话不存在", 404)
    if row["account_id"] != actor.account_id or row["user_id"] != actor.user_id:
        raise AppError("FORBIDDEN", "无权访问此会话", 403)
    return row


class Database:
    def __init__(self, engine): self.engine = engine

    def count(self, table):
        with self.engine.connect() as conn:
            return conn.scalar(select(func.count()).select_from(m.metadata.tables[table]))

    def all(self, table, **filters):
        target = m.metadata.tables[table]
        query = select(target)
        for name, value in filters.items(): query = query.where(target.c[name] == value)
        with self.engine.connect() as conn:
            return [dict(row) for row in conn.execute(query).mappings()]

    def seed(self, actor):
        """Explicit development identity; never silently creates a formal persona."""
        from sqlalchemy.dialects.postgresql import insert
        companion_id, conversation_id = uuid4(), uuid4()
        with self.engine.begin() as conn:
            conn.execute(insert(m.accounts).values(id=actor.account_id).on_conflict_do_nothing())
            conn.execute(insert(m.users).values(id=actor.user_id, account_id=actor.account_id).on_conflict_do_nothing())
            conn.execute(m.companions.insert().values(id=companion_id, account_id=actor.account_id, name="开发测试角色"))
            conn.execute(m.conversations.insert().values(id=conversation_id, account_id=actor.account_id, companion_id=companion_id, user_id=actor.user_id))
            from ..identity.models import persona_versions
            conn.execute(persona_versions.insert().values(companion_id=companion_id, version=1, core={"name": "开发测试角色", "development_only": True}, style={}, source_refs={}))
        return Scope(**actor.model_dump(), companion_id=companion_id, conversation_id=conversation_id)
