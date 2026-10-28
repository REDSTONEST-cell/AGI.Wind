from pydantic import Field, field_validator

from ..core.types import DTO


# Common password managers / private windows. Matching is case-insensitive. These are
# only defaults; the owner can edit the persisted list.
DEFAULT_EXCLUDED_PROCESSES = [
    "1password.exe",
    "bitwarden.exe",
    "keepass.exe",
    "keepassxc.exe",
    "lastpass.exe",
    "authenticator.exe",
    "authy.exe",
]

DEFAULT_EXCLUDED_TITLE_KEYWORDS = [
    "密码",
    "密码管理器",
    "银行",
    "支付",
    "验证码",
    "身份验证",
    "登录密码",
    "private browsing",
    "incognito",
    "inprivate",
    "无痕",
    "1password",
    "bitwarden",
    "keepass",
]


class AmbientSettings(DTO):
    """Observe the foreground desktop; disabled until the owner explicitly enables it.

    Screenshots are of the foreground window only, never the whole desktop. Sensitive
    windows are redacted before anything is written to the database.
    """

    enabled: bool = False
    sample_seconds: int = Field(default=60, ge=10, le=600)
    screenshot_seconds: int = Field(default=120, ge=60, le=1800)
    capture_screenshots: bool = True
    screenshot_max_width: int = Field(default=1280, ge=480, le=2560)
    max_screenshots_per_hour: int = Field(default=12, ge=1, le=60)
    retention_hours: int = Field(default=24, ge=1, le=168)
    share_with_model: bool = True
    excluded_processes: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_PROCESSES))
    excluded_title_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_TITLE_KEYWORDS))

    @field_validator("excluded_processes", "excluded_title_keywords")
    @classmethod
    def clean_exclusions(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = str(item or "").strip()
            key = text.casefold()
            if not text or len(text) > 120 or key in seen:
                continue
            seen.add(key)
            result.append(text)
        return result[:40]
