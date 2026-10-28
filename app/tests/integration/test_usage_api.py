from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from test_client import client_app

from nagi.archive import models as m
from nagi.archive.repository import digest
from nagi.delivery import models as dm
from nagi.usage import estimate_tokens

BEARER = {"Authorization": "Bearer synthetic-private-token"}


def seed_usage_rows(db, actor, seeded_scope):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)
    request_id, generation_id = uuid4(), uuid4()
    with db.engine.begin() as conn:
        conn.execute(
            m.events.insert().values(
                account_id=actor.account_id,
                companion_id=seeded_scope.companion_id,
                conversation_id=seeded_scope.conversation_id,
                role="user",
                kind="user_input",
                content={"text": "你好世界你好世界"},
                content_sha256=digest({"text": "你好世界你好世界"}),
                occurred_at=now,
                recorded_at=now,
            )
        )
        conn.execute(
            m.events.insert().values(
                account_id=actor.account_id,
                companion_id=seeded_scope.companion_id,
                conversation_id=seeded_scope.conversation_id,
                role="assistant",
                kind="assistant_output",
                content={"text": "hello world test", "state": "complete"},
                content_sha256=digest({"text": "hello world test"}),
                occurred_at=now,
                recorded_at=now,
            )
        )
        # An older message only shows up in the lifetime totals.
        conn.execute(
            m.events.insert().values(
                account_id=actor.account_id,
                companion_id=seeded_scope.companion_id,
                conversation_id=seeded_scope.conversation_id,
                role="user",
                kind="user_input",
                content={"text": "旧消息"},
                content_sha256=digest({"text": "旧消息"}),
                occurred_at=old,
                recorded_at=old,
            )
        )
        conn.execute(
            m.requests.insert().values(
                id=request_id,
                account_id=actor.account_id,
                conversation_id=seeded_scope.conversation_id,
                input_hash="synthetic-usage",
                input={"text": "你好世界你好世界"},
                output={},
                device_id="usage-test",
                state="accepted",
                generation_id=generation_id,
            )
        )
        conn.execute(
            m.generations.insert().values(
                id=generation_id,
                request_id=request_id,
                state="completed",
                created_at=now - timedelta(seconds=10),
                finished_at=now - timedelta(seconds=5),
            )
        )
        conn.execute(
            dm.speech_segments.insert().values(
                generation_id=generation_id,
                epoch=1,
                segment_index=0,
                display_text="你好世界",
                spoken_text="你好世界",
                voice_id="yuwan",
                voice_version="1",
                state="ready",
            )
        )
        conn.execute(
            dm.generation_stream_events.insert().values(
                generation_id=generation_id,
                sequence=1,
                type="tool.completed",
                epoch=1,
                payload={"tool": "web_search", "stage": "agent", "status": "completed"},
            )
        )
        # Non-agent trace steps (vision/reply) must not be counted as tool calls.
        conn.execute(
            dm.generation_stream_events.insert().values(
                generation_id=generation_id,
                sequence=2,
                type="tool.completed",
                epoch=1,
                payload={"tool": "vision", "stage": "vision", "status": "completed"},
            )
        )
        conn.execute(
            dm.generation_stream_events.insert().values(
                generation_id=generation_id,
                sequence=3,
                type="text.delta",
                epoch=1,
                payload={"text": "hello"},
                created_at=now - timedelta(seconds=8),
            )
        )
    return now


async def test_usage_stats_requires_auth(db, actor, seeded_scope, tmp_path):
    app = client_app(db, actor, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/usage/stats")).status_code == 401


async def test_usage_stats_aggregates_messages_tokens_voice_and_tools(db, actor, seeded_scope, tmp_path):
    seed_usage_rows(db, actor, seeded_scope)
    app = client_app(db, actor, tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/usage/stats", headers=BEARER)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["messages"] == {"total": 3, "today": 2}
    assert body["model_calls"] == {"total": 1, "today": 1}

    user_tokens = estimate_tokens("你好世界你好世界")
    assistant_tokens = estimate_tokens("hello world test")
    assert body["tokens"]["input"] == user_tokens + estimate_tokens("旧消息")
    assert body["tokens"]["output"] == assistant_tokens
    assert body["tokens"]["total"] == body["tokens"]["input"] + body["tokens"]["output"]
    assert body["tokens"]["today_input"] == user_tokens
    assert body["tokens"]["today_output"] == assistant_tokens
    assert body["tokens"]["estimated"] is True

    assert body["voice"] == {"segments": 1}
    assert body["tools"]["calls"] == 1
    assert body["tools"]["ranking"] == [{"name": "web_search", "calls": 1}]

    series = body["series"]
    assert series["window_hours"] == 24
    points = series["points"]
    assert len(points) == 24
    assert sum(point["messages"] for point in points) == 2
    assert sum(point["model_calls"] for point in points) == 1
    assert sum(point["output_tokens"] for point in points) == assistant_tokens
    assert points == sorted(points, key=lambda point: point["at"])

    assert len(body["models"]) == 1
    model = body["models"][0]
    assert model["calls"] == 1
    assert model["tokens"] == assistant_tokens
    assert model["model"]

    performance = body["performance"]
    assert performance["window_hours"] == 24
    assert performance["calls"] == 1
    assert performance["success_rate"] == 1.0
    assert performance["avg_response_ms"] == 5000
    assert performance["avg_ttft_ms"] == 2000
    assert performance["tpm"] == round(assistant_tokens / (5 / 60))

    server = body["server"]
    assert server["uptime_seconds"] >= 0
    assert 0 <= server["cpu_percent"] <= 100
    assert "started_at" in server
