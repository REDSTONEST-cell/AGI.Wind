import httpx

from test_client import client_app

BEARER = {"Authorization": "Bearer synthetic-private-token"}


async def test_system_logs_tail_redact_and_incremental_offsets(db, actor, seeded_scope, tmp_path):
    stderr = tmp_path / "gateway.stderr.log"
    stdout = tmp_path / "gateway.stdout.log"
    stderr.write_text(
        "INFO:     Started server process [1]\n"
        "ERROR:    boom synthetic-private-token\n"
        "password=supersecret value\n",
        encoding="utf-8",
    )
    stdout.write_text("stdout: application starting\n", encoding="utf-8")
    app = client_app(db, actor, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/system-logs")).status_code == 401
        first = await client.get("/api/v1/system-logs", headers=BEARER)
        assert first.status_code == 200, first.text
        body = first.json()
        texts = [line["text"] for line in body["lines"]]
        assert any("Started server process" in line for line in texts)
        assert any("stdout: application starting" in line for line in texts)
        assert "synthetic-private-token" not in first.text
        assert "supersecret" not in first.text
        offsets = body["offsets"]
        assert offsets["stderr"] > 0 and offsets["stdout"] > 0

        with stderr.open("a", encoding="utf-8") as stream:
            stream.write("INFO:     new line only\n")
        second = await client.get(
            "/api/v1/system-logs",
            headers=BEARER,
            params={"stdout_offset": offsets["stdout"], "stderr_offset": offsets["stderr"]},
        )
        new_texts = [line["text"] for line in second.json()["lines"]]
        assert any("new line only" in line for line in new_texts)
        assert not any("Started server process" in line for line in new_texts)
        assert second.json()["offsets"]["stderr"] > offsets["stderr"]

        # A truncated/rotated file resets the cursor instead of silently losing content.
        stderr.write_text("INFO: replaced\n", encoding="utf-8")
        reset = await client.get(
            "/api/v1/system-logs",
            headers=BEARER,
            params={"stdout_offset": second.json()["offsets"]["stdout"], "stderr_offset": second.json()["offsets"]["stderr"]},
        )
        assert reset.status_code == 200
        assert reset.json()["reset"] is True
        assert any("replaced" in line["text"] for line in reset.json()["lines"])
