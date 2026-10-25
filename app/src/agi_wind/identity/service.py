from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from . import models as i
from ..archive import models as m
from ..archive.repository import lock
from ..core.errors import AppError
from ..core.types import PersonaSnapshot


def authorize(conn, actor, companion_id):
    row = conn.execute(select(m.companions).where(m.companions.c.id == companion_id)).mappings().first()
    if not row: raise AppError("NOT_FOUND", "角色不存在", 404)
    if row["account_id"] != actor.account_id: raise AppError("FORBIDDEN", "无权访问角色", 403)
    if row["status"]=="recovery_quarantine": raise AppError("RECOVERY_QUARANTINED","恢复角色尚未核实最新删除账本",409)
    user = conn.execute(select(m.users.c.id).where(m.users.c.id == actor.user_id, m.users.c.account_id == actor.account_id)).first()
    if not user: raise AppError("FORBIDDEN", "用户身份无效", 403)
    return row


class IdentityService:
    def __init__(self, db): self.db = db

    async def snapshot(self, actor, companion_id):
        with self.db.engine.begin() as conn:
            companion = authorize(conn, actor, companion_id)
            row = conn.execute(select(i.persona_versions).where(i.persona_versions.c.companion_id == companion_id, i.persona_versions.c.version == companion["active_persona_version"])).mappings().first()
            if row is None:
                raise AppError("PERSONA_MISSING", "角色人格未初始化", 409)
            relationship = conn.execute(select(i.relationships.c.data).where(i.relationships.c.companion_id == companion_id, i.relationships.c.user_id == actor.user_id)).scalar() or {}
            commitments = [dict(item) for item in conn.execute(select(i.commitments).where(i.commitments.c.companion_id == companion_id, i.commitments.c.user_id == actor.user_id, i.commitments.c.status == "pending")).mappings()]
            return PersonaSnapshot(companion_id=companion_id, version=row["version"], core=row["core"], style=row["style"], relationship=relationship, commitments=commitments)

    async def update(self, actor, companion_id, expected_version, patch, origin="user"):
        if origin != "user": raise AppError("PROPOSAL_ONLY", "人格建议须由用户接受后应用", 403)
        if set(patch) - {"core", "style", "human_notes"}: raise AppError("INVALID_PATCH", "不支持的人格字段", 422)
        if any(key in patch and not isinstance(patch[key], dict) for key in ("core", "style")):
            raise AppError("INVALID_PATCH", "人格与风格必须为对象", 422)
        with self.db.engine.begin() as conn:
            lock(conn, companion_id)
            companion = authorize(conn, actor, companion_id)
            if companion["active_persona_version"] != expected_version:
                raise AppError("PERSONA_CONFLICT", "人格版本已更新", 409)
            previous = conn.execute(select(i.persona_versions).where(i.persona_versions.c.companion_id == companion_id, i.persona_versions.c.version == expected_version)).mappings().one()
            values = {key: patch.get(key, previous[key]) for key in ("core", "style", "human_notes")}
            conn.execute(i.persona_versions.insert().values(companion_id=companion_id, version=expected_version + 1, origin=origin, **values))
            conn.execute(m.companions.update().where(m.companions.c.id == companion_id).values(active_persona_version=expected_version + 1, memory_revision=companion["memory_revision"] + 1))
            conn.execute(i.runtime_bindings.update().where(i.runtime_bindings.c.companion_id == companion_id).values(status="stale"))
            from ..delivery.invalidation import invalidate_active
            invalidate_active(conn,companion_id,"persona.changed")
        return await self.snapshot(actor, companion_id)

    async def bind_model(self, actor, companion_id, model_ref):
        with self.db.engine.begin() as conn:
            lock(conn, companion_id)
            authorize(conn, actor, companion_id)
            conn.execute(insert(i.runtime_bindings).values(companion_id=companion_id, runtime="model_selection", model_ref=model_ref.model_dump(mode="json"), status="configured").on_conflict_do_update(index_elements=["companion_id", "runtime"], set_={"model_ref": model_ref.model_dump(mode="json"), "status": "configured"}))
        return model_ref
