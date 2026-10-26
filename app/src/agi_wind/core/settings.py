from pathlib import Path
import json
from threading import RLock
from typing import Literal
from urllib.parse import urlparse
from pydantic import Field, SecretStr, field_validator, model_validator
import yaml
from .types import DTO, ModelRef
from ..agent.settings import AgentSettings

SETTINGS_WRITE_LOCK = RLock()


class ProviderSettings(DTO):
    base_url: str
    credential_ref: str
    protocol: Literal["openai_compatible", "vendor_native"] = "openai_compatible"
    supported_parameters: list[str] = Field(default_factory=list)
    capabilities: list[Literal["text", "image", "streaming", "embedding", "asr", "tts", "rerank"]] = Field(default_factory=lambda: ["text", "streaming"])
    timeout_seconds: float = Field(default=60, gt=0, le=600)


class ModelRoles(DTO):
    dialogue: ModelRef | None = None
    vision: ModelRef | None = None
    memory_extract: ModelRef | None = None
    memory_reflect: ModelRef | None = None
    embedding: ModelRef | None = None
    asr: ModelRef | None = None
    tts: ModelRef | None = None
    rerank: ModelRef | None = None


class VoiceSettings(DTO):
    """Local Breeze-TTS settings. Disabled by default so tests never touch the GPU."""
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8137"
    voice_id: str = "yuwan"
    voice_version: str = "1"
    probability: float = Field(default=0.3, ge=0, le=1)
    # A voice turn normally stays voice; after two voiced segments an auto-mode turn may
    # send one text segment instead, so the rhythm reads more like a real chat partner.
    text_break_probability: float = Field(default=0.35, ge=0, le=1)
    max_chars: int = Field(default=400, ge=20, le=1000)
    timeout_seconds: float = Field(default=180, gt=0, le=600)

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("voice base_url must be http(s) without embedded credentials")
        return value

    @field_validator("voice_id")
    @classmethod
    def valid_voice_id(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 64 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in value):
            raise ValueError("voice_id must use letters, digits, dot, underscore or hyphen")
        return value


class RuntimeSettings(DTO):
    preferred: Literal["letta", "direct_vendor", "development_fake"] = "letta"
    fallback_policy: Literal["explicit_admin_selection"] = "explicit_admin_selection"


class ProactiveSettings(DTO):
    """Bounded idle-time messages. Off by default; every limit is server-enforced."""

    enabled: bool = False
    min_idle_minutes: int = Field(default=120, ge=5, le=1440)
    min_gap_minutes: int = Field(default=180, ge=5, le=1440)
    max_per_day: int = Field(default=3, ge=1, le=20)
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"

    @field_validator("quiet_start", "quiet_end")
    @classmethod
    def clock(cls, value: str) -> str:
        value = str(value or "").strip()
        if len(value) != 5 or value[2] != ":" or not (value[:2] + value[3:]).isdigit():
            raise ValueError("quiet hours must use HH:MM")
        hour, minute = int(value[:2]), int(value[3:])
        if hour > 23 or minute > 59:
            raise ValueError("quiet hours must use a valid HH:MM time")
        return value


