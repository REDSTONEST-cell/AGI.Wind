"""Opt-in real Breeze + real dialogue-provider check using a disposable schema."""
import os
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("AGI_RUN_VOICE_LIVE") != "1", reason="explicit opt-in GPU/vendor call"),
]


async def test_real_breeze_voice_reply(db, actor, seeded_scope, chat_request_factory, tmp_path, monkeypatch):
    from nagi.api.app import from_env
    from nagi.archive.assets import AssetStore
    from nagi.delivery.service import GenerationService
    from nagi.voice.engine import BreezeVoiceEngine
    from nagi.voice.service import VoiceService

    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv("AGI_CONFIG", str(root / "config/app.local.yaml"))
    source_app = from_env()
    real = source_app.state.service
    settings = source_app.state.settings
    assert settings.voice.enabled and real.provider and real.model, "live voice config and dialogue model required"
    store = AssetStore(db, tmp_path / "assets")
    engine = BreezeVoiceEngine(settings.voice.base_url, timeout=settings.voice.timeout_seconds)
    voice = VoiceService(db, store, lambda: settings.voice, engine=engine)
    service = GenerationService(db, real.provider, model=real.model, random_value=lambda: 1, voice=voice)
    request = chat_request_factory(text="用一句温柔的话回应我，今天有点累。", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    try:
        accepted = await service.submit(actor, request)
        assert await service.run(accepted.generation_id)
        await voice.wait_for_idle(timeout=180)
        content = db.all("events", role="assistant")[0]["content"]
        segment = db.all("speech_segments")[0]
        assert content["voice"]["state"] == "ready"
        assert segment["state"] == "ready" and segment["asset_id"]
        assert "[情绪:" not in content["text"]
        audio = [item for item in content["attachments"] if item["kind"] == "audio"]
        assert audio and audio[0]["duration_ms"] > 0
        print(
            "LIVE VOICE: emotion="
            + str(content["voice"].get("emotion"))
            + " source="
            + str(content["voice"].get("emotion_source"))
            + " duration_ms="
            + str(audio[0]["duration_ms"])
            + " text_chars="
            + str(len(content["text"]))
        )
    finally:
        await real.provider.aclose()
        registry = getattr(real, "registry", None)
        if registry:
            await registry.aclose()
        source_app.state.db.engine.dispose()
