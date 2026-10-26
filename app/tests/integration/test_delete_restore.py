import json
from sqlalchemy import select
import pytest

async def test_latest_ledger_prevents_old_backup_and_clone_resurrection(db,actor,seeded_scope,archive,chat_request_factory,tmp_path):
    from agi_wind.archive.exports import ExportService
    from agi_wind.archive.imports import ImportService
    from agi_wind.archive.deletions import DeletionLedger, DeletionService
    from agi_wind.memory.retrieval import MemoryService
    await archive.accept(actor,chat_request_factory(text="待永久删除的合成经历",conversation_id=seeded_scope.conversation_id))
    event=db.all("events")[0]
    await MemoryService(db).index_events(seeded_scope)
    package=await ExportService(db,tmp_path/"assets").export(actor,seeded_scope.companion_id,tmp_path/"old.zip")
    ledger=DeletionLedger(tmp_path/"governance",control_dir=tmp_path/"independent-control")
    result=await DeletionService(db,ledger,tmp_path/"assets").delete_event(actor,event["id"])
    assert result["ledger_synced"] is True
    assert db.count("events")==0
    assert db.all("requests")[0]["input"]["text"]==""
    assert "待永久删除的合成经历" not in ledger.path.read_text(encoding="utf-8")
    restored=await ImportService(db,tmp_path/"assets").restore(package,mode="clone",latest_ledger=ledger)
    assert restored["status"]=="restored"
    assert db.count("events")==0
    assert db.count("retrieval_chunks")==0
    assert ledger.load()[0]["target_ids"]==[str(event["id"])]

async def test_delete_conversation_removes_events_requests_and_conversation(db,actor,seeded_scope,archive,chat_request_factory,tmp_path):
    from agi_wind.archive.deletions import DeletionLedger, DeletionService
    from agi_wind.core.errors import AppError
    from sqlalchemy import text
    await archive.accept(actor,chat_request_factory(text="待整段删除的合成对话",conversation_id=seeded_scope.conversation_id))
    ledger=DeletionLedger(tmp_path/"governance"); ledger.initialize()
    service=DeletionService(db,ledger,tmp_path/"assets")
    with pytest.raises(AppError) as pending:
        await service.delete_conversation(actor,seeded_scope.conversation_id)
    assert pending.value.code=="GENERATION_IN_PROGRESS"
    with db.engine.begin() as conn:
        conn.execute(text("UPDATE generations SET state='completed' WHERE request_id IN (SELECT id FROM requests WHERE conversation_id=:conversation)"),{"conversation":seeded_scope.conversation_id})
    result=await service.delete_conversation(actor,seeded_scope.conversation_id)
    assert result["deleted_events"]==1
    assert result["deletion_id"]
    for table in ("events","requests","generations","conversations"):
        assert db.count(table)==0
    assert ledger.load()[0]["target_ids"]

def test_ledger_detects_truncation_and_hash_chain_corruption(tmp_path):
    from agi_wind.archive.deletions import DeletionLedger
    from agi_wind.core.errors import AppError
    from uuid import uuid4
    ledger=DeletionLedger(tmp_path)
    ledger.append({"account_id":str(uuid4()),"companion_id":str(uuid4()),"target_type":"event","target_ids":[str(uuid4())]})
    line=json.loads(ledger.path.read_text(encoding="utf-8"))
    line["target_ids"]=[str(uuid4())]
    ledger.path.write_text(json.dumps(line)+"\n",encoding="utf-8")
    with pytest.raises(AppError): ledger.load()
