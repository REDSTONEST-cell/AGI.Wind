import hashlib
import re
from uuid import uuid4
from pydantic import Field
from sqlalchemy import select
from . import models as mm
from ..archive import models as m
from ..archive.repository import lock, digest
from ..identity.service import authorize
from ..core.types import DTO, EpistemicType, SourceRef
from ..core.errors import AppError

class MemoryCandidate(DTO):
    kind: str
    content: str = Field(min_length=1)
    epistemic_type: EpistemicType
    confidence: float = Field(default=1,ge=0,le=1)
    source_refs: list[SourceRef] = Field(min_length=1)
    occurred_at: str | None = None
    relations: list[dict] = Field(default_factory=list)

class MemoryWriter:
    def __init__(self,db): self.db=db

    async def validate_and_store(self,scope,candidate,expected_revision):
        with self.db.engine.begin() as conn:
            lock(conn,scope.companion_id)
            companion=authorize(conn,scope,scope.companion_id)
            if companion["memory_revision"]!=expected_revision:
                raise AppError("STALE_CONTEXT","提炼任务的记忆版本已失效",409)
            reason, source_quotes, fictional = None, [], False
            for ref in candidate.source_refs:
                event=conn.execute(select(m.events).where(m.events.c.id==ref.event_id,m.events.c.account_id==scope.account_id,m.events.c.companion_id==scope.companion_id,m.events.c.visibility=="visible")).mappings().first()
                if not event or ref.json_pointer!="/text":
                    reason="UNVERIFIABLE_SOURCE"
                    break
                text=event["content"].get("text","")
                quote=text[ref.start_offset:ref.end_offset] if ref.start_offset is not None else text
                if ref.end_offset is not None and ref.end_offset>len(text): reason="INVALID_OFFSETS"
                if ref.quote_hash and hashlib.sha256(quote.encode()).hexdigest()!=ref.quote_hash: reason="QUOTE_HASH_MISMATCH"
                if candidate.epistemic_type=="user_asserted" and event["role"]!="user": reason="ASSISTANT_IS_NOT_USER_FACT"
                if candidate.epistemic_type=="observed" and event["kind"]!="vision_observation": reason="NOT_AN_OBSERVATION"
                fictional=fictional or bool(re.search("故事设定|虚构|假设",text))
                source_quotes.append(quote)
            # Conservative first release: automatic facts must be verifiable extracts.
            if candidate.content not in "\n".join(source_quotes): reason=reason or "UNSUPPORTED_CLAIM"
            if reason:
                key=conn.execute(mm.quarantine.insert().values(account_id=scope.account_id,companion_id=scope.companion_id,candidate=candidate.model_dump(mode="json"),reason=reason).returning(mm.quarantine.c.id)).scalar_one()
                return {"id":key,"status":"quarantine","reason":reason}
            kind="fictional" if fictional else candidate.epistemic_type
            existing=conn.execute(select(mm.memory_items.c.id).where(mm.memory_items.c.account_id==scope.account_id,mm.memory_items.c.companion_id==scope.companion_id,mm.memory_items.c.content==candidate.content,mm.memory_items.c.epistemic_type==kind,mm.memory_items.c.status=="active")).scalar()
            if existing: return {"id":existing,"status":"active"}
            key=uuid4()
            conn.execute(mm.memory_items.insert().values(id=key,account_id=scope.account_id,companion_id=scope.companion_id,user_id=scope.user_id,kind=candidate.kind,content=candidate.content,epistemic_type=kind,confidence=candidate.confidence,revision=expected_revision))
            for ref in candidate.source_refs:
                conn.execute(mm.source_refs.insert().values(memory_id=key,**ref.model_dump()))
            return {"id":key,"status":"active"}

class Extractor:
    """A configured extraction model proposes candidates; the writer is the trust gate."""
    def __init__(self,complete,model): self.complete,self.model=complete,model
    async def extract(self,scope,event_batch):
        import json
        raw=await self.complete(self.model,[{"role":"system","content":"只抽取有精确原文出处的记忆，返回 JSON 数组；source_refs 必须指向提供的 event_id。用户事实、AI 自述、虚构剧情严格分开。"},{"role":"user","content":json.dumps(event_batch,ensure_ascii=False,default=str)}])
        try: return [MemoryCandidate.model_validate(item) for item in json.loads(raw)]
        except (ValueError,TypeError): raise AppError("EXTRACTION_INVALID","记忆提炼响应无效",503) from None
