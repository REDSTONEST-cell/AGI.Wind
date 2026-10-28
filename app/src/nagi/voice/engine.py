"""Breeze-TTS HTTP engine and a tiny protocol so tests never touch the GPU."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import BytesIO
import logging
import time
from typing import Protocol
import wave

import httpx

from ..core.errors import AppError

logger = logging.getLogger("nagi.voice")

MAX_AUDIO_SECONDS = 115.0


@dataclass(frozen=True)
class WavAudio:
    data: bytes
    sample_rate: int
    channels: int
    duration_ms: int


class VoiceEngine(Protocol):
    async def health(self) -> dict: ...

    async def list_voices(self) -> list[dict]: ...

    async def synthesize(self, text: str, voice_id: str, instruction: str) -> WavAudio: ...


class BreezeVoiceEngine:
    """Minimal client for breeze-server's HTTP API (one request at a time upstream)."""

    def __init__(self, base_url: str, *, timeout: float = 180.0, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=self.transport,
            follow_redirects=False,
        )

    async def health(self) -> dict:
        try:
            async with self._client(2.0) as client:
                response = await client.get(self.base_url + "/health")
            if response.status_code != 200:
                return {"available": False, "status": response.status_code}
            payload = response.json() if response.content else {}
            if not isinstance(payload, dict):
                payload = {}
            return {
                "available": payload.get("status") == "ok",
                "status": payload.get("status") or "unknown",
                "sample_rate": int(payload.get("sample_rate") or 24000),
            }
        except Exception as error:
            logger.debug("voice health probe failed: %s", type(error).__name__)
            return {"available": False, "status": "unreachable"}

    async def list_voices(self) -> list[dict]:
        try:
            async with self._client(8.0) as client:
                response = await client.get(self.base_url + "/v1/voices")
            if response.status_code != 200:
                return []
            payload = response.json()
        except Exception as error:
            logger.debug("voice list probe failed: %s", type(error).__name__)
            return []
        rows = payload if isinstance(payload, list) else (payload.get("voices") if isinstance(payload, dict) else None)
        if not isinstance(rows, list):
            return []
        voices = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            voices.append(
                {
                    "id": str(row["id"]),
                    "name": str(row.get("name") or row["id"]),
                    "description": str(row.get("description") or ""),
                    "seconds": row.get("seconds"),
                    "frames": row.get("frames"),
                    "saved": bool(row.get("saved", True)),
                }
            )
        return voices

    async def synthesize(self, text: str, voice_id: str, instruction: str) -> WavAudio:
        form = {
            "text": text,
            "voice_id": voice_id,
            "instruction": instruction,
            "cfg_scale": "1.0",
            "seed": "42",
        }
        last_busy: Exception | None = None
        for attempt in range(3):
            started = time.perf_counter()
            first_at: float | None = None
            try:
                async with self._client(self.timeout) as client:
                    async with client.stream("POST", self.base_url + "/v1/audio/speech", data=form) as response:
                        if response.status_code == 409:
                            last_busy = AppError("TTS_BUSY", "语音服务正在处理另一条语音", 409, retryable=True)
                        elif response.status_code == 404:
                            raise AppError("VOICE_NOT_FOUND", "本地没有找到这个音色", 404)
                        elif response.status_code >= 400:
                            raise AppError("TTS_FAILED", "本地语音合成失败", 502, retryable=True)
                        else:
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                if first_at is None:
                                    first_at = time.perf_counter()
                                chunks.extend(chunk)
                            sample_rate = int(response.headers.get("x-sample-rate") or 24000)
                            content_type = response.headers.get("content-type", "")
                            audio = self._to_wav(bytes(chunks), sample_rate, content_type)
                            elapsed = time.perf_counter() - started
                            duration = audio.duration_ms / 1000
                            logger.info(
                                "voice synthesized voice_id=%s seconds=%.2f total_ms=%d rtf=%.2f first_audio_ms=%s",
                                voice_id,
                                duration,
                                round(elapsed * 1000),
                                (elapsed / duration) if duration else 0,
                                round((first_at - started) * 1000) if first_at is not None else None,
                            )
                            return audio
            except AppError:
                raise
            except httpx.TimeoutException as error:
                raise AppError("TTS_TIMEOUT", "本地语音合成超时", 504, retryable=True) from error
            except httpx.HTTPError as error:
                raise AppError("TTS_UNAVAILABLE", "无法连接本地语音服务", 503, retryable=True) from error
            if last_busy is not None and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last_busy or AppError("TTS_BUSY", "语音服务正忙，请稍后再试", 409, retryable=True)

    @staticmethod
    def _to_wav(raw: bytes, sample_rate: int, content_type: str) -> WavAudio:
        if not raw:
            raise AppError("TTS_EMPTY", "本地语音服务没有返回音频", 502, retryable=True)
        if raw[:4] == b"RIFF":
            reader = wave.open(BytesIO(raw), "rb")
            try:
                frames = reader.getnframes()
                rate = reader.getframerate()
                channels = reader.getnchannels()
                width = reader.getsampwidth()
            finally:
                reader.close()
            if not rate or not frames or width != 2 or channels < 1:
                raise AppError("TTS_INVALID_AUDIO", "本地语音返回了无法解析的音频", 502)
            duration_ms = round(frames * 1000 / rate)
            if duration_ms > MAX_AUDIO_SECONDS * 1000:
                raise AppError("TTS_AUDIO_TOO_LONG", "语音超过单条时长上限", 502)
            return WavAudio(raw, rate, channels, duration_ms)
        if len(raw) % 2:
            raise AppError("TTS_INVALID_AUDIO", "本地语音返回的 PCM 字节数不完整", 502)
        rate = int(sample_rate or 24000)
        frames = len(raw) // 2
        duration_ms = round(frames * 1000 / rate) if rate else 0
        if not duration_ms:
            raise AppError("TTS_EMPTY", "本地语音返回了空音频", 502)
        if duration_ms > MAX_AUDIO_SECONDS * 1000:
            raise AppError("TTS_AUDIO_TOO_LONG", "语音超过单条时长上限", 502)
        buffer = BytesIO()
        writer = wave.open(buffer, "wb")
        try:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(rate)
            writer.writeframes(raw)
        finally:
            writer.close()
        return WavAudio(buffer.getvalue(), rate, 1, duration_ms)
