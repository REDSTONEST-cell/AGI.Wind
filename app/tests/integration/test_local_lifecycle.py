import asyncio
from uuid import uuid4
import httpx
from agi_wind.core.settings import Settings
from agi_wind.core.types import ModelRef

async def test_running_gateway_worker_completes_synthetic_http_request(db,actor,seeded_scope,tmp_path):
    from agi_wind.api.app import create_app
    from agi_wind.delivery.service import GenerationService
    from test_generation import FakeProvider
    provider=FakeProvider()
    from agi_wind.archive.deletions import DeletionLedger
    DeletionLedger(tmp_path/"governance").initialize()
    app=create_app(Settings(access_token="synthetic",data_dir=tmp_path),db,actor,GenerationService(db,provider,ModelRef(provider_id="fake",model_id="test")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            headers={"Authorization":"Bearer synthetic"}
            request={"request_id":str(uuid4()),"conversation_id":str(seeded_scope.conversation_id),"input":{"text":"本地生命周期测试"},"device_id":"test"}
            accepted=(await client.post("/api/v1/chat/requests",json=request,headers=headers)).json()
            for _ in range(100):
                status=(await client.get("/api/v1/generations/"+accepted["generation_id"],headers=headers)).json()
                if status["state"] in ("completed","failed"): break
                await asyncio.sleep(.02)
            assert status["state"]=="completed"
            events=await client.get(accepted["events_url"],headers=headers)
            assert "event: generation.completed" in events.text
            assert provider.calls==1
