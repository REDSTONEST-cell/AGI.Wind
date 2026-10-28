from io import BytesIO
import wave

import httpx
import pytest

from nagi.core.errors import AppError
from nagi.voice.engine import BreezeVoiceEngine


def pcm(seconds=0.2, rate=24000):
    return b"\x00\x00" * int(rate * seconds)


def wav_bytes(seconds=0.2, rate=24000):
    buffer = BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(pcm(seconds, rate))
    return buffer.getvalue()


async def test_engine_wraps_pcm_and_reports_duration():
    captured = {}

    async def handler(request):
        if request.url.path == "/v1/audio/speech":
            captured["body"] = await request.aread()
            captured["type"] = request.headers.get("content-type", "")
            return httpx.Response(200, content=pcm(), headers={"X-Sample-Rate": "24000", "Content-Type": "application/octet-stream"})
        return httpx.Response(404)

    engine = BreezeVoiceEngine("http://voice.test", transport=httpx.MockTransport(handler))
    audio = await engine.synthesize("你好", "yuwan", "开心一点")
    assert audio.data[:4] == b"RIFF"
    assert audio.sample_rate == 24000
    assert audio.channels == 1
    assert audio.duration_ms == 200
    assert b"yuwan" in captured["body"]
    assert "urlencoded" in captured["type"]


async def test_engine_retries_busy_then_succeeds():
    calls = {"count": 0}

    async def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(409)
        return httpx.Response(200, content=pcm(0.1), headers={"X-Sample-Rate": "24000"})

    engine = BreezeVoiceEngine("http://voice.test", transport=httpx.MockTransport(handler))
    audio = await engine.synthesize("你好", "yuwan", "平静")
    assert calls["count"] == 2
    assert audio.duration_ms == 100


async def test_engine_maps_missing_voice_and_invalid_pcm():
    async def missing(request):
        return httpx.Response(404)

    engine = BreezeVoiceEngine("http://voice.test", transport=httpx.MockTransport(missing))
    with pytest.raises(AppError) as error:
        await engine.synthesize("你好", "missing", "平静")
    assert error.value.code == "VOICE_NOT_FOUND"

    async def odd(request):
        return httpx.Response(200, content=b"\x00", headers={"X-Sample-Rate": "24000"})

    engine = BreezeVoiceEngine("http://voice.test", transport=httpx.MockTransport(odd))
    with pytest.raises(AppError) as error:
        await engine.synthesize("你好", "yuwan", "平静")
    assert error.value.code == "TTS_INVALID_AUDIO"


async def test_engine_accepts_wav_passthrough():
    raw = wav_bytes(0.1)

    async def handler(request):
        return httpx.Response(200, content=raw, headers={"Content-Type": "audio/wav"})

    engine = BreezeVoiceEngine("http://voice.test", transport=httpx.MockTransport(handler))
    audio = await engine.synthesize("你好", "yuwan", "平静")
    assert audio.data == raw
    assert audio.duration_ms == 100


async def test_engine_health_and_voice_list():
    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "sample_rate": 24000})
        return httpx.Response(200, json=[{"id": "yuwan", "seconds": 8.48, "saved": True}])

    engine = BreezeVoiceEngine("http://voice.test", transport=httpx.MockTransport(handler))
    health = await engine.health()
    assert health["available"] is True and health["sample_rate"] == 24000
    voices = await engine.list_voices()
    assert voices == [{"id": "yuwan", "name": "yuwan", "description": "", "seconds": 8.48, "frames": None, "saved": True}]
