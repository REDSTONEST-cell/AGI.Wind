from uuid import uuid4
from io import BytesIO
from PIL import Image
import pytest
from agi_wind.core.errors import AppError

async def test_delete_removes_unshared_media_and_archived_stream_content(db,actor,seeded_scope,archive,chat_request_factory,tmp_path):
    from agi_wind.archive.assets import AssetStore
    from agi_wind.archive.deletions import DeletionService,DeletionLedger
    store=AssetStore(db,tmp_path/"assets")
    stream=BytesIO(); Image.new("RGB",(2,2),"blue").save(stream,"PNG")
    asset=await store.put(actor,BytesIO(stream.getvalue()),"image/png")
    request=chat_request_factory(conversation_id=seeded_scope.conversation_id)
    request.input.asset_ids=[asset["id"]]
    await archive.accept(actor,request)
    event=db.all("events")[0]
    await DeletionService(db,DeletionLedger(tmp_path/"ledger"),store.root).delete_event(actor,event["id"])
    assert db.count("assets")==0
    assert not (store.root/asset["relative_path"]).exists()

async def test_quarantined_restore_cannot_be_queried(db,actor,seeded_scope,tmp_path):
    from agi_wind.archive.exports import ExportService
    from agi_wind.archive.imports import ImportService
    from agi_wind.identity.service import IdentityService
    path=await ExportService(db,tmp_path/"assets").export(actor,seeded_scope.companion_id,tmp_path/"copy.zip")
    restored=await ImportService(db,tmp_path/"assets").restore(path,mode="clone")
    with pytest.raises(AppError) as error:
        await IdentityService(db).snapshot(actor,restored["companion_id"])
    assert error.value.code=="RECOVERY_QUARANTINED"

async def test_changed_output_reindex_does_not_keep_obsolete_partial_chunks(db,actor,seeded_scope,archive,chat_request_factory):
    from agi_wind.memory.retrieval import MemoryService
    accepted=await archive.accept(actor,chat_request_factory(conversation_id=seeded_scope.conversation_id))
    await archive.append_output(accepted.generation_id,"半句")
    memory=MemoryService(db)
    await memory.index_events(seeded_scope)
    await archive.append_output(accepted.generation_id,"完整回答。",final_state="complete")
    await memory.index_events(seeded_scope)
    chunks=db.all("retrieval_chunks")
    assert not any(chunk["content"]=="半句" for chunk in chunks)

async def test_corrected_active_memory_is_exposed_as_current(db,actor,seeded_scope,archive,chat_request_factory):
    from agi_wind.memory.models import memory_items,source_refs
    from agi_wind.memory.corrections import CorrectionService
    from agi_wind.memory.retrieval import MemoryService
    await archive.accept(actor,chat_request_factory(text="喜欢咖啡",conversation_id=seeded_scope.conversation_id))
    event=db.all("events")[0]
    memory_id=uuid4()
    with db.engine.begin() as conn:
        conn.execute(memory_items.insert().values(id=memory_id,account_id=actor.account_id,companion_id=seeded_scope.companion_id,user_id=actor.user_id,kind="preference",content="喜欢咖啡",epistemic_type="user_asserted"))
        conn.execute(source_refs.insert().values(memory_id=memory_id,event_id=event["id"],json_pointer="/text"))
    result=await CorrectionService(db).apply_correction(actor,memory_id,"现在不喝咖啡",event["id"])
    hits=await MemoryService(db).search(seeded_scope,"咖啡")
    assert hits[0].memory_id==result["memory_id"]
    assert hits[0].content=="现在不喝咖啡"
