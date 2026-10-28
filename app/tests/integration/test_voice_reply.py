import asyncio
from io import BytesIO
from uuid import uuid4
import wave

import pytest

from nagi.archive.assets import AssetStore
from nagi.core.errors import AppError
from nagi.core.settings import VoiceSettings
from nagi.core.types import ModelRef
from nagi.delivery.service import GenerationService
from nagi.voice.engine import WavAudio
from nagi.voice.service import VoiceService


def wav_bytes(seconds=0.2, rate=24000):
    buffer = BytesIO()
    with wave.open(buffer, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x00\x00" * int(rate * seconds))
    return buffer.getvalue()


class FakeEngine:
    def __init__(self, *, available=True, fail=None, audio=None):
        self.available = available
        self.fail = fail
        self.audio = audio or wav_bytes()
        self.calls = []

    async def health(self):
        return {"available": self.available, "status": "ok" if self.available else "unreachable", "sample_rate": 24000}

    async def list_voices(self):
        return [{"id": "yuwan", "name": "yuwan", "description": "test", "seconds": 8.48, "saved": True}]

    async def synthesize(self, text, voice_id, instruction):
        self.calls.append((text, voice_id, instruction))
        if self.fail:
            raise AppError(self.fail, "synthetic tts failure", 502)
        return WavAudio(self.audio, 24000, 1, 200)


class BlockingEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def synthesize(self, text, voice_id, instruction):
        self.calls.append((text, voice_id, instruction))
        self.started.set()
        await self.release.wait()
        return WavAudio(self.audio, 24000, 1, 200)


class VoiceProvider:
    def __init__(self, reply="今天好开心呀。", emotion="开心", main_tag=True, director_json=True):
        self.reply = reply
        self.emotion = emotion
        self.main_tag = main_tag
        self.director_json = director_json
        self.calls = []

    async def stream(self, model, messages):
        self.calls.append(messages)
        if "语音语气导演" in messages[0]["content"]:
            if self.director_json:
                yield '{"emotion":"' + self.emotion + '"}'
            else:
                yield "不是 JSON"
            return
        if self.main_tag:
            yield "[情绪:" + self.emotion + "]\n"
        yield self.reply


def build_service(db, tmp_path, provider, engine, *, probability=1.0, random_value=lambda: 0.5, text_break_probability=0.0):
    settings = VoiceSettings(enabled=True, probability=probability, voice_id="yuwan", max_chars=400, text_break_probability=text_break_probability)
    voice = VoiceService(db, AssetStore(db, tmp_path / "assets"), lambda: settings, engine=engine)
    service = GenerationService(
        db,
        provider,
        model=ModelRef(provider_id="fake", model_id="voice"),
        random_value=random_value,
        voice=voice,
    )
    return service, voice, settings


async def run_turn(service, voice, actor, request):
    accepted = await service.submit(actor, request)
    assert await service.run(accepted.generation_id)
    await voice.wait_for_idle()
    return accepted.generation_id


async def test_always_creates_playable_audio_attachment(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider()
    engine = FakeEngine()
    service, voice, _ = build_service(db, tmp_path, provider, engine)
    request = chat_request_factory(text="我今天升职了", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    generation_id = await run_turn(service, voice, actor, request)

    events = service.replay(actor, generation_id)
    assert any(event.type == "speech.queued" for event in events)
    segment = db.all("speech_segments")[0]
    assert segment["state"] == "ready"
    assert segment["voice_id"] == "yuwan"
    assert segment["duration_ms"] == 200
    assert segment["asset_id"]
    content = db.all("events", role="assistant")[0]["content"]
    assert content["text"] == "今天好开心呀。"
    assert content["voice"]["state"] == "ready"
    assert content["voice"]["emotion"] == "开心"
    assert "带笑意" in content["voice"]["instruction"]
    audio = [item for item in content["attachments"] if item["kind"] == "audio"]
    assert len(audio) == 1
    assert audio[0]["duration_ms"] == 200
    assert audio[0]["emotion"] == "开心"
    assert db.count("assets") == 1
    status = voice.speech_status(generation_id)
    assert status["state"] == "ready"
    assert status["emotion"] == "开心"
    assert status["emotion_source"] == "model_tag"
    assert status["attachment"]["asset_id"] == str(segment["asset_id"])
    assert len(provider.calls) == 1
    assert "情绪标记" in provider.calls[0][0]["content"]
    assert "[情绪:开心]" not in content["text"]
    assert engine.calls[0][1] == "yuwan"
    with wave.open(BytesIO((tmp_path / "assets" / db.all("assets")[0]["relative_path"]).read_bytes()), "rb") as stream:
        assert stream.getframerate() == 24000 and stream.getnframes() == 4800


async def test_auto_probability_zero_never_queues(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider()
    service, voice, _ = build_service(db, tmp_path, provider, FakeEngine(), probability=0.0, random_value=lambda: 0.5)
    request = chat_request_factory(text="今天天气不错", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "auto"
    generation_id = await run_turn(service, voice, actor, request)
    assert not any(event.type == "speech.queued" for event in service.replay(actor, generation_id))
    assert db.count("speech_segments") == 0
    assert "voice" not in db.all("events", role="assistant")[0]["content"]


async def test_auto_probability_one_queues(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider()
    service, voice, _ = build_service(db, tmp_path, provider, FakeEngine(), probability=1.0, random_value=lambda: 0.0)
    request = chat_request_factory(text="今天天气不错", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "auto"
    generation_id = await run_turn(service, voice, actor, request)
    assert any(event.type == "speech.queued" for event in service.replay(actor, generation_id))
    assert db.count("speech_segments") == 1


async def test_explicit_voice_request_forces_voice_with_zero_auto_probability(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider()
    service, voice, _ = build_service(
        db, tmp_path, provider, FakeEngine(), probability=0.0, random_value=lambda: 0.5
    )
    request = chat_request_factory(text="发一条语音来听听", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "auto"
    generation_id = await run_turn(service, voice, actor, request)
    assert any(event.type == "speech.queued" for event in service.replay(actor, generation_id))
    assert db.count("speech_segments") == 1
    content = db.all("events", role="assistant")[0]["content"]
    assert content["voice"]["mode"] == "always"


async def test_tts_failure_keeps_text_and_marks_voice_failed(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider()
    service, voice, _ = build_service(db, tmp_path, provider, FakeEngine(fail="TTS_FAILED"))
    request = chat_request_factory(text="今天好开心", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    generation_id = await run_turn(service, voice, actor, request)
    segment = db.all("speech_segments")[0]
    assert segment["state"] == "failed" and segment["error_code"] == "TTS_FAILED"
    content = db.all("events", role="assistant")[0]["content"]
    assert content["text"] == "今天好开心呀。"
    assert content["state"] == "complete"
    assert content["voice"]["state"] == "failed"
    assert not any(item["kind"] == "audio" for item in content.get("attachments", []))
    assert db.count("assets") == 0
    assert service.replay(actor, generation_id)[-1].type == "generation.completed"


async def test_unavailable_breeze_warns_for_always_but_completes(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider()
    service, voice, _ = build_service(db, tmp_path, provider, FakeEngine(available=False))
    request = chat_request_factory(text="今天好开心", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    generation_id = await run_turn(service, voice, actor, request)
    warnings = [event for event in service.replay(actor, generation_id) if event.type == "warning"]
    assert warnings and warnings[0].payload["code"] == "TTS_UNAVAILABLE"
    assert db.count("speech_segments") == 0
    assert db.all("events", role="assistant")[0]["content"]["text"] == "今天好开心呀。"
    assert service.replay(actor, generation_id)[-1].type == "generation.completed"


async def test_missing_tag_uses_director_fallback(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider(emotion="难过", main_tag=False)
    service, voice, _ = build_service(db, tmp_path, provider, FakeEngine())
    request = chat_request_factory(text="今天有点累", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    generation_id = await run_turn(service, voice, actor, request)
    content = db.all("events", role="assistant")[0]["content"]
    assert content["text"] == "今天好开心呀。"
    assert content["voice"]["emotion"] == "难过"
    assert content["voice"]["emotion_source"] == "director_fallback"
    assert voice.speech_status(generation_id)["emotion_source"] == "director_fallback"
    assert len(provider.calls) == 2


async def test_emotion_director_failure_falls_back_to_neutral(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider(director_json=False, main_tag=False)
    engine = FakeEngine()
    service, voice, _ = build_service(db, tmp_path, provider, engine)
    request = chat_request_factory(text="今天好开心", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    await run_turn(service, voice, actor, request)
    content = db.all("events", role="assistant")[0]["content"]
    assert content["text"] == "今天好开心呀。"
    assert content["voice"]["emotion"] == "平静"
    assert content["voice"]["emotion_source"] == "director_fallback"
    assert "日常聊天" in content["voice"]["instruction"]


async def test_voice_off_has_no_segment(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider()
    service, voice, _ = build_service(db, tmp_path, provider, FakeEngine())
    request = chat_request_factory(text="今天好开心", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "off"
    generation_id = await run_turn(service, voice, actor, request)
    assert not any(event.type == "speech.queued" for event in service.replay(actor, generation_id))
    assert db.count("speech_segments") == 0


async def test_agent_tool_reply_keeps_hidden_emotion_and_playable_voice(db, actor, seeded_scope, chat_request_factory, tmp_path):
    from nagi.agent.settings import AgentSettings
    from nagi.providers.protocol import ToolCall, ToolTurn

    class AgentVoiceProvider(VoiceProvider):
        async def tool_turn(self, model, messages, tools, tool_choice="auto"):
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                return ToolTurn("先查一下", "查询工具", (ToolCall("memory-voice", "search_memory", '{"query":"升职"}'),))
            return ToolTurn("[情绪:开心]\n今天好开心呀。", "查询结束")

        async def stream(self, *args):
            raise AssertionError("Agent reply with emotion tag must not need the legacy/director call")
            yield ""

    provider = AgentVoiceProvider()
    engine = FakeEngine()
    service, voice, _ = build_service(db, tmp_path, provider, engine)
    service.agent_settings = lambda: AgentSettings(enabled=True)
    request = chat_request_factory(text="我今天升职了", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    generation_id = await run_turn(service, voice, actor, request)
    content = db.all("events", role="assistant")[0]["content"]
    assert content["text"] == "今天好开心呀。"
    assert content["voice"]["emotion"] == "开心"
    assert content["voice"]["emotion_source"] == "model_tag"
    assert content["voice"]["state"] == "ready"
    assert len([item for item in content["attachments"] if item["kind"] == "audio"]) == 1
    events = service.replay(actor, generation_id)
    text = "".join(event.payload["text"] for event in events if event.type == "text.delta")
    assert text == "今天好开心呀。"
    assert any(event.type == "tool.completed" for event in events)
    assert len(provider.calls) == 2
    assert len(engine.calls) == 1 and "带笑意" in engine.calls[0][2]


async def test_each_model_line_gets_its_own_voice_segment_in_order(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider(reply="今天好开心呀。\n我也很高兴。")
    engine = FakeEngine()
    service, voice, _ = build_service(db, tmp_path, provider, engine)
    request = chat_request_factory(text="我今天升职了", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    generation_id = await run_turn(service, voice, actor, request)

    rows = db.all("speech_segments")
    assert [row["segment_index"] for row in rows] == [0, 1]
    assert [call[0] for call in engine.calls] == ["今天好开心呀。", "我也很高兴。"]
    content = db.all("events", role="assistant")[0]["content"]
    assert content["text"] == "今天好开心呀。\n我也很高兴。"
    assert content["segments"] == [
        {"index": 0, "kind": "voice", "text": "今天好开心呀", "segment_id": str(rows[0]["id"])},
        {"index": 1, "kind": "voice", "text": "我也很高兴", "segment_id": str(rows[1]["id"])},
    ]
    audio = [item for item in content["attachments"] if item["kind"] == "audio"]
    assert [item["segment_index"] for item in audio] == [0, 1]
    assert [item["emotion"] for item in audio] == ["开心", "开心"]
    events = service.replay(actor, generation_id)
    segment_events = [event for event in events if event.type == "text.segment"]
    assert [event.payload["kind"] for event in segment_events] == ["voice", "voice"]
    assert [event.payload["index"] for event in segment_events] == [0, 1]
    status = voice.speech_status(generation_id)
    assert status["state"] == "ready"
    assert [item["segment_index"] for item in status["segments"]] == [0, 1]
    assert all(item["attachment"]["segment_index"] == item["segment_index"] for item in status["segments"])


async def test_auto_voice_is_decided_once_per_turn(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider(reply="今天好开心呀。\n我也很高兴。")
    engine = FakeEngine()
    service, voice, _ = build_service(
        db, tmp_path, provider, engine, probability=0.5, random_value=lambda: 0.0
    )
    request = chat_request_factory(text="我今天升职了", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "auto"
    generation_id = await run_turn(service, voice, actor, request)
    content = db.all("events", role="assistant")[0]["content"]
    assert [item["kind"] for item in content["segments"]] == ["voice", "voice"]
    assert db.count("speech_segments") == 2
    assert [call[0] for call in engine.calls] == ["今天好开心呀。", "我也很高兴。"]
    events = service.replay(actor, generation_id)
    assert [event.payload["kind"] for event in events if event.type == "text.segment"] == ["voice", "voice"]


async def test_voice_turn_can_switch_to_text_after_two_voice_segments(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider(reply="第一句呀。\n第二句呀。\n第三句呀。\n第四句呀。")
    engine = FakeEngine()
    values = iter([0.0, 0.0, 0.0])
    service, voice, _ = build_service(
        db,
        tmp_path,
        provider,
        engine,
        probability=1.0,
        random_value=lambda: next(values),
        text_break_probability=1.0,
    )
    request = chat_request_factory(text="今天好开心", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "auto"
    generation_id = await run_turn(service, voice, actor, request)
    content = db.all("events", role="assistant")[0]["content"]
    assert [item["kind"] for item in content["segments"]] == ["voice", "voice", "text", "voice"]
    assert db.count("speech_segments") == 3
    assert [call[0] for call in engine.calls] == ["第一句呀。", "第二句呀。", "第四句呀。"]
    events = service.replay(actor, generation_id)
    assert [event.payload["kind"] for event in events if event.type == "text.segment"] == ["voice", "voice", "text", "voice"]


async def test_stale_queued_speech_is_reported_and_cleaned(db, actor, seeded_scope, archive, chat_request_factory, tmp_path):
    from datetime import datetime, timedelta, timezone

    from nagi.archive import models as m
    from nagi.delivery import models as dm

    request = chat_request_factory(text="静默测试", conversation_id=seeded_scope.conversation_id)
    accepted = await archive.accept(actor, request)
    await archive.append_output(accepted.generation_id, "好的。", final_state="complete")
    with db.engine.begin() as conn:
        conn.execute(
            m.generations.update()
            .where(m.generations.c.id == accepted.generation_id)
            .values(state="completed", finished_at=datetime.now(timezone.utc) - timedelta(seconds=10))
        )
        conn.execute(
            dm.speech_segments.insert().values(
                generation_id=accepted.generation_id,
                epoch=1,
                segment_index=0,
                display_text="好的。",
                spoken_text="好的。",
                voice_id="yuwan",
                voice_version="1",
                state="queued",
            )
        )
    settings = VoiceSettings(enabled=True, probability=1.0, voice_id="yuwan")
    voice = VoiceService(db, AssetStore(db, tmp_path / "assets"), lambda: settings, engine=FakeEngine())
    status = voice.speech_status(accepted.generation_id)
    assert status["state"] == "failed" and status["error_code"] == "SPEECH_INTERRUPTED"
    voice.cleanup_on_startup()
    segment = db.all("speech_segments")[0]
    assert segment["state"] == "failed" and segment["error_code"] == "PROCESS_RESTART"


async def test_pending_speech_is_not_stale_while_a_sibling_segment_is_active(db, actor, seeded_scope, archive, chat_request_factory, tmp_path):
    from datetime import datetime, timedelta, timezone

    from nagi.archive import models as m
    from nagi.delivery import models as dm

    request = chat_request_factory(text="并发语音测试", conversation_id=seeded_scope.conversation_id)
    accepted = await archive.accept(actor, request)
    await archive.append_output(accepted.generation_id, "第一句。第二句。", final_state="complete")
    with db.engine.begin() as conn:
        conn.execute(
            m.generations.update()
            .where(m.generations.c.id == accepted.generation_id)
            .values(state="completed", finished_at=datetime.now(timezone.utc) - timedelta(seconds=10))
        )
        for index in (0, 1):
            conn.execute(
                dm.speech_segments.insert().values(
                    generation_id=accepted.generation_id,
                    epoch=1,
                    segment_index=index,
                    display_text="第" + str(index + 1) + "句。",
                    spoken_text="第" + str(index + 1) + "句。",
                    voice_id="yuwan",
                    voice_version="1",
                    state="queued",
                )
            )
    settings = VoiceSettings(enabled=True, probability=1.0, voice_id="yuwan")
    voice = VoiceService(db, AssetStore(db, tmp_path / "assets"), lambda: settings, engine=FakeEngine())
    # One TTS task for this generation is still running; the other queued row must not be
    # reported as SPEECH_INTERRUPTED just because a sibling task has already finished.
    voice._active[accepted.generation_id] = 1
    status = voice.speech_status(accepted.generation_id)
    assert status["state"] == "queued"
    assert all(item["state"] == "queued" for item in status["segments"])


async def test_interrupt_cancels_in_flight_voice(db, actor, seeded_scope, chat_request_factory, tmp_path):
    provider = VoiceProvider()
    engine = BlockingEngine()
    service, voice, _ = build_service(db, tmp_path, provider, engine)
    request = chat_request_factory(text="今天好开心", conversation_id=seeded_scope.conversation_id)
    request.output.voice = "always"
    accepted = await service.submit(actor, request)
    run_task = asyncio.create_task(service.run(accepted.generation_id))
    await asyncio.wait_for(engine.started.wait(), timeout=5)
    await service.interrupt(actor, accepted.generation_id, uuid4())
    engine.release.set()
    assert await run_task
    await voice.wait_for_idle()
    segment = db.all("speech_segments")[0]
    assert segment["state"] == "cancelled"
    content = db.all("events", role="assistant")[0]["content"]
    assert content["voice"]["state"] == "cancelled"
    assert voice.speech_status(accepted.generation_id)["state"] == "cancelled"
    assert db.count("assets") == 0
