"""Voice reply orchestration: queue, background synthesis, assets and status."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from io import BytesIO
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..archive import models as m
from ..archive.assets import AssetStore
from ..archive.repository import digest, lock
from ..archive.stickers import attachment
from ..core.errors import AppError
from ..core.settings import VoiceSettings
from ..core.types import ModelRef, Scope
from ..delivery import models as dm
from ..delivery.stream import emit, emit_live
from .emotion import DEFAULT_EMOTION, director_prompt, instruction_for, normalize_emotion
from .engine import BreezeVoiceEngine, VoiceEngine, WavAudio
from .text import clean_for_speech

logger = logging.getLogger("agi_wind.voice")
TERMINAL = ("completed", "cancelled", "failed", "interrupted")
AUDIO_STATES = ("queued", "synthesizing", "ready")


class VoiceService:
    def __init__(
        self,
        db,
        assets: AssetStore,
        settings_getter,
        *,
        engine: VoiceEngine | None = None,
    ):
        self.db = db
        self.assets = assets
        self._settings = settings_getter
        self.engine = engine
        self._injected_engine = engine is not None
        self._auto_engine: BreezeVoiceEngine | None = None
        self._engine_key: tuple[str, float] | None = None
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._active: dict[UUID, int] = {}
        self._health_at = 0.0
        self._health_value: dict = {}
        self._voices_at = 0.0
        self._voices_value: list[dict] = []

    @property
    def settings(self) -> VoiceSettings:
        return self._settings()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.enabled)

    @property
    def probability(self) -> float:
        return float(self.settings.probability)

    def invalidate(self) -> None:
        self._health_at = 0.0
        self._health_value = {}
        self._voices_at = 0.0
        self._voices_value = []
        self._engine_key = None
        if not self._injected_engine:
            self._auto_engine = None

    def _engine_instance(self) -> VoiceEngine:
        if self.engine is not None:
            return self.engine
        settings = self.settings
        key = (settings.base_url, settings.timeout_seconds)
        if self._engine_key != key or self._auto_engine is None:
            self._auto_engine = BreezeVoiceEngine(settings.base_url, timeout=settings.timeout_seconds)
            self._engine_key = key
        return self._auto_engine

    async def health(self, *, force: bool = False) -> dict:
        if not self.enabled:
            return {"available": False, "status": "disabled", "sample_rate": 24000}
        now = time.monotonic()
        if not force and self._health_value and now - self._health_at < 10:
            return dict(self._health_value)
        try:
            result = await self._engine_instance().health()
        except Exception:
            logger.debug("voice health probe failed", exc_info=True)
            result = {"available": False, "status": "unreachable"}
        value = {
            "available": bool(result.get("available")),
            "status": result.get("status") or ("ready" if result.get("available") else "unreachable"),
            "sample_rate": int(result.get("sample_rate") or 24000),
        }
        self._health_at, self._health_value = now, value
        return dict(value)

    async def available(self) -> bool:
        return bool((await self.health()).get("available"))

    async def list_voices(self, *, force: bool = False) -> list[dict]:
        if not self.enabled:
            return []
        now = time.monotonic()
        if not force and self._voices_value and now - self._voices_at < 30:
            return [dict(row) for row in self._voices_value]
        try:
            rows = await self._engine_instance().list_voices()
        except Exception:
            logger.debug("voice list probe failed", exc_info=True)
            rows = []
        self._voices_at = time.monotonic()
        self._voices_value = [dict(row) for row in rows]
        return [dict(row) for row in self._voices_value]

    async def preview(self, text: str, emotion: str | None = None) -> WavAudio:
        if not self.enabled:
            raise AppError("VOICE_DISABLED", "本地语音尚未启用", 503)
        limit = min(200, self.settings.max_chars)
        spoken = clean_for_speech(text, limit)
        if not spoken.text:
            raise AppError("VOICE_EMPTY", "试听文本为空", 422)
        instruction = instruction_for(emotion or DEFAULT_EMOTION)
        async with self._lock:
            return await self._engine_instance().synthesize(spoken.text, self.settings.voice_id, instruction)

    async def queue(self, generation_id: UUID, mode: str, reply_text: str, emotion: str | None = None) -> dict | None:
        """Backward-compatible single-segment reservation."""
        return await self.queue_segment(generation_id, mode, reply_text, emotion=emotion, segment_index=0)

    async def queue_segment(
        self,
        generation_id: UUID,
        mode: str,
        display_text: str,
        emotion: str | None = None,
        *,
        segment_index: int = 0,
        segment_id: UUID | None = None,
        emotion_source: str | None = None,
    ) -> dict | None:
        """Reserve one speech segment and announce it before generation completes."""
        settings = self.settings
        if not settings.enabled:
            return None
        mode = mode if mode in ("auto", "always", "off") else "auto"
        emotion_value = normalize_emotion(emotion) if emotion else None
        spoken = clean_for_speech(display_text, settings.max_chars)
        if not spoken.text:
            return None
        requested_id = segment_id or uuid4()
        with self.db.engine.begin() as conn:
            generation = conn.execute(select(m.generations).where(m.generations.c.id == generation_id)).mappings().first()
            if not generation:
                return None
            event = conn.execute(
                select(m.events).where(m.events.c.generation_id == generation_id, m.events.c.role == "assistant")
            ).mappings().first()
            if not event:
                return None
            lock(conn, event["companion_id"])
            lock(conn, generation_id)
            generation = conn.execute(
                select(m.generations).where(m.generations.c.id == generation_id).with_for_update()
            ).mappings().first()
            event = conn.execute(
                select(m.events).where(m.events.c.id == event["id"]).with_for_update()
            ).mappings().first()
            if not generation or not event or generation["state"] in TERMINAL:
                return None
            existing = conn.execute(
                select(dm.speech_segments).where(
                    dm.speech_segments.c.generation_id == generation_id,
                    dm.speech_segments.c.epoch == generation["epoch"],
                    dm.speech_segments.c.segment_index == segment_index,
                )
            ).mappings().first()
            if existing and existing["state"] in AUDIO_STATES:
                return None
            segment_id = existing["id"] if existing else requested_id
            values = {
                "generation_id": generation_id,
                "epoch": generation["epoch"],
                "segment_index": segment_index,
                "source_event_id": event["id"],
                "display_text": display_text,
                "spoken_text": spoken.text,
                "voice_id": settings.voice_id,
                "voice_version": settings.voice_version,
                "asset_id": None,
                "sample_rate": None,
                "channels": None,
                "duration_ms": None,
                "state": "queued",
                "error_code": None,
            }
            if existing:
                conn.execute(dm.speech_segments.update().where(dm.speech_segments.c.id == segment_id).values(**values))
            else:
                conn.execute(dm.speech_segments.insert().values(id=segment_id, **values))
            content = dict(event["content"])
            entry = {
                "index": segment_index,
                "state": "queued",
                "mode": mode,
                "voice_id": settings.voice_id,
                "voice_version": settings.voice_version,
                "segment_id": str(segment_id),
                "emotion": emotion_value,
                "emotion_source": (emotion_source or "model_tag") if emotion_value else None,
                "instruction": None,
                "truncated": spoken.truncated,
                "error_code": None,
            }
            entries = [item for item in (content.get("voices") or []) if item.get("segment_id") != str(segment_id)]
            entries.append(entry)
            entries.sort(key=lambda item: item.get("index", 0))
            content["voices"] = entries
            self._sync_legacy_voice(content)
            conn.execute(
                m.events.update()
                .where(m.events.c.id == event["id"])
                .values(content=content, content_sha256=digest(content))
            )
            emit(
                conn,
                generation_id,
                "speech.queued",
                {
                    "segment_id": str(segment_id),
                    "segment_index": segment_index,
                    "mode": mode,
                    "voice_id": settings.voice_id,
                    "voice_version": settings.voice_version,
                    "state": "queued",
                    "truncated": spoken.truncated,
                },
                generation["epoch"],
            )
        return {
            "segment_id": segment_id,
            "segment_index": segment_index,
            "spoken_text": spoken.text,
            "truncated": spoken.truncated,
        }

    @staticmethod
    def _sync_legacy_voice(content: dict) -> None:
        """Keep the pre-segment ``content.voice`` key useful for old clients/tests."""
        voices = content.get("voices") or []
        if voices:
            content["voice"] = dict(voices[0])
        else:
            content.pop("voice", None)

    def start(
        self,
        generation_id: UUID,
        segment_id: UUID,
        scope: Scope,
        provider,
        model: ModelRef | None,
        user_text: str,
        reply_text: str,
        tone: str = "",
        emotion: str | None = None,
        emotion_source: str | None = None,
    ) -> None:
        # Reference-count per generation: a finished segment must not clear the active flag
        # while another segment of the same turn is still queued/synthesizing.
        self._active[generation_id] = self._active.get(generation_id, 0) + 1

        async def runner():
            try:
                await self._synthesize(
                    generation_id, segment_id, scope, provider, model, user_text, reply_text, tone, emotion, emotion_source
                )
            finally:
                remaining = self._active.get(generation_id, 0) - 1
                if remaining > 0:
                    self._active[generation_id] = remaining
                else:
                    self._active.pop(generation_id, None)

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _synthesize(
        self,
        generation_id: UUID,
        segment_id: UUID,
        scope: Scope,
        provider,
        model: ModelRef | None,
        user_text: str,
        reply_text: str,
        tone: str,
        emotion: str | None = None,
        emotion_source: str | None = None,
    ) -> None:
        asset = None
        try:
            with self.db.engine.connect() as conn:
                segment = conn.execute(
                    select(dm.speech_segments).where(dm.speech_segments.c.id == segment_id)
                ).mappings().first()
                generation = conn.execute(
                    select(m.generations).where(m.generations.c.id == generation_id)
                ).mappings().first()
            if not segment or not generation or generation["state"] in ("cancelled", "failed", "interrupted"):
                return
            if segment["state"] not in ("queued", "synthesizing"):
                return
            if not await self._mark_synthesizing(generation_id, segment_id, scope):
                return
            if emotion:
                emotion = normalize_emotion(emotion)
                emotion_source = emotion_source or "model_tag"
            else:
                emotion = await self._direct_emotion(provider, model, user_text, reply_text, tone)
                emotion_source = "director_fallback"
            instruction = instruction_for(emotion, tone)
            if not await self._save_emotion(generation_id, segment_id, scope, emotion, instruction, emotion_source):
                return
            async with self._lock:
                audio = await self._engine_instance().synthesize(
                    segment["spoken_text"], segment["voice_id"], instruction
                )
            asset = await self.assets.put(
                scope,
                BytesIO(audio.data),
                "audio/wav",
                original_filename="voice-" + str(segment_id) + ".wav",
            )
            attached = await self._finalize(
                generation_id,
                segment_id,
                scope,
                asset,
                emotion,
                instruction,
            )
            if not attached:
                self._discard_asset(asset, scope)
        except asyncio.CancelledError:
            if asset is not None:
                self._discard_asset(asset, scope)
            self._mark_ended(segment_id, scope, "cancelled", "SPEECH_CANCELLED")
            raise
        except AppError as error:
            if asset is not None:
                self._discard_asset(asset, scope)
            logger.warning("voice synthesis failed segment=%s code=%s", segment_id, error.code)
            self._mark_ended(segment_id, scope, "failed", error.code)
        except Exception:
            if asset is not None:
                self._discard_asset(asset, scope)
            logger.exception("voice synthesis crashed segment=%s", segment_id)
            self._mark_ended(segment_id, scope, "failed", "TTS_FAILED")

    async def _mark_synthesizing(self, generation_id: UUID, segment_id: UUID, scope: Scope) -> bool:
        with self.db.engine.begin() as conn:
            lock(conn, scope.companion_id)
            lock(conn, generation_id)
            generation = conn.execute(
                select(m.generations).where(m.generations.c.id == generation_id).with_for_update()
            ).mappings().first()
            if not generation or generation["state"] in ("cancelled", "failed", "interrupted"):
                return False
            segment = conn.execute(
                select(dm.speech_segments).where(dm.speech_segments.c.id == segment_id).with_for_update()
            ).mappings().first()
            if not segment or segment["state"] not in ("queued", "synthesizing"):
                return False
            conn.execute(dm.speech_segments.update().where(dm.speech_segments.c.id == segment_id).values(state="synthesizing"))
            self._set_voice(conn, generation_id, scope, {"state": "synthesizing"}, segment_id)
        return True

    async def _save_emotion(
        self,
        generation_id: UUID,
        segment_id: UUID,
        scope: Scope,
        emotion: str,
        instruction: str,
        emotion_source: str,
    ) -> bool:
        with self.db.engine.begin() as conn:
            lock(conn, scope.companion_id)
            lock(conn, generation_id)
            generation = conn.execute(
                select(m.generations).where(m.generations.c.id == generation_id).with_for_update()
            ).mappings().first()
            if not generation or generation["state"] in ("cancelled", "failed", "interrupted"):
                return False
            segment = conn.execute(
                select(dm.speech_segments).where(dm.speech_segments.c.id == segment_id).with_for_update()
            ).mappings().first()
            if not segment or segment["state"] not in ("queued", "synthesizing"):
                return False
            self._set_voice(
                conn,
                generation_id,
                scope,
                {"emotion": emotion, "instruction": instruction, "emotion_source": emotion_source},
                segment_id,
            )
        return True

    async def _finalize(
        self,
        generation_id: UUID,
        segment_id: UUID,
        scope: Scope,
        asset: dict,
        emotion: str,
        instruction: str,
    ) -> bool:
        with self.db.engine.begin() as conn:
            lock(conn, scope.companion_id)
            lock(conn, generation_id)
            generation = conn.execute(
                select(m.generations).where(m.generations.c.id == generation_id).with_for_update()
            ).mappings().first()
            segment = conn.execute(
                select(dm.speech_segments).where(dm.speech_segments.c.id == segment_id).with_for_update()
            ).mappings().first()
            if not generation or not segment:
                return False
            if generation["state"] in ("cancelled", "failed", "interrupted") or segment["state"] not in ("queued", "synthesizing"):
                conn.execute(
                    dm.speech_segments.update()
                    .where(dm.speech_segments.c.id == segment_id)
                    .values(state="cancelled", error_code="SPEECH_CANCELLED")
                )
                return False
            event = conn.execute(
                select(m.events).where(m.events.c.generation_id == generation_id, m.events.c.role == "assistant")
            ).mappings().first()
            if not event:
                conn.execute(
                    dm.speech_segments.update().where(dm.speech_segments.c.id == segment_id).values(state="cancelled", error_code="EVENT_MISSING")
                )
                return False
            entries = list((event["content"] or {}).get("voices") or [])
            current_entry = next((item for item in entries if item.get("segment_id") == str(segment_id)), None)
            if current_entry and current_entry.get("state") == "cancelled":
                conn.execute(
                    dm.speech_segments.update().where(dm.speech_segments.c.id == segment_id).values(state="cancelled", error_code="SPEECH_CANCELLED")
                )
                return False
            value = attachment(
                asset,
                "audio",
                "语音",
                emotion=emotion,
                voice_id=segment["voice_id"],
                segment_index=segment["segment_index"],
                segment_id=str(segment_id),
            )
            content = dict(event["content"] or {})
            attachments = list(content.get("attachments") or [])
            attachments.append(value)
            content["attachments"] = attachments
            entries = list(content.get("voices") or [])
            for entry in entries:
                if entry.get("segment_id") == str(segment_id):
                    entry.update(
                        {
                            "state": "ready",
                            "voice_id": segment["voice_id"],
                            "voice_version": segment["voice_version"],
                            "segment_id": str(segment_id),
                            "emotion": emotion,
                            "instruction": instruction,
                            "error_code": None,
                        }
                    )
                    break
            content["voices"] = entries
            self._sync_legacy_voice(content)
            conn.execute(
                m.events.update()
                .where(m.events.c.id == event["id"])
                .values(content=content, content_sha256=digest(content))
            )
            conn.execute(
                pg_insert(m.asset_refs)
                .values(event_id=event["id"], asset_id=asset["id"], purpose="output")
                .on_conflict_do_nothing()
            )
            conn.execute(
                dm.speech_segments.update()
                .where(dm.speech_segments.c.id == segment_id)
                .values(
                    state="ready",
                    asset_id=asset["id"],
                    sample_rate=(asset.get("metadata") or {}).get("sample_rate"),
                    channels=(asset.get("metadata") or {}).get("channels"),
                    duration_ms=(asset.get("metadata") or {}).get("duration_ms"),
                    error_code=None,
                )
            )
            emit_live(
                conn,
                scope.conversation_id,
                "speech.ready",
                {
                    "generation_id": str(generation_id),
                    "segment_id": str(segment_id),
                    "attachment": value,
                    "emotion": emotion,
                },
            )
        return True

    def _discard_asset(self, asset: dict, scope: Scope) -> None:
        """Remove an unreferenced WAV after cancellation/race; never steals shared media."""
        try:
            asset_id = asset["id"]
            with self.db.engine.begin() as conn:
                if conn.execute(select(m.asset_refs.c.asset_id).where(m.asset_refs.c.asset_id == asset_id)).first():
                    return
                if conn.execute(select(m.stickers.c.id).where(m.stickers.c.asset_id == asset_id)).first():
                    return
                if conn.execute(select(dm.speech_segments.c.id).where(dm.speech_segments.c.asset_id == asset_id)).first():
                    return
                conn.execute(
                    m.asset_owners.delete().where(
                        m.asset_owners.c.asset_id == asset_id,
                        m.asset_owners.c.account_id == scope.account_id,
                    )
                )
                if conn.execute(select(m.asset_owners.c.account_id).where(m.asset_owners.c.asset_id == asset_id)).first():
                    return
                conn.execute(m.assets.delete().where(m.assets.c.id == asset_id))
            root = self.assets.root.resolve()
            path = (root / asset["relative_path"]).resolve()
            if path.is_relative_to(root):
                path.unlink(missing_ok=True)
        except Exception:
            logger.warning("could not discard unreferenced voice asset", exc_info=True)

    def _set_voice(self, conn, generation_id: UUID, scope: Scope, changes: dict, segment_id: UUID | None = None) -> None:
        event = conn.execute(
            select(m.events).where(m.events.c.generation_id == generation_id, m.events.c.role == "assistant")
        ).mappings().first()
        if not event:
            return
        content = dict(event["content"] or {})
        entries = list(content.get("voices") or [])
        if segment_id is not None:
            found = False
            for entry in entries:
                if entry.get("segment_id") == str(segment_id):
                    entry.update(changes)
                    found = True
                    break
            if not found:
                entries.append({"segment_id": str(segment_id), **changes})
            content["voices"] = entries
        else:
            voice = dict(content.get("voice") or {})
            voice.update(changes)
            voice.setdefault("voice_id", self.settings.voice_id)
            content["voice"] = voice
        self._sync_legacy_voice(content)
        conn.execute(
            m.events.update()
            .where(m.events.c.id == event["id"])
            .values(content=content, content_sha256=digest(content))
        )

    def _mark_ended(self, segment_id: UUID, scope: Scope, state: str, code: str) -> None:
        try:
            with self.db.engine.begin() as conn:
                segment = conn.execute(
                    select(dm.speech_segments).where(dm.speech_segments.c.id == segment_id).with_for_update()
                ).mappings().first()
                if not segment or segment["state"] in ("ready", "cancelled"):
                    return
                conn.execute(
                    dm.speech_segments.update()
                    .where(dm.speech_segments.c.id == segment_id)
                    .values(state=state, error_code=code)
                )
                event = conn.execute(
                    select(m.events).where(m.events.c.generation_id == segment["generation_id"], m.events.c.role == "assistant")
                ).mappings().first()
                if event:
                    content = dict(event["content"] or {})
                    entries = list(content.get("voices") or [])
                    for entry in entries:
                        if entry.get("segment_id") == str(segment_id):
                            entry.update({"state": state, "error_code": code})
                            break
                    content["voices"] = entries
                    self._sync_legacy_voice(content)
                    conn.execute(
                        m.events.update()
                        .where(m.events.c.id == event["id"])
                        .values(content=content, content_sha256=digest(content))
                    )
                emit_live(
                    conn,
                    scope.conversation_id,
                    "speech." + ("cancelled" if state == "cancelled" else "failed"),
                    {
                        "generation_id": str(segment["generation_id"]),
                        "segment_id": str(segment_id),
                        "segment_index": segment["segment_index"],
                        "error_code": code,
                    },
                )
        except Exception:
            logger.debug("could not record voice end state segment=%s", segment_id, exc_info=True)

    async def _direct_emotion(
        self,
        provider,
        model: ModelRef | None,
        user_text: str,
        reply_text: str,
        tone: str,
    ) -> str:
        if provider is None or model is None:
            return DEFAULT_EMOTION
        params = dict(model.parameters or {})
        supported = getattr(getattr(provider, "config", None), "supported_parameters", []) or []
        if "max_output_tokens" in supported:
            params["max_output_tokens"] = 120
        elif "max_tokens" in supported:
            params["max_tokens"] = 120
        tuned = model.model_copy(update={"parameters": params})
        messages = [{"role": "system", "content": director_prompt(user_text, reply_text, tone)}]

        async def collect() -> str:
            raw = ""
            async for chunk in provider.stream(tuned, messages):
                if not isinstance(chunk, str) and chunk.kind == "reasoning":
                    continue
                raw += chunk if isinstance(chunk, str) else chunk.text
                if len(raw) > 2000:
                    break
            return raw

        try:
            raw = await asyncio.wait_for(collect(), timeout=8)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("voice director failed; falling back to neutral", exc_info=True)
            return DEFAULT_EMOTION
        value = raw.strip()
        if value.startswith("```"):
            value = value[3:].strip()
            if value[:4].lower() == "json":
                value = value[4:].strip()
            if value.endswith("```"):
                value = value[:-3].strip()
        start, end = value.find("{"), value.rfind("}")
        if start >= 0 and end > start:
            value = value[start : end + 1]
        try:
            payload = json.loads(value)
            return normalize_emotion(payload.get("emotion") if isinstance(payload, dict) else payload)
        except Exception:
            return DEFAULT_EMOTION

    async def direct_emotion(
        self,
        provider,
        model: ModelRef | None,
        user_text: str,
        reply_text: str,
        tone: str = "",
    ) -> str:
        """Restricted fallback emotion used once per turn when the model omitted its tag."""
        return await self._direct_emotion(provider, model, user_text, reply_text, tone)

    def speech_status(self, generation_id: UUID, epoch: int | None = None) -> dict:
        with self.db.engine.connect() as conn:
            generation = conn.execute(select(m.generations).where(m.generations.c.id == generation_id)).mappings().first()
            if not generation:
                return {"state": "none", "segments": []}
            epoch = epoch or generation["epoch"]
            rows = list(
                conn.execute(
                    select(dm.speech_segments)
                    .where(dm.speech_segments.c.generation_id == generation_id, dm.speech_segments.c.epoch == epoch)
                    .order_by(dm.speech_segments.c.segment_index)
                ).mappings()
            )
            event = conn.execute(
                select(m.events).where(m.events.c.generation_id == generation_id, m.events.c.role == "assistant")
            ).mappings().first()
            content = dict(event["content"] or {}) if event else {}
            voices = {
                str(item.get("segment_id")): item for item in (content.get("voices") or []) if item.get("segment_id")
            }
            voice = dict(content.get("voice") or {})
            if not rows:
                # Legacy archives (or an epoch advanced by interrupt) keep their summary state.
                return {
                    "state": voice.get("state") or "none",
                    "segment_id": voice.get("segment_id"),
                    "voice_id": voice.get("voice_id") or self.settings.voice_id,
                    "mode": voice.get("mode") or "auto",
                    "emotion": voice.get("emotion"),
                    "emotion_source": voice.get("emotion_source"),
                    "truncated": bool(voice.get("truncated", False)),
                    "attachment": None,
                    "error_code": voice.get("error_code"),
                    "segments": [],
                }
            stale = False
            if generation["state"] in TERMINAL and generation_id not in self._active:
                stamp = generation.get("finished_at") or generation.get("created_at")
                age = 999.0
                if stamp:
                    try:
                        age = (m.now() - stamp).total_seconds()
                    except TypeError:
                        age = 999.0
                stale = age >= 5.0
            segments: list[dict] = []
            for row in rows:
                state, error_code = row["state"], row["error_code"]
                if state in ("queued", "synthesizing") and stale:
                    # ``generation.completed`` is visible before the background task is scheduled;
                    # keep a short grace window, then call genuinely stranded speech interrupted.
                    state, error_code = "failed", "SPEECH_INTERRUPTED"
                audio = None
                if state == "ready" and row["asset_id"]:
                    asset = conn.execute(select(m.assets).where(m.assets.c.id == row["asset_id"])).mappings().first()
                    if asset:
                        info = voices.get(str(row["id"])) or {}
                        audio = attachment(
                            asset,
                            "audio",
                            "语音",
                            emotion=info.get("emotion"),
                            voice_id=row["voice_id"],
                            segment_index=row["segment_index"],
                            segment_id=str(row["id"]),
                        )
                    else:
                        state, error_code = "failed", "ASSET_MISSING"
                info = voices.get(str(row["id"])) or {}
                segments.append(
                    {
                        "segment_id": str(row["id"]),
                        "segment_index": row["segment_index"],
                        "state": state,
                        "attachment": audio,
                        "voice_id": row["voice_id"],
                        "mode": info.get("mode") or voice.get("mode") or "auto",
                        "emotion": info.get("emotion") or voice.get("emotion"),
                        "emotion_source": info.get("emotion_source") or voice.get("emotion_source"),
                        "truncated": bool(info.get("truncated", voice.get("truncated", False))),
                        "error_code": error_code,
                    }
                )
            pending = [item for item in segments if item["state"] in ("queued", "synthesizing")]
            if pending:
                state = "synthesizing" if any(item["state"] == "synthesizing" for item in pending) else "queued"
                error_code = None
            elif segments and all(item["state"] == "ready" for item in segments):
                state, error_code = "ready", None
            elif any(item["state"] == "failed" for item in segments):
                failed = next(item for item in segments if item["state"] == "failed")
                state, error_code = "failed", failed.get("error_code")
            elif any(item["state"] == "cancelled" for item in segments):
                state, error_code = "cancelled", next(
                    item.get("error_code") for item in segments if item["state"] == "cancelled"
                )
            else:
                state, error_code = "none", None
            first = segments[0] if segments else {}
            ready_audio = next((item["attachment"] for item in segments if item["attachment"]), None)
            return {
                "state": state,
                "segment_id": first.get("segment_id") or voice.get("segment_id"),
                "voice_id": first.get("voice_id") or voice.get("voice_id") or self.settings.voice_id,
                "mode": first.get("mode") or voice.get("mode") or "auto",
                "emotion": first.get("emotion") or voice.get("emotion"),
                "emotion_source": first.get("emotion_source") or voice.get("emotion_source"),
                "truncated": bool(first.get("truncated", voice.get("truncated", False))),
                "attachment": ready_audio,
                "error_code": error_code,
                "segments": segments,
            }

    def cleanup_on_startup(self) -> None:
        try:
            with self.db.engine.begin() as conn:
                conn.execute(
                    dm.speech_segments.update()
                    .where(dm.speech_segments.c.state.in_(("queued", "synthesizing")))
                    .values(state="failed", error_code="PROCESS_RESTART")
                )
        except Exception:
            logger.warning("voice startup cleanup failed", exc_info=True)

    async def wait_for_idle(self, timeout: float = 30.0) -> None:
        async def wait() -> None:
            while self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)

        await asyncio.wait_for(wait(), timeout)

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._active.clear()
