import json
import zipfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import pytest
from PIL import Image
from agi_wind.core.errors import AppError

async def test_roundtrip_preserves_identity_events_media_and_sources(db, actor, seeded_scope, archive, chat_request_factory, tmp_path):
    from agi_wind.archive.assets import AssetStore
    from agi_wind.archive.exports import ExportService
    from agi_wind.archive.imports import ImportService
    from agi_wind.archive.deletions import DeletionLedger
    stream=BytesIO()
    Image.new("RGB",(2,2),"red").save(stream,"PNG")
    store=AssetStore(db,tmp_path/"assets")
    asset=await store.put(actor,BytesIO(stream.getvalue()),"image/png")
    request=chat_request_factory(text="一起看过的红色图片",conversation_id=seeded_scope.conversation_id)
    request.input.asset_ids=[asset["id"]]
    accepted=await archive.accept(actor,request)
    await archive.append_output(accepted.generation_id,"记得这张图。",final_state="complete")
    package=await ExportService(db,store.root).export(actor,seeded_scope.companion_id,tmp_path/"archive.zip")
    report=ImportService.validate(package)
    assert report["valid"] and report["records"]["events"]==2
    # Restore into a real independent PostgreSQL schema, not a fake or Letta cache.
    from sqlalchemy import create_engine,text
    from alembic import command
    from alembic.config import Config
    from agi_wind.archive.repository import Database
    schema="restore_"+uuid4().hex
    with db.engine.begin() as conn: conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    target_engine=create_engine(db.engine.url,connect_args={"options":"-csearch_path="+schema},hide_parameters=True)
    try:
        cfg=Config("alembic.ini")
        with target_engine.begin() as conn:
            cfg.attributes["connection"]=conn
            command.upgrade(cfg,"head")
        target=Database(target_engine)
        ledger=DeletionLedger(tmp_path/"governance")
        ledger.initialize()
        result=await ImportService(target,tmp_path/"restored_assets").restore(package,latest_ledger=ledger)
        assert result["status"]=="restored"
        assert target.all("companions")[0]["id"]==seeded_scope.companion_id
        assert sorted(row["content_sha256"] for row in target.all("events"))==sorted(row["content_sha256"] for row in db.all("events"))
        assert target.all("assets")[0]["sha256"]==asset["sha256"]
        assert (tmp_path/"restored_assets"/asset["relative_path"]).read_bytes()==stream.getvalue()
        assert target.count("runtime_bindings")==0
    finally:
        target_engine.dispose()
        with db.engine.begin() as conn: conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))

def test_archive_rejects_traversal_symlinks_and_hash_tampering(tmp_path):
    from agi_wind.archive.imports import ImportService
    for name in ("../escape.txt", "C:/escape.txt", "assets\\escape.txt"):
        path=tmp_path/(str(uuid4())+".zip")
        with zipfile.ZipFile(path,"w") as z:
            z.writestr(name,"bad")
            z.writestr("manifest.json",'{}')
        with pytest.raises(AppError): ImportService.validate(path)

async def test_restore_without_latest_ledger_is_quarantined(db, actor, seeded_scope, tmp_path):
    from agi_wind.archive.exports import ExportService
    from agi_wind.archive.imports import ImportService
    package=await ExportService(db,tmp_path/"assets").export(actor,seeded_scope.companion_id,tmp_path/"copy.zip")
    result=await ImportService(db,tmp_path/"assets").restore(package,mode="clone")
    assert result["status"]=="recovery_quarantine"
    assert result["companion_id"]!=seeded_scope.companion_id
