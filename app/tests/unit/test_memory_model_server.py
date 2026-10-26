import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


def module():
    path = Path(__file__).resolve().parents[3] / "scripts/memory-server.py"
    assert path.is_file(), "local CPU model service is missing"
    spec = importlib.util.spec_from_file_location("memory_server", path)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


async def test_local_service_auth_origin_and_payload_boundaries():
    server = module()
    class Encoder:
        def embed(self, texts, batch_size=64):
            return iter([[1.0]+[0.0]*511 for _ in texts])
    app = server.create_app(Encoder(), {"fingerprint":"synthetic", "space":{"dimension":512}}, "a"*40)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8141") as client:
        assert (await client.get("/health")).status_code == 401
        auth = {"Authorization":"Bearer "+"a"*40}
        assert (await client.get("/health", headers={**auth,"Origin":"http://localhost"})).status_code == 403
        assert (await client.get("/health", headers=auth)).json()["space"]["dimension"] == 512
        assert (await client.post("/embed", headers=auth, json={"texts":["合成咖啡"], "fingerprint":"synthetic"})).status_code == 200
        for payload in ({"texts":["x"]*65}, {"texts":["x"*4097]}, {"texts":[" "]}, {"texts":["x"]*2,"query":True}):
            assert (await client.post("/embed", headers=auth, json=payload)).status_code == 422
        assert (await client.post("/embed", headers=auth, json={"texts":["x"],"fingerprint":"wrong"})).status_code == 409
        assert (await client.post("/embed", headers=auth, content=b"x"*180000)).status_code == 413
