from io import BytesIO
from uuid import uuid4
from PIL import Image
import pytest
from agi_wind.core.types import ModelRef
from agi_wind.core.errors import AppError

async def test_switch_to_other_provider_never_leaks_context_to_old_endpoint(db,actor,seeded_scope,chat_request_factory):
    from agi_wind.delivery.service import GenerationService
    from agi_wind.identity.service import IdentityService
    from test_generation import FakeProvider
    old=FakeProvider()
    service=GenerationService(db,old,ModelRef(provider_id="vendor-a",model_id="a"))
    await IdentityService(db).bind_model(actor,seeded_scope.companion_id,ModelRef(provider_id="vendor-b",model_id="b"))
    accepted=await service.submit(actor,chat_request_factory(conversation_id=seeded_scope.conversation_id))
    await service.run(accepted.generation_id)
    assert old.calls==0
    assert db.all("generations")[0]["error_code"]=="PROVIDER_NOT_CONFIGURED"

async def test_provider_resolver_selects_correct_endpoint(db,actor,seeded_scope,chat_request_factory):
    from agi_wind.delivery.service import GenerationService
    from agi_wind.identity.service import IdentityService
    from test_generation import FakeProvider
    first,second=FakeProvider(),FakeProvider()
    service=GenerationService(db,first,ModelRef(provider_id="vendor-a",model_id="a"),provider_resolver=lambda model:{"vendor-a":first,"vendor-b":second}[model.provider_id])
    await IdentityService(db).bind_model(actor,seeded_scope.companion_id,ModelRef(provider_id="vendor-b",model_id="b"))
    accepted=await service.submit(actor,chat_request_factory(conversation_id=seeded_scope.conversation_id))
    assert await service.run(accepted.generation_id)
    assert first.calls==0 and second.calls==1

async def test_image_limit_is_independent_of_audio_limit(db,actor,seeded_scope,tmp_path):
    from agi_wind.archive.assets import AssetStore
    store=AssetStore(db,tmp_path,max_bytes=20*1024*1024)
    with pytest.raises(AppError) as error: await store.put(actor,BytesIO(b"x"*(10*1024*1024+1)),"image/png")
    assert error.value.status==413

async def test_missing_claimed_latest_ledger_never_promotes_restore(db,actor,seeded_scope,tmp_path):
    from agi_wind.archive.exports import ExportService
    from agi_wind.archive.imports import ImportService
    from agi_wind.archive.deletions import DeletionLedger
    path=await ExportService(db,tmp_path/"assets").export(actor,seeded_scope.companion_id,tmp_path/"copy.zip")
    report=await ImportService(db,tmp_path/"assets").restore(path,mode="clone",latest_ledger=DeletionLedger(tmp_path/"missing-current-ledger"))
    assert report["status"]=="recovery_quarantine"

async def test_asset_id_collision_during_restore_fails_atomically(db,actor,seeded_scope,tmp_path):
    from agi_wind.archive.assets import AssetStore
    from agi_wind.archive.exports import ExportService
    from agi_wind.archive.imports import ImportService
    from agi_wind.archive.models import assets
    stream=BytesIO();Image.new("RGB",(2,2),"red").save(stream,"PNG")
    store=AssetStore(db,tmp_path/"assets")
    asset=await store.put(actor,BytesIO(stream.getvalue()),"image/png")
    path=await ExportService(db,store.root).export(actor,seeded_scope.companion_id,tmp_path/"copy.zip")
    with db.engine.begin() as conn:conn.execute(assets.update().where(assets.c.id==asset["id"]).values(mime="image/jpeg"))
    with pytest.raises(AppError) as error:await ImportService(db,store.root).restore(path,mode="clone")
    assert error.value.code=="ASSET_CONFLICT"
    assert db.count("companions")==1

async def test_http_database_failure_is_redacted_without_leaking_exception(db,actor,seeded_scope,tmp_path):
    import httpx
    from sqlalchemy import text
    from agi_wind.api.app import create_app
    from agi_wind.core.settings import Settings
    app=create_app(Settings(access_token="synthetic",data_dir=tmp_path),db,actor)
    with db.engine.begin() as conn:conn.execute(text("ALTER TABLE outbox ADD CONSTRAINT fail_http CHECK(false)"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        response=await client.post("/api/v1/chat/requests",headers={"Authorization":"Bearer synthetic"},json={"request_id":str(uuid4()),"conversation_id":str(seeded_scope.conversation_id),"input":{"text":"synthetic-private-input"},"device_id":"test"})
    assert response.status_code==500
    assert "synthetic-private-input" not in response.text
    assert db.count("requests")==0
