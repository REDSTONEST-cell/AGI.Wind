import httpx

from agi_wind.api.app import create_app
from agi_wind.core.settings import Settings
from test_client import client_app


async def test_client_key_is_a_bearer_credential_and_can_open_cookie_session(db, actor, seeded_scope, tmp_path):
    app = client_app(db, actor, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/companions", headers={"Authorization": "Bearer 123456"})).status_code == 200
        assert (await client.get("/api/v1/companions", headers={"Authorization": "Bearer wrong-key"})).status_code == 401
        wrong_origin = await client.post("/api/v1/client/key-session", headers={"Origin": "https://untrusted.invalid"}, json={"key": "123456"})
        assert wrong_origin.status_code == 403 and "set-cookie" not in wrong_origin.headers
        wrong_key = await client.post("/api/v1/client/key-session", headers={"Origin": "http://test"}, json={"key": "wrong-key"})
        assert wrong_key.status_code == 401
        good = await client.post("/api/v1/client/key-session", headers={"Origin": "http://test"}, json={"key": "123456"})
        assert good.status_code == 200 and good.json()["csrf_token"]
        assert "httponly" in good.headers["set-cookie"].lower()
        assert (await client.get("/api/v1/client/session")).status_code == 200
        assert (await client.get("/api/v1/companions")).status_code == 200


async def test_client_key_session_rate_limits_repeated_failures(db, actor, seeded_scope, tmp_path):
    app = client_app(db, actor, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(5):
            attempt = await client.post("/api/v1/client/key-session", headers={"Origin": "http://test"}, json={"key": "nope"})
            assert attempt.status_code == 401
        limited = await client.post("/api/v1/client/key-session", headers={"Origin": "http://test"}, json={"key": "nope"})
        assert limited.status_code == 429
        # Correct keys are also refused while the per-IP window is saturated.
        assert (await client.post("/api/v1/client/key-session", headers={"Origin": "http://test"}, json={"key": "123456"})).status_code == 429


async def test_cors_only_allows_configured_standalone_client_origins(db, actor, seeded_scope, tmp_path):
    settings = Settings(access_token="synthetic-private-token", data_dir=tmp_path, allowed_origins=["https://client.example"])
    app = create_app(settings, db, actor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        allowed = await client.options(
            "/api/v1/companions",
            headers={"Origin": "https://client.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
        )
        assert allowed.status_code == 200
        assert allowed.headers.get("access-control-allow-origin") == "https://client.example"
        # Actual cross-origin Bearer call succeeds through the middleware.
        result = await client.get("/api/v1/companions", headers={"Origin": "https://client.example", "Authorization": "Bearer 123456"})
        assert result.status_code == 200
        assert result.headers.get("access-control-allow-origin") == "https://client.example"
        denied = await client.options(
            "/api/v1/companions",
            headers={"Origin": "https://other.example", "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
        )
        assert "access-control-allow-origin" not in denied.headers
