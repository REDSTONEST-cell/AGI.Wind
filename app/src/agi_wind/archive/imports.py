from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
from uuid import UUID,uuid4
import zipfile
from sqlalchemy import DateTime,select,or_
from sqlalchemy.dialects.postgresql import UUID as UUIDType,insert
from . import models as m
from .exports import ARCHIVE_TABLE_ORDER
from .deletions import DeletionLedger
from .repository import lock
from ..core.errors import AppError

class ImportService:
    def __init__(self,db,asset_root): self.db,self.asset_root=db,Path(asset_root)

    @staticmethod
    def validate(path,max_bytes=1024**3):
        try:
            with zipfile.ZipFile(path) as z:
                names=z.namelist()
                if len(names)!=len(set(names)): raise ValueError("duplicate members")
                total=0
                for item in z.infolist():
                    name=item.filename
                    posix=PurePosixPath(name)
                    if posix.is_absolute() or ".." in posix.parts or "\\" in name or ":" in name or stat.S_ISLNK(item.external_attr>>16): raise ValueError("unsafe member")
                    total+=item.file_size
                    if total>max_bytes: raise ValueError("archive too large")
                if z.getinfo("manifest.json").file_size>2*1024*1024: raise ValueError("manifest too large")
                manifest=json.loads(z.read("manifest.json"))
                supported={(1,"0006_restore_origins"),(2,"0007_conversation_lifecycle"),(3,"0008_reasoning_stickers")}
                if (manifest.get("archive_format_version"),manifest.get("schema_version")) not in supported: raise ValueError("unsupported schema")
                expected=manifest["files"]
                if set(names)!=set(expected)|{"manifest.json"}: raise ValueError("unlisted member")
                records={}
                for name,entry in expected.items():
                    raw=z.read(name)
                    if len(raw)!=entry["bytes"] or hashlib.sha256(raw).hexdigest()!=entry["sha256"]: raise ValueError("file checksum mismatch")
                    if name.endswith(".ndjson"):
                        table=name.removesuffix(".ndjson")
                        if table not in ARCHIVE_TABLE_ORDER: raise ValueError("unexpected table")
                        rows=[json.loads(line) for line in raw.splitlines()]
                        if len(rows)!=entry["record_count"] or len(rows)>100000: raise ValueError("record count mismatch")
                        columns=set(m.metadata.tables[table].c.keys())
                        if any(set(row)-columns for row in rows): raise ValueError("unknown columns")
                        records[table]=len(rows)
                if manifest['archive_format_version'] < 3: records.setdefault('stickers', 0)
                if set(records)!=set(ARCHIVE_TABLE_ORDER): raise ValueError("missing authoritative table")
                return {"valid":True,"records":records,"manifest":manifest}
        except (OSError,ValueError,KeyError,TypeError,zipfile.BadZipFile):
            raise AppError("INVALID_ARCHIVE","归档路径、结构、计数或哈希校验失败",422) from None

    @staticmethod
    def sanitize(data,records):
        for intent in records:
            if intent["target_type"]!="event": continue
            target=set(intent["target_ids"])
            target.update(row["target_id"] for row in data["archive_origins"] if row["entity_table"]=="events" and row["origin_id"] in target)
            affected={row["generation_id"] for row in data["events"] if row["id"] in target and row.get("generation_id")}
            ids={row["id"] for row in data["events"] if row["id"] in target or row.get("generation_id") in affected}
            memory_ids={row["memory_id"] for row in data["source_refs"] if row["event_id"] in ids}
            data["events"]=[row for row in data["events"] if row["id"] not in ids]
            data["source_refs"]=[row for row in data["source_refs"] if row["memory_id"] not in memory_ids]
            data["memory_items"]=[row for row in data["memory_items"] if row["id"] not in memory_ids]
            data["memory_relations"]=[row for row in data["memory_relations"] if row["from_memory_id"] not in memory_ids and row["to_memory_id"] not in memory_ids]
            removed_assets={row["asset_id"] for row in data["asset_refs"] if row["event_id"] in ids}
            data["asset_refs"]=[row for row in data["asset_refs"] if row["event_id"] not in ids]
            data['stickers']=[row for row in data['stickers'] if row.get('source_event_id') not in ids or row['source']=='uploaded']
            still_referenced={row["asset_id"] for row in data["asset_refs"]} | {row['asset_id'] for row in data['stickers']}
            remove_assets=removed_assets-still_referenced
            data["assets"]=[row for row in data["assets"] if row["id"] not in remove_assets]
            data["asset_owners"]=[row for row in data["asset_owners"] if row["asset_id"] not in remove_assets]
            data["commitments"]=[row for row in data["commitments"] if row.get("source_event_id") not in ids]
            data["quarantine"]=[row for row in data["quarantine"] if row["companion_id"]!=intent["companion_id"]]
            for row in data["requests"]:
                if row["generation_id"] in affected: row.update(input={"text":"","asset_ids":[]},state="redacted")
            for row in data["generations"]:
                if row["id"] in affected: row.update(state="cancelled",delivery_state="cancelled",epoch=row["epoch"]+1)
            for row in data["speech_segments"]:
                if row.get("source_event_id") in ids or row["generation_id"] in affected: row.update(source_event_id=None,asset_id=None,display_text="",spoken_text="",state="cancelled")
        return data

    async def restore(self,path,mode="new_namespace",latest_ledger=None):
        if mode not in ("new_namespace","clone"): raise AppError("REPLACE_REQUIRES_RECOVERY_POINT","覆盖恢复尚未开放；使用空环境或 clone",409)
        report=self.validate(path)
        with zipfile.ZipFile(path) as z:
            data={name:[json.loads(line) for line in z.read(name+".ndjson").splitlines()] if name+'.ndjson' in z.namelist() else [] for name in ARCHIVE_TABLE_ORDER}
            records=latest_ledger.load() if latest_ledger else []
            data=self.sanitize(data,records)
            trusted=latest_ledger is not None and latest_ledger.path.is_file()
            original_companion=data["companions"][0]["id"]
            mapping={}
            if mode=="clone":
                # Remap entity identities, including references embedded in DTO JSON.
                for name,rows in data.items():
                    if name in ("accounts","users","assets","asset_owners","deletion_tombstones","archive_origins"): continue
                    for row in rows:
                        if "id" in row: mapping[row["id"]]=str(uuid4())
            def remap(value):
                if isinstance(value,str): return mapping.get(value,value)
                if isinstance(value,list): return [remap(item) for item in value]
                if isinstance(value,dict): return {key:(item if key in ("text","content","display_text","spoken_text","core","style","name","human_notes","origin_id") and not isinstance(item,list) else remap(item)) for key,item in value.items()}
                return value
            data=remap(data)
            companion_id=UUID(data["companions"][0]["id"])
            if mode=="clone":
                existing_origins={(row["entity_table"],row["target_id"]) for row in data["archive_origins"]}
                for name,rows in data.items():
                    if name in ("accounts","users","assets","asset_owners","deletion_tombstones","archive_origins"): continue
                    for row in rows:
                        if "id" in row and (name,row["id"]) not in existing_origins:
                            origin=next((old for old,new in mapping.items() if new==row["id"]),row["id"])
                            data["archive_origins"].append({"id":str(uuid4()),"companion_id":str(companion_id),"entity_table":name,"target_id":row["id"],"origin_id":origin})
                for row in data["outbox"]: row["dedupe_key"]="restored:"+str(companion_id)+":"+row["dedupe_key"]
            for row in data["companions"]:
                if not trusted: row["status"]="recovery_quarantine"
            with self.db.engine.begin() as conn:
                lock(conn,companion_id)
                if conn.execute(select(m.companions.c.id).where(m.companions.c.id==companion_id)).first(): raise AppError("ARCHIVE_CONFLICT","角色已存在，选择 clone 或空环境",409)
                for asset in sorted(data["assets"],key=lambda item:item["sha256"]):
                    lock(conn,"asset:"+asset["sha256"])
                    existing=conn.execute(select(m.assets).where(or_(m.assets.c.id==UUID(asset["id"]),m.assets.c.sha256==asset["sha256"]))).mappings().first()
                    if existing and any(str(existing[key])!=str(asset[key]) for key in ("id","sha256","mime","bytes","relative_path")):
                        raise AppError("ASSET_CONFLICT","媒体标识或内容发生冲突，未导入任何记录",409)
                for name in ARCHIVE_TABLE_ORDER:
                    table=m.metadata.tables[name]
                    for raw in data[name]:
                        row={}
                        for key,value in raw.items():
                            column=table.c[key]
                            if value is not None and isinstance(column.type,UUIDType): value=UUID(value)
                            elif value is not None and isinstance(column.type,DateTime): value=datetime.fromisoformat(value)
                            row[key]=value
                        stmt=insert(table).values(**row)
                        if name in ("accounts","users","assets","asset_owners","deletion_tombstones"): stmt=stmt.on_conflict_do_nothing()
                        conn.execute(stmt)
                for asset in data["assets"]:
                    relative=asset["relative_path"]
                    destination=(self.asset_root/relative).resolve()
                    if not destination.is_relative_to(self.asset_root.resolve()): raise AppError("INVALID_ARCHIVE","媒体路径越界",422)
                    raw=z.read("assets/"+relative)
                    if hashlib.sha256(raw).hexdigest()!=asset["sha256"]: raise AppError("INVALID_ARCHIVE","媒体哈希不一致",422)
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    temp=destination.with_suffix(".import-"+uuid4().hex)
                    with temp.open("wb") as stream:
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temp,destination)
            return {"status":"restored" if trusted else "recovery_quarantine","companion_id":companion_id,"origin_id":original_companion,"records":report["records"],"runtime_bindings_restored":False,"latest_deletions_applied":trusted}
