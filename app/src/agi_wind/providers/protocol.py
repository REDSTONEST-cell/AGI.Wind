from typing import Protocol, AsyncIterator
from ..core.types import ModelRef

class TextProvider(Protocol):
    def stream(self, model: ModelRef, messages: list[dict]) -> AsyncIterator[str]: ...
