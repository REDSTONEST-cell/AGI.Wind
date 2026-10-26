from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4
import zipfile
from sqlalchemy import select, or_
from . import models as m
from .repository import lock
from ..identity.service import authorize
from ..identity import models as im
from ..memory import models as mm
from ..delivery import models as dm
from .deletions import deletion_tombstones
from .provenance import archive_origins
from ..core.errors import AppError

ARCHIVE_TABLE_ORDER=["accounts","users","external_principals","companions","conversations","channel_bindings","requests","generations","events","qq_message_receipts","assets","asset_owners","asset_refs","stickers","persona_versions","relationships","commitments","memory_items","source_refs","memory_relations","outbox","jobs","speech_segments","avatar_cues","playback_receipts","deletion_tombstones","quarantine","archive_origins"]

def json_bytes(value): return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str)+"\n").encode()

class ExportService:
    def __init__(self,db,asset_root): self.db,self.asset_root=db,Path(asset_root)

    def snapshot(self,conn,actor,companion_id):
        authorize(conn,actor,companion_id)
        result={}
        def take(name,condition):
            result[name]=[dict(row) for row in conn.execute(select(m.metadata.tables[name]).where(condition)).mappings()]
            return result[name]
        take("accounts",m.accounts.c.id==actor.account_id)
        take("users",m.users.c.account_id==actor.account_id)
        take("external_principals",m.external_principals.c.account_id==actor.account_id)
        take("companions",m.companions.c.id==companion_id)
        conversations=take("conversations",m.conversations.c.companion_id==companion_id)
        conversation_ids=[row["id"] for row in conversations]
        bindings=take("channel_bindings",m.channel_bindings.c.companion_id==companion_id)
        binding_ids=[row["id"] for row in bindings]
        requests=take("requests",m.requests.c.conversation_id.in_(conversation_ids))
        generations=take("generations",m.generations.c.request_id.in_([row["id"] for row in requests]))
        generation_ids=[row["id"] for row in generations]
        events=take("events",m.events.c.companion_id==companion_id)
        event_ids=[row["id"] for row in events]
        take("qq_message_receipts",m.qq_message_receipts.c.binding_id.in_(binding_ids))
        owned=list(conn.scalars(select(m.asset_owners.c.asset_id).where(m.asset_owners.c.account_id==actor.account_id)))
        take("assets",m.assets.c.id.in_(owned))
        take("asset_owners",m.asset_owners.c.account_id==actor.account_id)
        take("asset_refs",m.asset_refs.c.event_id.in_(event_ids))
        take("stickers",m.stickers.c.companion_id==companion_id)
        take("persona_versions",im.persona_versions.c.companion_id==companion_id)
        take("relationships",im.relationships.c.companion_id==companion_id)
        take("commitments",im.commitments.c.companion_id==companion_id)
        memories=take("memory_items",mm.memory_items.c.companion_id==companion_id)
        memory_ids=[row["id"] for row in memories]
        take("source_refs",mm.source_refs.c.memory_id.in_(memory_ids))
        take("memory_relations",mm.memory_relations.c.from_memory_id.in_(memory_ids))
        take("outbox",m.outbox.c.aggregate_id.in_(generation_ids+[companion_id]+event_ids+memory_ids))
        take("jobs",m.jobs.c.scope["companion_id"].astext==str(companion_id))
        for table in (dm.speech_segments,dm.avatar_cues,dm.playback_receipts): take(table.name,table.c.generation_id.in_(generation_ids))
        take("deletion_tombstones",deletion_tombstones.c.intent["companion_id"].astext==str(companion_id))
        take("quarantine",mm.quarantine.c.companion_id==companion_id)
        take("archive_origins",archive_origins.c.companion_id==companion_id)
        # Paid calls and old audio must never resume just because an archive was imported.
        for row in result["generations"]:
            if row["state"] in ("pending","running","draining"): row["state"]="interrupted"
            row["delivery_state"]="stopped"
        for row in result["outbox"]:
            if row["state"]!="completed": row["state"]="interrupted"
            row["lease_until"]=None
            row["worker_id"]=None
        return result

    async def export(self,actor,companion_id,destination):
        destination=Path(destination)
        destination.parent.mkdir(parents=True,exist_ok=True)
        staged=None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent,suffix=".tmp",delete=False) as temp: staged=Path(temp.name)
            with self.db.engine.begin() as conn:
                # Every source write uses the companion lock; keep it through immutable asset copy.
                lock(conn,companion_id)
                data=self.snapshot(conn,actor,companion_id)
                manifest={"archive_format_version":4,"schema_version":"0014_qq_channels","export_id":str(uuid4()),"scope":{"account_id":str(actor.account_id),"companion_id":str(companion_id)},"created_at":datetime.now(timezone.utc).isoformat(),"consistent_cut":"companion-write-barrier","files":{},"runtime_private_state_required":False,"indexes_included":False,"voice_training_material":"not_configured","secrets_included":False}
                with zipfile.ZipFile(staged,"w",compression=zipfile.ZIP_DEFLATED) as z:
                    for name in ARCHIVE_TABLE_ORDER:
                        rows=data.get(name,[])
                        raw=b"".join(json_bytes(row) for row in rows)
                        file=name+".ndjson"
                        z.writestr(file,raw)
                        manifest["files"][file]={"bytes":len(raw),"record_count":len(rows),"sha256":hashlib.sha256(raw).hexdigest()}
                    for asset in data["assets"]:
                        path=(self.asset_root/asset["relative_path"]).resolve()
                        if not path.is_relative_to(self.asset_root.resolve()) or not path.is_file(): raise AppError("ASSET_MISSING","归档所需媒体文件缺失",503)
                        raw=path.read_bytes()
                        if hashlib.sha256(raw).hexdigest()!=asset["sha256"]: raise AppError("ASSET_CORRUPT","媒体哈希不符",503)
                        file="assets/"+asset["relative_path"]
                        z.writestr(file,raw)
                        manifest["files"][file]={"bytes":len(raw),"sha256":asset["sha256"]}
                    z.writestr("manifest.json",json_bytes(manifest))
            with staged.open("r+b") as stream: os.fsync(stream.fileno())
            os.replace(staged,destination)
            return destination
        finally:
            if staged: staged.unlink(missing_ok=True)