class Settings(DTO):
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    models: ModelRoles = Field(default_factory=ModelRoles)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    proactive: ProactiveSettings = Field(default_factory=ProactiveSettings)
    database_url: SecretStr = SecretStr("postgresql+psycopg://agi@127.0.0.1:55432/agi_wind")
    access_token: SecretStr | None = None
    data_dir: Path = Path("../data")
    timezone: str = "Asia/Shanghai"
    max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_audio_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    embedding_device: Literal["cpu"] = "cpu"
    asr_device: Literal["cpu"] = "cpu"
    # Loopback-only automatic browser session: the gateway binds 127.0.0.1 and a local
    # single user should not have to paste the access token. Non-loopback clients still
    # need the token (see api.app.is_loopback).
    local_auto_login: bool = True
    # Server/client split: the gateway can bind a LAN interface and a standalone client
    # authenticates with this shared key (Bearer for cross-origin, key-session for
    # same-origin cookies). The default is intentional for local testing only.
    host: str = "127.0.0.1"
    client_key: SecretStr = SecretStr("123456")
    allowed_origins: list[str] = Field(default_factory=list)
    # Phase-1 memory extraction is enabled by default but is asynchronous and never blocks a
    # chat turn. memory_extract falls back to the dialogue model when no dedicated role exists.
    memory_extraction_enabled: bool = True
    memory_extract_batch_events: int = Field(default=8, ge=1, le=32)
    memory_extract_max_candidates: int = Field(default=8, ge=1, le=8)
    memory_extract_timeout_seconds: float = Field(default=30, gt=0, le=300)
    memory_maintenance_enabled: bool = True
    memory_maintenance_hours: float = Field(default=6, gt=0, le=720)
    memory_semantic_enabled: bool = True
    memory_retention_days: dict[str, float] = Field(default_factory=dict)
    memory_daily_budget_usd: float = Field(default=0.05, ge=0, le=100)
    memory_daily_token_limit: int = Field(default=200000, ge=0, le=100_000_000)
    memory_input_price_per_million: float = Field(default=0.3, ge=0, le=1000)
    memory_output_price_per_million: float = Field(default=1.2, ge=0, le=1000)
    memory_extract_max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    memory_consolidation_enabled: bool = True
    memory_worker_mode: Literal['gateway', 'separate'] = 'gateway'
    # estimate_tokens counts UTF-8 bytes; Chinese persona text is ~3x its character count.
    # 48000 leaves room for a long persona plus a normal turn without every request failing.
    context_budget_bytes: int = Field(default=48000, ge=1000, le=4_000_000)
    input_budget_bytes: int = Field(default=12000, ge=100, le=1_000_000)

    @model_validator(mode="after")
    def supported_models(self):
        allowed_kinds = {'episode','plan','fact','relationship','preference','commitment','identity','other'}
        if any(kind not in allowed_kinds or not 1 <= days <= 3650 for kind, days in self.memory_retention_days.items()):
            raise ValueError('memory_retention_days requires known kinds and 1..3650 days')
        for role in type(self.models).model_fields:
            model = getattr(self.models, role)
            if model is None or (role == "embedding" and model.provider_id == "local"):
                continue
            if model.provider_id not in self.providers:
                raise ValueError("unknown provider for model role: " + role)
            supported = self.providers[model.provider_id].supported_parameters
            if set(model.parameters) - set(supported):
                raise ValueError("unsupported parameter for model role: " + role)
        return self


def load_settings(path: str | Path) -> Settings:
    with Path(path).open(encoding="utf-8") as stream:
        return Settings.model_validate(yaml.safe_load(stream) or {})


def persisted_settings_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "settings.json"


MEMORY_SETTING_FIELDS = (
    "memory_extraction_enabled",
    "memory_extract_batch_events",
    "memory_extract_max_candidates",
    "memory_extract_timeout_seconds",
    "memory_maintenance_enabled",
    "memory_maintenance_hours",
    "memory_semantic_enabled", "memory_retention_days", "memory_daily_budget_usd",
    "memory_daily_token_limit", "memory_input_price_per_million", "memory_output_price_per_million",
    "memory_extract_max_output_tokens", "memory_consolidation_enabled", "memory_worker_mode",
)


def apply_persisted_settings(settings: Settings, data_dir: str | Path | None = None) -> Settings:
    """Overlay provider/model choices saved from the client settings page onto the YAML baseline.

    The file contains configuration only: API keys live in SecretStore and never appear here.
    """
    path = persisted_settings_path(data_dir or settings.data_dir)
    if not path.is_file():
        return settings
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    values = settings.model_dump()
    providers = values.get("providers", {})
    providers.update(payload.get("providers", {}))
    values["providers"] = providers
    models = values.get("models", {})
    models.update(payload.get("models", {}))
    values["models"] = models
    voice = values.get("voice", {})
    voice.update(payload.get("voice", {}))
    values["voice"] = voice
    agent = values.get("agent", {})
    agent.update(payload.get("agent", {}))
    values["agent"] = agent
    proactive = values.get("proactive", {})
    proactive.update(payload.get("proactive", {}))
    values["proactive"] = proactive
    for field in MEMORY_SETTING_FIELDS:
        if field in payload.get("memory", {}):
            values[field] = payload["memory"][field]
    merged = Settings.model_validate(values)
    for name in type(settings).model_fields:
        setattr(settings, name, getattr(merged, name))
    return settings


def persist_settings(settings: Settings, data_dir: str | Path | None = None) -> None:
    """Write providers/models (no secrets) so a restart keeps the configured runtime."""
    with SETTINGS_WRITE_LOCK:
        _persist_settings(settings, data_dir)


def _persist_settings(settings: Settings, data_dir: str | Path | None = None) -> None:
    path = persisted_settings_path(data_dir or settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "providers": {key: value.model_dump(mode="json") for key, value in settings.providers.items()},
        "models": {
            role: (None if getattr(settings.models, role) is None else getattr(settings.models, role).model_dump(mode="json"))
            for role in type(settings.models).model_fields
        },
        "memory": {field: getattr(settings, field) for field in MEMORY_SETTING_FIELDS},
        "voice": settings.voice.model_dump(mode="json"),
        "agent": settings.agent.model_dump(mode="json"),
        "proactive": settings.proactive.model_dump(mode="json"),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    temporary.replace(path)
