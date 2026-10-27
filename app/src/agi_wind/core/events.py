from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4
from pydantic import Field
from .types import DTO


class AudioSegment(DTO):
    generation_id: UUID
    epoch: int = Field(ge=1)
    segment_id: UUID
    segment_index: int = Field(ge=0)
    text: str
    voice_id: str
    voice_version: str
    asset_id: UUID
    sample_rate: int = Field(gt=0)
    channels: int = Field(ge=1, le=8)
    duration_ms: int = Field(gt=0)


class AvatarCue(DTO):
    generation_id: UUID
    epoch: int = Field(ge=1)
    segment_id: UUID | None = None
    expression: str = "neutral"
    motion: str = "idle"
    intensity: float = Field(default=1, ge=0, le=1)
    duration_ms: int = Field(default=1000, ge=0)
    sync: Literal["immediate", "segment_start", "segment_end"] = "immediate"


class StreamEvent(DTO):
    protocol_version: Literal["1.0"] = "1.0"
    event_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=1)
    type: Literal["accepted", "memory.context", "text.delta", "text.segment", "text.completed", "audio.segment", "speech.queued", "avatar.cue", "generation.completed", "generation.cancelled", "generation.failed", "warning", "reasoning.delta", "reasoning.completed", "tool.started", "tool.completed", "sticker.sent", "cognition.completed"]
    generation_id: UUID
    epoch: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
