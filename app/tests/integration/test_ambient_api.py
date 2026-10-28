from datetime import datetime, timezone

import httpx

from nagi.ambient.models import ambient_observations
from nagi.api.app import create_app
from nagi.core.settings import Settings
from test_client import connect


async def test_ambient_api_status_settings_and_redacted_observation(db, actor, tmp_path):
    settings = Settings(
        access_token="synthetic-private-token",
        data_dir=tmp_path,
        ambient={"enabled": False, "capture_screenshots": False},
    )
    app = create_app(settings, db, actor)
    db.seed(actor)
    with db.engine.begin() as conn:
        conn.execute(ambient_observations.insert().values(
            account_id=actor.account_id,
            captured_at=datetime.now(timezone.utc),
            window_title="main.py - Visual Studio Code",
            process_name="Code.exe",
            idle_seconds=0,
            sensitive=False,
            screenshot_path="ambient/screenshots/20261027/120000-secret.jpg",
        ))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        _, csrf = await connect(client)
        headers = {"Origin": "http://test", "X-AGI-CSRF": csrf}

        status = await client.get("/api/v1/ambient/status")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["settings"]["enabled"] is False
        assert body["observations"][0]["screenshot_url"].endswith("/screenshot")
        assert "screenshot_path" not in body["observations"][0]

        listed = await client.get("/api/v1/ambient/observations", params={"limit": 10})
        assert listed.status_code == 200
        assert listed.json()[0]["process_name"] == "Code.exe"

        patched = await client.patch(
            "/api/v1/ambient/settings",
            json={"sample_seconds": 30, "retention_hours": 6},
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["settings"]["sample_seconds"] == 30

        invalid = await client.patch(
            "/api/v1/ambient/settings",
            json={"sample_seconds": 5},
            headers=headers,
        )
        assert invalid.status_code == 422

        missing = await client.get("/api/v1/ambient/observations/00000000-0000-0000-0000-000000000000/screenshot")
        assert missing.status_code == 404
