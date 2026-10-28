from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from ..core.types import DTO

SEARCH_SECRET_REF = 'NAGI_AGENT_SEARCH'


def search_secret_ref(backend):
    # Switching backend must never forward a Brave key to a SearXNG instance.
    return SEARCH_SECRET_REF + '_' + backend.upper()


class AgentSettings(DTO):
    enabled: bool = False
    memory_search_enabled: bool = True
    sticker_search_enabled: bool = True
    sticker_send_enabled: bool = True
    web_search_enabled: bool = False
    file_access_mode: Literal['disabled', 'ask', 'full'] = 'ask'
    command_access_mode: Literal['disabled', 'ask', 'full'] = 'disabled'
    command_timeout_seconds: float = Field(default=30, ge=1, le=300)
    command_max_output_chars: int = Field(default=8000, ge=500, le=16000)
    max_rounds: int = Field(default=3, ge=1, le=8)
    max_tool_calls: int = Field(default=6, ge=1, le=16)
    tool_timeout_seconds: float = Field(default=15, gt=0, le=60)
    total_timeout_seconds: float = Field(default=90, ge=1, le=300)
    max_result_chars: int = Field(default=6000, ge=500, le=16000)
    search_backend: Literal['searxng', 'brave', 'deepseek'] = 'searxng'
    search_url: str = Field(default='', max_length=2000)
    search_max_results: int = Field(default=5, ge=1, le=8)

    @field_validator('search_url')
    @classmethod
    def search_endpoint(cls, value):
        value = value.strip().rstrip('/')
        if value:
            url = urlsplit(value)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('search URL must be an HTTP(S) endpoint without credentials, query or fragment')
        return value
