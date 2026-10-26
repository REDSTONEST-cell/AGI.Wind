from uuid import uuid4
import httpx
from agi_wind.core.settings import Settings
from agi_wind.core.types import ModelRef

async def test_auth_validation_ready_and_durable_accept(db,actor,seeded_scope,tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.delivery.service import GenerationService
    from test_generation import FakeProvider
    service=GenerationService(db,FakeProvider(),model=ModelRef(provider_id="fake",model_id="test"))
    settings=Settings(access_token="synthetic-access-token",data_dir=tmp_path)
    app=create_app(settings,db=db,actor=actor,generation_service=service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code==200
        assert (await client.get("/api/v1/capabilities")).status_code==401
        headers={"Authorization":"Bearer synthetic-access-token"}
        capabilities=(await client.get("/api/v1/capabilities",headers=headers)).json()
        assert capabilities["protocol_version"]=="1.0" and capabilities["tts"] is False
        request={"request_id":str(uuid4()),"conversation_id":str(seeded_scope.conversation_id),"input":{"text":"HTTP合成测试"},"device_id":"test"}
        response=await client.post("/api/v1/chat/requests",headers=headers,json=request)
        assert response.status_code==202
        accepted=response.json()
        assert db.count("requests")==1
        duplicate=await client.post("/api/v1/chat/requests",headers=headers,json=request)
        assert duplicate.json()["generation_id"]==accepted["generation_id"]
        invalid=await client.post("/api/v1/chat/requests",headers=headers,json={**request,"access_token":"synthetic-access-token"})
        assert invalid.status_code==422 and "synthetic-access-token" not in invalid.text

async def test_unconfigured_model_is_explicit_not_fake_success(db,actor,seeded_scope,tmp_path):
    from agi_wind.api.app import create_app
    app=create_app(Settings(access_token="test",data_dir=tmp_path),db=db,actor=actor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        ready=await client.get("/health/ready")
        assert ready.status_code==503
        assert ready.json()["dependencies"]["dialogue"]=="unconfigured"


async def test_browser_home_opens_even_when_models_are_unconfigured(db,actor,seeded_scope,tmp_path):
    from agi_wind.api.app import create_app
    app=create_app(Settings(access_token="synthetic-private-token",data_dir=tmp_path),db=db,actor=actor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
        home=await client.get("/")
        assert home.status_code==200
        assert home.headers["content-type"].startswith("text/html")
        assert "AGI.Wind" in home.text
        assert "synthetic-private-token" not in home.text
        # The dependency status page moved to /status and must still render while models are unconfigured.
        status=await client.get("/status")
        assert status.status_code==200
        assert "本地服务" in status.text
        assert "synthetic-private-token" not in status.text
        # A successful browser landing must not disguise unavailable dependencies.
        ready=await client.get("/health/ready")
        assert ready.status_code==503
        assert ready.json()["dependencies"]["dialogue"]=="unconfigured"
