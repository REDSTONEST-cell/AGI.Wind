from pathlib import Path
from typing import Literal
from pydantic import Field, SecretStr, model_validator
import yaml
from .types import DTO, ModelRef


class ProviderSettings(DTO):
    base_url: str
    credential_ref: str
    protocol: Literal["openai_compatible", "vendor_native"] = "openai_compatible"
    supported_parameters: list[str] = Field(default_factory=list)
    capabilities: list[Literal["text", "image", "streaming"]] = Field(default_factory=lambda: ["text", "streaming"])
    timeout_seconds: float = Field(default=60, gt=0, le=600)


class ModelRoles(DTO):
    dialogue: ModelRef | None = None
    vision: ModelRef | None = None
    memory_extract: ModelRef | None = None
    memory_reflect: ModelRef | None = None
    embedding: ModelRef | None = None


class RuntimeSettings(DTO):
    preferred: Literal["letta", "direct_vendor", "development_fake"] = "letta"
    fallback_policy: Literal["explicit_admin_selection"] = "explicit_admin_selection"


class Settings(DTO):
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    models: ModelRoles = Field(default_factory=ModelRoles)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    database_url: SecretStr = SecretStr("postgresql+psycopg://agi@127.0.0.1:55432/agi_wind")
    access_token: SecretStr | None = None
    data_dir: Path = Path("../data")
    timezone: str = "Asia/Shanghai"
    max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    max_audio_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    embedding_device: Literal["cpu"] = "cpu"
    asr_device: Literal["cpu"] = "cpu"
    proactive_enabled: bool = False

    @model_validator(mode="after")
    def supported_models(self):
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
