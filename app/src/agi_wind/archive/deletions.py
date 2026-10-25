"""Content-free, fsynced deletion intent ledger independent of database restores."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4
from sqlalchemy import Table, Column, Text, Integer, select, or_, delete
from sqlalchemy.dialects.postgresql import insert
from . import models as m
from .repository import lock, digest, authorized_conversation
from ..memory import models as mm
from ..identity import models as im
from ..identity.service import authorize
from ..delivery import models as dm
from ..core.errors import AppError

deletion_tombstones=Table("deletion_tombstones",m.metadata,m.idcol(),Column("ledger_sequence",Integer,nullable=False),m.js("intent"),Column("ledger_hash",Text,nullable=False))

def canonical(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))

@contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        if stream.read(1)==b"": stream.write(b"0"); stream.flush()
        stream.seek(0)
        if os.name=="nt":
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_LOCK,1)
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX)
        try: yield
        finally:
            stream.seek(0)
            if os.name=="nt": msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(stream,fcntl.LOCK_UN)

class DeletionLedger:
    def __init__(self,root,control_dir=None):
        self.root=Path(root)
        self.path=self.root/"deletions.ndjson"
        self.control_dir=Path(control_dir) if control_dir else None

    @staticmethod
    def read(path):
        path=Path(path)
        if not path.exists(): return []
        data=path.read_bytes()
        if data and not data.endswith(b"\n"): raise AppError("LEDGER_CORRUPT","删除账本不完整",503)
        records,previous=[],"0"*64
        try:
            for line in data.splitlines():
                record=json.loads(line)
                sha=record.pop("hash")
                if record["sequence"]!=len(records)+1 or record["previous_hash"]!=previous or hashlib.sha256(canonical(record).encode()).hexdigest()!=sha:
                    raise ValueError("broken chain")
                record["hash"]=sha
                records.append(record)
                previous=sha
        except (ValueError,KeyError,TypeError): raise AppError("LEDGER_CORRUPT","删除账本校验失败",503) from None
        return records

    def initialize(self):
        self.root.mkdir(parents=True,exist_ok=True)
        with file_lock(self.root/".ledger.lock"):
            if not self.path.exists():
                with self.path.open("xb") as stream: stream.flush();os.fsync(stream.fileno())
            self.read(self.path)

    def load(self,require_exists=False):
        with file_lock(self.root/".ledger.lock"):
            if require_exists and not self.path.is_file():raise AppError("LATEST_LEDGER_MISSING","缺少当前删除账本，拒绝开放角色",503)
            return self.read(self.path)

    @staticmethod
    def write_line(path,record):
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open("ab") as stream:
            stream.write((canonical(record)+"\n").encode())
            stream.flush()
            os.fsync(stream.fileno())

    def append(self,intent):
        allowed={"account_id","companion_id","target_type","target_ids","policy"}
        if set(intent)-allowed or intent.get("target_type")!="event": raise AppError("INVALID_DELETION","删除意图字段无效",422)
        with file_lock(self.root/".ledger.lock"):
            records=self.read(self.path)
            record={**intent,"deletion_id":str(uuid4()),"sequence":len(records)+1,"previous_hash":records[-1]["hash"] if records else "0"*64,"deleted_at":datetime.now(timezone.utc).isoformat()}
            record["hash"]=hashlib.sha256(canonical(record).encode()).hexdigest()
            self.write_line(self.path,record)
            synced=False
            if self.control_dir:
                mirror=self.control_dir/"deletions.ndjson"
                with file_lock(self.control_dir/".ledger.lock"):
                    older=self.read(mirror)
                    if older!=records[:len(older)] or len(older)>len(records): raise AppError("LEDGER_DIVERGED","独立账本副本不一致",503)
                    for missing in records[len(older):]+[record]: self.write_line(mirror,missing)
                synced=True
            return {**record,"ledger_synced":synced}

    @classmethod
    def load_latest(cls,control_dir): return cls.read(Path(control_dir)/"deletions.ndjson")


class DeletionService:
    def __init__(self,db,ledger,asset_root): self.db,self.ledger,self.asset_root=db,ledger,Path(asset_root)

    async def delete_event(self,actor,event_id):
        with self.db.engine.begin() as conn:
            event=conn.execute(select(m.events).where(m.events.c.id==event_id)).mappings().first()
            if not event: raise AppError("NOT_FOUND","事件不存在",404)
            lock(conn,event["companion_id"])
            authorize(conn,actor,event["companion_id"])
            intent=self.ledger.append({"account_id":str(actor.account_id),"companion_id":str(event["companion_id"]),"target_type":"event","target_ids":[str(event_id)],"policy":"permanent"})
            self.apply(conn,intent)
        self.collect_garbage()
        return {"deletion_id":intent["deletion_id"],"ledger_synced":intent["ledger_synced"],"external_copies":"invalidated_pending_physical_purge"}

    async def delete_conversation(self,actor,conversation_id):
        with self.db.engine.begin() as conn:
            conversation=authorized_conversation(conn,actor,conversation_id)
            companion_id=conversation["companion_id"]
            lock(conn,companion_id)
            generation_ids=list(conn.scalars(select(m.generations.c.id).join(m.requests,m.generations.c.request_id==m.requests.c.id).where(m.requests.c.conversation_id==conversation_id)))
            if conn.execute(select(m.generations.c.id).where(m.generations.c.id.in_(generation_ids),m.generations.c.state.in_(["pending","running","draining"]))).first():
                raise AppError("GENERATION_IN_PROGRESS","这段对话仍在生成，请先停止或等待完成",409)
            event_ids=list(conn.scalars(select(m.events.c.id).where(m.events.c.conversation_id==conversation_id)))
            intent=None
            if event_ids:
                intent=self.ledger.append({"account_id":str(actor.account_id),"companion_id":str(companion_id),"target_type":"event","target_ids":[str(key) for key in event_ids],"policy":"permanent"})
                self.apply(conn,intent)
            conn.execute(dm.playback_receipts.delete().where(dm.playback_receipts.c.generation_id.in_(generation_ids)))
            conn.execute(dm.avatar_cues.delete().where(dm.avatar_cues.c.generation_id.in_(generation_ids)))
            conn.execute(dm.speech_segments.delete().where(dm.speech_segments.c.generation_id.in_(generation_ids)))
            conn.execute(dm.delivery_commands.delete().where(dm.delivery_commands.c.generation_id.in_(generation_ids)))
            conn.execute(dm.generation_stream_events.delete().where(dm.generation_stream_events.c.generation_id.in_(generation_ids)))
            conn.execute(dm.conversation_live_events.delete().where(dm.conversation_live_events.c.conversation_id==conversation_id))
            if generation_ids:
                conn.execute(m.outbox.delete().where(m.outbox.c.event_type.in_(["generation.requested","memory.extract"]),or_(m.outbox.c.payload["generation_id"].astext.in_([str(key) for key in generation_ids]),m.outbox.c.payload["conversation_id"].astext==str(conversation_id))))
                conn.execute(m.generations.delete().where(m.generations.c.id.in_(generation_ids)))
            conn.execute(m.requests.delete().where(m.requests.c.conversation_id==conversation_id))
            conn.execute(m.conversations.delete().where(m.conversations.c.id==conversation_id))
        self.collect_garbage()
        return {"deleted":str(conversation_id),"deleted_events":len(event_ids),"deletion_id":intent["deletion_id"] if intent else None,"ledger_synced":intent["ledger_synced"] if intent else None}

    @staticmethod
    def apply(conn,intent):
        if conn.execute(select(deletion_tombstones.c.id).where(deletion_tombstones.c.id==UUID(intent["deletion_id"]))).first(): return
        target_ids=[UUID(key) for key in intent["target_ids"]]
        companion_id=UUID(intent["companion_id"])
        lock(conn,companion_id)
        rows=list(conn.execute(select(m.events).where(m.events.c.id.in_(target_ids),m.events.c.companion_id==companion_id)).mappings())
        generation_ids={row["generation_id"] for row in rows if row["generation_id"]}
        if generation_ids:
            rows=list(conn.execute(select(m.events).where(or_(m.events.c.id.in_(target_ids),m.events.c.generation_id.in_(generation_ids)))).mappings())
        ids=[row["id"] for row in rows]
        media_ids=set(conn.scalars(select(m.asset_refs.c.asset_id).where(m.asset_refs.c.event_id.in_(ids))))
        media_ids.update(key for key in conn.scalars(select(dm.speech_segments.c.asset_id).where(dm.speech_segments.c.generation_id.in_(generation_ids))) if key)
        memory_ids=list(conn.scalars(select(mm.source_refs.c.memory_id).where(mm.source_refs.c.event_id.in_(ids))))
        conn.execute(mm.memory_relations.delete().where(or_(mm.memory_relations.c.from_memory_id.in_(memory_ids),mm.memory_relations.c.to_memory_id.in_(memory_ids))))
        conn.execute(mm.source_refs.delete().where(mm.source_refs.c.memory_id.in_(memory_ids)))
        conn.execute(mm.memory_items.delete().where(mm.memory_items.c.id.in_(memory_ids)))
        conn.execute(mm.retrieval_chunks.delete().where(mm.retrieval_chunks.c.source_id.in_(ids)))
        conn.execute(mm.quarantine.delete().where(mm.quarantine.c.companion_id==companion_id))
        conn.execute(im.commitments.delete().where(im.commitments.c.source_event_id.in_(ids)))
        conn.execute(m.asset_refs.delete().where(m.asset_refs.c.event_id.in_(ids)))
        conn.execute(m.stickers.delete().where(m.stickers.c.source_event_id.in_(ids), m.stickers.c.source=='captured'))
        conn.execute(dm.speech_segments.update().where(or_(dm.speech_segments.c.source_event_id.in_(ids),dm.speech_segments.c.generation_id.in_(generation_ids))).values(source_event_id=None,asset_id=None,display_text="",spoken_text="",state="cancelled"))
        conn.execute(dm.avatar_cues.delete().where(dm.avatar_cues.c.generation_id.in_(generation_ids)))
        conn.execute(dm.generation_stream_events.delete().where(dm.generation_stream_events.c.generation_id.in_(generation_ids)))
        conn.execute(m.requests.update().where(m.requests.c.generation_id.in_(generation_ids)).values(input={"text":"","asset_ids":[]},state="redacted"))
        conn.execute(m.generations.update().where(m.generations.c.id.in_(generation_ids)).values(state="cancelled",delivery_state="cancelled",epoch=m.generations.c.epoch+1))
        conn.execute(m.events.delete().where(m.events.c.id.in_(ids)))
        for asset_id in media_ids:
            remaining=conn.execute(select(m.asset_refs).where(m.asset_refs.c.asset_id==asset_id)).first()
            if conn.execute(select(m.stickers.c.id).where(m.stickers.c.asset_id==asset_id)).first(): continue
            if remaining: continue
            asset=conn.execute(select(m.assets).where(m.assets.c.id==asset_id)).mappings().first()
            if not asset: continue
            lock(conn,"asset:"+asset["sha256"])
            conn.execute(m.asset_owners.delete().where(m.asset_owners.c.asset_id==asset_id,m.asset_owners.c.account_id==UUID(intent["account_id"])))
            if conn.execute(select(m.asset_owners).where(m.asset_owners.c.asset_id==asset_id)).first(): continue
            if conn.execute(select(dm.speech_segments.c.id).where(dm.speech_segments.c.asset_id==asset_id)).first(): continue
            conn.execute(m.assets.delete().where(m.assets.c.id==asset_id))
            conn.execute(insert(m.outbox).values(event_type="asset.gc",aggregate_id=asset_id,payload={"relative_path":asset["relative_path"],"sha256":asset["sha256"]},dedupe_key="asset-gc:"+str(asset_id)).on_conflict_do_nothing())
        conn.execute(m.companions.update().where(m.companions.c.id==companion_id).values(memory_revision=m.companions.c.memory_revision+1))
        conn.execute(im.runtime_bindings.update().where(im.runtime_bindings.c.companion_id==companion_id).values(status="stale"))
        conn.execute(insert(deletion_tombstones).values(id=UUID(intent["deletion_id"]),ledger_sequence=intent["sequence"],intent={key:value for key,value in intent.items() if key!="ledger_synced"},ledger_hash=intent["hash"]).on_conflict_do_nothing())

    def replay(self):
        for intent in self.ledger.load(require_exists=True):
            with self.db.engine.begin() as conn: self.apply(conn,intent)
        self.collect_garbage()

    def collect_garbage(self):
        with self.db.engine.begin() as conn:
            jobs=list(conn.execute(select(m.outbox).where(m.outbox.c.event_type=="asset.gc",m.outbox.c.state!="completed").with_for_update()).mappings())
            for job in jobs:
                sha=job["payload"]["sha256"]
                lock(conn,"asset:"+sha)
                if not conn.execute(select(m.assets.c.id).where(m.assets.c.sha256==sha)).first():
                    path=(self.asset_root/job["payload"]["relative_path"]).resolve()
                    if not path.is_relative_to(self.asset_root.resolve()) or path.stem!=sha:
                        raise AppError("INVALID_GC_PATH","媒体清理路径校验失败",503)
                    path.unlink(missing_ok=True)
                conn.execute(m.outbox.update().where(m.outbox.c.id==job["id"]).values(state="completed",lease_until=None))
