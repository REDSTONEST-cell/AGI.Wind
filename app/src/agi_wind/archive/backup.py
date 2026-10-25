from pathlib import Path
from ..memory.retrieval import MemoryService
from ..memory.models import retrieval_chunks
from ..identity.service import authorize
from .repository import lock
from .exports import ExportService
from .imports import ImportService

class IndexService:
    def __init__(self,db): self.db=db
    async def rebuild_keywords(self,scope):
        with self.db.engine.begin() as conn:
            lock(conn,scope.companion_id)
            authorize(conn,scope,scope.companion_id)
            conn.execute(retrieval_chunks.delete().where(retrieval_chunks.c.companion_id==scope.companion_id,retrieval_chunks.c.account_id==scope.account_id))
        await MemoryService(self.db).index_events(scope)

class BackupService:
    """Portable consistent backup; native pg_dump and off-disk copy remain deployment checks."""
    def __init__(self,db,asset_root): self.exports=ExportService(db,asset_root)
    async def create(self,actor,companion_id,target_ref):
        path=await self.exports.export(actor,companion_id,Path(target_ref))
        return ImportService.validate(path)["manifest"]
