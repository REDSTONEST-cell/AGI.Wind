from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Actor(DTO):
    account_id: UUID
    user_id: UUID


class Scope(Actor):
    companion_id: UUID
    conversation_id: UUID


class ModelRef(DTO):
    provider_id: str
    model_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    config_version: int = Field(default=1, ge=1)


class SourceRef(DTO):
    event_id: UUID
    json_pointer: str = "/text"
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, ge=0)
    quote_hash: str | None = None

    @model_validator(mode="after")
    def offsets(self):
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("source offsets must be paired")
        if self.start_offset is not None and self.end_offset <= self.start_offset:
            raise ValueError("source offsets must be increasing")
        if not self.json_pointer.startswith("/"):
            raise ValueError("source pointer must start with /")
        return self


class ContentPart(DTO):
    type: Literal["text", "image", "audio"]
    text: str | None = None
    asset_id: UUID | None = None


class ChatInput(DTO):
    text: str = Field(default="", max_length=48000)
    asset_ids: list[UUID] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def not_empty(self):
        if not self.text.strip() and not self.asset_ids:
            raise ValueError("input must contain text or an asset")
        return self


class OutputOptions(DTO):
    text: bool = True
    audio: bool = False
    avatar: bool = False


class ChatRequest(DTO):
    request_id: UUID
    conversation_id: UUID
    input: ChatInput
    output: OutputOptions = Field(default_factory=OutputOptions)
    device_id: str = Field(min_length=1, max_length=128)


class AcceptedRequest(DTO):
    request_id: UUID
    generation_id: UUID
    epoch: int = Field(ge=1)
    events_url: str


class PersonaSnapshot(DTO):
    companion_id: UUID
    version: int = Field(ge=1)
    core: dict[str, Any]
    style: dict[str, Any]
    relationship: dict[str, Any]
    commitments: list[dict[str, Any]]


EpistemicType = Literal["user_asserted", "observed", "assistant_generated", "inferred", "fictional"]


class MemoryHit(DTO):
    memory_id: UUID | None = None
    chunk_id: UUID | None = None
    content: str
    score: float
    epistemic_type: EpistemicType
    source_refs: list[SourceRef]
    time_range: tuple[datetime, datetime] | None = None
    status: Literal["active", "historical"] = "active"


class ContextBundle(DTO):
    memory_revision: int
    persona_snapshot: PersonaSnapshot
    recent_events: list[dict[str, Any]]
    memories: list[MemoryHit]
    current_input: ChatInput
    vision_observations: list[dict[str, Any]]
    token_estimate: int
    selected_source_ids: list[UUID]


class RuntimeRequest(DTO):
    scope: Scope
    generation_id: UUID
    epoch: int
    memory_revision: int
    model_ref: ModelRef
    persona_snapshot: PersonaSnapshot
    recent_events: list[dict[str, Any]]
    memories: list[MemoryHit]
    current_input: ChatInput
    vision_observations: list[dict[str, Any]]


class RuntimeDelta(DTO):
    generation_id: UUID
    epoch: int
    kind: Literal["text", "tool", "end", "error"]
    payload: dict[str, Any]


class RuntimeCapabilities(DTO):
    text: bool = False
    image: bool = False
    streaming: bool = False
    cancel: bool = False
    model_switch: bool = False
    context_injection: bool = False
    controlled_tools: bool = False
    local_state: bool = False
