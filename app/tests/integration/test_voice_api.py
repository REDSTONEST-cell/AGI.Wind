from io import BytesIO
import json
import wave

import httpx
import pytest
from sqlalchemy import text

from agi_wind.archive.assets import AssetStore
from agi_wind.core.settings import Settings, VoiceSettings
from agi_wind.core.types import ModelRef
from agi_wind.delivery.service import GenerationService
from agi_wind.voice.engine import WavAudio
from agi_wind.voice.service import VoiceService


def wav_bytes(seconds=0.2, rate=24000):
    buffer = BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


class VoiceEngine:
    def __init__(self):
        self.instruction = ""

    async def health(self):
        return {"available": True, "status": "ok", "sample_rate": 24000}

    async def list_voices(self):
        return [{"id": "yuwan", "name": "yuwan", "description": "鱼丸", "seconds": 8.48, "saved": True}]

    async def synthesize(self, text, voice_id, instruction):
        self.instruction = instruction
        return WavAudio(wav_bytes(), 24000, 1, 200)


class VoiceProvider:
    async def stream(self, model, messages):
        if "语音语气导演" in messages[0]["content"]:
            yield '{"emotion":"开心"}'
            return
        yield "[情绪:开心]\n今天真开心。"


async def build_app(db, actor, tmp_path):
    from agi_wind.api.app import create_app

    settings = Settings(
        access_token="synthetic-private-token",
        data_dir=tmp_path,
        voice=VoiceSettings(enabled=True, probability=0.3, voice_id="yuwan"),
    )
    service = GenerationService(db, None, model=None)
    service.voice = VoiceService(db, AssetStore(db, tmp_path / "assets"), lambda: settings.voice, engine=VoiceEngine())
    app = create_app(settings, db, actor, service)
    return app, settings, service.voice


async def test_voice_settings_voice_list_and_preview(db, actor, seeded_scope, tmp_path):
    app, settings, voice = await build_app(db, actor, tmp_path)
    headers = {"Authorization": "Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/api/v1/voice/settings", headers=headers)
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["enabled"] is True and payload["available"] is True
        assert payload["voice_id"] == "yuwan"
        assert payload["voices"][0]["id"] == "yuwan"

        patch = await client.patch("/api/v1/voice/settings", headers=headers, json={"probability": 0.5, "max_chars": 300})
        assert patch.status_code == 200
        assert patch.json()["probability"] == 0.5 and patch.json()["max_chars"] == 300
        saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert saved["voice"]["probability"] == 0.5

        voices = await client.get("/api/v1/voices", headers=headers)
        assert voices.status_code == 200
        assert voices.json()["voices"][0]["id"] == "yuwan"
        assert voices.json()["status"] == "ok"

        preview = await client.post("/api/v1/voice/preview", headers=headers, json={"text": "今天真开心", "emotion": "开心"})
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("audio/wav")
        assert preview.content[:4] == b"RIFF"
        assert "带笑意" in voice.engine.instruction

        capabilities = await client.get("/api/v1/capabilities", headers=headers)
        assert capabilities.json()["tts"] is True
        ready = await client.get("/health/ready")
        assert ready.json()["dependencies"]["tts"] == "available"


async def test_invalid_voice_settings_are_rejected(db, actor, seeded_scope, tmp_path):
    app, settings, voice = await build_app(db, actor, tmp_path)
    headers = {"Authorization": "Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        bad_url = await client.patch("/api/v1/voice/settings", headers=headers, json={"base_url": "not-a-url"})
        assert bad_url.status_code == 422
        missing = await client.patch("/api/v1/voice/settings", headers=headers, json={"voice_id": "missing"})
        assert missing.status_code == 422
        assert settings.voice.voice_id == "yuwan"

        disabled = await client.patch("/api/v1/voice/settings", headers=headers, json={"enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["available"] is False
        capabilities = await client.get("/api/v1/capabilities", headers=headers)
        assert capabilities.json()["tts"] is False
        ready = await client.get("/health/ready")
        assert ready.json()["dependencies"]["tts"] == "unconfigured"


async def test_history_returns_audio_attachment(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from agi_wind.api.app import create_app

    settings = Settings(
        access_token="synthetic-private-token",
        data_dir=tmp_path,
        voice=VoiceSettings(enabled=True, probability=1.0, voice_id="yuwan"),
    )
    provider = VoiceProvider()
    service = GenerationService(db, provider, model=ModelRef(provider_id="fake", model_id="voice"), random_value=lambda: 0.5)
    service.voice = VoiceService(db, AssetStore(db, tmp_path / "assets"), lambda: settings.voice, engine=VoiceEngine())
    app = create_app(settings, db, actor, service)
    request = chat_request_factory(text="今天好开心", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    accepted = await service.submit(actor, request)
    assert await service.run(accepted.generation_id)
    await service.voice.wait_for_idle()
    headers = {"Authorization": "Bearer synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        history = await client.get(f"/api/v1/conversations/{seeded_scope.conversation_id}/events", headers=headers)
        assert history.status_code == 200
        assistant = [row for row in history.json() if row["role"] == "assistant"][0]
        assert assistant["content"]["voice"]["state"] == "ready"
        audio = [item for item in assistant["attachments"] if item["kind"] == "audio"]
        assert len(audio) == 1
        assert audio[0]["duration_ms"] == 200
        status = await client.get(f"/api/v1/generations/{accepted.generation_id}/speech", headers=headers)
        assert status.status_code == 200
        assert status.json()["state"] == "ready"
        assert status.json()["attachment"]["asset_id"] == audio[0]["asset_id"]
