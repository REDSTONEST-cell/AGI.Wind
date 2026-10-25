from typing import Protocol, AsyncIterator, Literal
from dataclasses import dataclass
from ..core.types import ModelRef


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolTurn:
    text: str = ''
    reasoning: str = ''
    tool_calls: tuple[ToolCall, ...] = ()

@dataclass(frozen=True)
class ProviderChunk:
    kind: Literal['text', 'reasoning']
    text: str


class TextProvider(Protocol):
    def stream(self, model: ModelRef, messages: list[dict]) -> AsyncIterator[ProviderChunk]: ...
