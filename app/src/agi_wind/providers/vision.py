import base64
from sqlalchemy import select
from ..archive import models as m
from ..archive.repository import digest, authorized_conversation, lock
from ..core.errors import AppError

class VisionAdapter:
    def __init__(self, db, assets, complete, model):
        self.db, self.assets, self.complete, self.model = db, assets, complete, model

    async def observe(self, scope, asset_ids, question):
        with self.db.engine.begin() as conn:
            authorized_conversation(conn, scope, scope.conversation_id)
            revision = conn.scalar(select(m.companions.c.memory_revision).where(m.companions.c.id == scope.companion_id))
        parts = [{"type":"text", "text": question}]
        for asset_id in asset_ids:
            asset, path = self.assets.get(scope, asset_id)
            if not asset["mime"].startswith("image/"): raise AppError("NOT_IMAGE", "附件不是图像", 422)
            parts.append({"type":"image_url", "image_url":{"url": "data:" + asset["mime"] + ";base64," + base64.b64encode(path.read_bytes()).decode()}})
        description = await self.complete(self.model, [{"role":"user", "content": parts}])
        value = {"description": description, "text": description, "asset_ids": [str(key) for key in asset_ids], "question": question, "model_ref": self.model.model_dump(mode="json"), "epistemic_type": "observed"}
        with self.db.engine.begin() as conn:
            lock(conn, scope.companion_id)
            current = conn.scalar(select(m.companions.c.memory_revision).where(m.companions.c.id == scope.companion_id))
            if current != revision: raise AppError("STALE_CONTEXT", "观察期间记忆已变化", 409)
            event_id = conn.execute(m.events.insert().values(account_id=scope.account_id, companion_id=scope.companion_id, conversation_id=scope.conversation_id, role="system", kind="vision_observation", content=value, content_sha256=digest(value)).returning(m.events.c.id)).scalar_one()
            for asset_id in asset_ids: conn.execute(m.asset_refs.insert().values(event_id=event_id, asset_id=asset_id, purpose="vision_source"))
        return {**value, "event_id": event_id}
