"""Optional external memory-framework adapters (Letta/EverOS or any future runtime).

The integration is deliberately inert by default:

* nothing in this module imports a third-party framework, contacts a network
  service or registers an adapter;
* :class:`MemoryFrameworkConfig` defaults to ``enabled=False`` and
  :meth:`MemoryFrameworkRegistry.create` then returns ``None`` without even
  instantiating a factory;
* a future Letta/EverOS adapter only needs to implement the small
  :class:`MemoryFrameworkAdapter` protocol and be registered by the wiring layer.

Keeping the port in a separate module means the manual autobiography organizer
and the rest of memory work correctly with no framework installed.

Integration notes:
  * settings: add a ``memory_framework`` mapping (``{"enabled": bool, "adapter":
    str, "options": {...}}``) to ``core/settings.py`` and the persisted settings
    overlay; :meth:`MemoryFrameworkConfig.from_settings` already reads that
    attribute (and a few aliases) defensively, so wiring it before the settings
    change is safe.
  * app wiring: create one ``MemoryFrameworkRegistry`` on ``app.state``, register
    adapter factories from the composition root, and expose
    :func:`framework_status` on ``/api/v1/capabilities`` / settings endpoints.
  * secrets: adapter options may contain API tokens; use
    :meth:`MemoryFrameworkConfig.to_public_dict` for anything user-visible and
    keep keys/tokens in the existing SecretStore instead of plain options.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from ..core.errors import AppError

# Settings attribute names checked, in order, by ``from_settings``. The first
# non-None value wins. None of these attributes exist yet; the getattr fallback
# keeps this module usable before the settings change lands.
FRAMEWORK_SETTINGS_ATTRIBUTES = ("memory_framework", "memory_frameworks", "frameworks")

_SECRET_HINTS = ("token", "key", "secret", "password", "credential", "authorization")


class FrameworkError(AppError):
    """Domain error for framework configuration/adapter failures."""

    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(code, message, status)


def _mask(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): "***" if any(hint in str(key).lower() for hint in _SECRET_HINTS)
                else _mask(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask(item) for item in value]
    return value


@dataclass(frozen=True)
class MemoryFrameworkConfig:
    """Feature-flag + adapter selection. Defaults to disabled for every deployment."""

    enabled: bool = False
    adapter: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.adapter is not None:
            if not isinstance(self.adapter, str) or not self.adapter.strip():
                raise ValueError("adapter must be a non-empty string or None")
            object.__setattr__(self, "adapter", self.adapter.strip())
        if not isinstance(self.options, Mapping):
            raise ValueError("options must be a mapping")

    @classmethod
    def from_mapping(cls, raw: Any) -> "MemoryFrameworkConfig":
        """Tolerant parser used for persisted settings; unknown keys are ignored."""
        if raw is None:
            return cls()
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, Mapping):
            return cls()
        adapter = raw.get("adapter")
        adapter = str(adapter).strip() if adapter not in (None, "") else None
        options = raw.get("options")
        return cls(enabled=bool(raw.get("enabled", False)), adapter=adapter,
                   options=dict(options) if isinstance(options, Mapping) else {})

    @classmethod
    def from_settings(cls, settings: Any) -> "MemoryFrameworkConfig":
        """Read the flag from an optional settings attribute without requiring one."""
        for name in FRAMEWORK_SETTINGS_ATTRIBUTES:
            raw = getattr(settings, name, None)
            if raw is not None:
                return cls.from_mapping(raw)
        return cls()

    def to_public_dict(self) -> dict[str, Any]:
        """JSON-safe view with secret-looking option values masked."""
        return {
            "enabled": bool(self.enabled),
            "adapter": self.adapter,
            "options": _mask(dict(self.options)),
        }


@dataclass(frozen=True)
class FrameworkStatus:
    """Health report for one framework integration, safe to expose publicly."""

    name: str
    enabled: bool
    available: bool
    detail: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "available": self.available,
            "detail": self.detail,
        }


class MemoryFrameworkAdapter(Protocol):
    """Minimal lifecycle port a future Letta/EverOS adapter must implement.

    Feature-specific calls belong to capability protocols added later (the base
    stays small so a missing optional dependency is never imported by this
    module). ``probe`` must not raise for an ordinary "not configured" state;
    return ``available=False`` instead. Raise only for truly broken adapters; the
    registry converts that into ``available=False`` for status endpoints.
    """

    name: str

    async def probe(self) -> FrameworkStatus:
        ...

    async def close(self) -> None:
        ...


FrameworkFactory = Callable[[MemoryFrameworkConfig], MemoryFrameworkAdapter]


class MemoryFrameworkRegistry:
    """Explicit adapter registry; empty unless the composition root registers one."""

    def __init__(self):
        self._factories: dict[str, FrameworkFactory] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def register(self, name: str, factory: FrameworkFactory) -> None:
        if not isinstance(name, str) or not name.strip():
            raise FrameworkError("FRAMEWORK_ADAPTER_NAME_INVALID", "适配器名称不能为空", 422)
        if not callable(factory):
            raise FrameworkError("FRAMEWORK_ADAPTER_FACTORY_INVALID", "适配器工厂必须可调用", 422)
        key = name.strip()
        if key in self._factories:
            raise FrameworkError("FRAMEWORK_ADAPTER_DUPLICATE", f"适配器已注册：{key}", 409)
        self._factories[key] = factory

    def unregister(self, name: str) -> bool:
        return self._factories.pop(str(name).strip(), None) is not None

    def create(self, config: MemoryFrameworkConfig) -> MemoryFrameworkAdapter | None:
        """Return ``None`` when the feature flag is off; never imports a framework here."""
        if not isinstance(config, MemoryFrameworkConfig):
            config = MemoryFrameworkConfig.from_mapping(config)
        if not config.enabled:
            return None
        if not config.adapter:
            raise FrameworkError("FRAMEWORK_ADAPTER_UNCONFIGURED",
                                 "已启用记忆框架但未指定适配器", 503)
        factory = self._factories.get(config.adapter)
        if factory is None:
            known = ", ".join(self.names) or "无"
            raise FrameworkError("FRAMEWORK_ADAPTER_UNKNOWN",
                                 f"未注册的记忆框架适配器：{config.adapter}（已注册：{known}）", 503)
        adapter = factory(config)
        if adapter is None:
            raise FrameworkError("FRAMEWORK_ADAPTER_FACTORY_FAILED",
                                 f"记忆框架适配器工厂返回空：{config.adapter}", 503)
        return adapter


async def _close_quietly(adapter: MemoryFrameworkAdapter) -> None:
    close = getattr(adapter, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 - teardown must never mask the status result
        pass


async def framework_status(registry: MemoryFrameworkRegistry,
                           config: MemoryFrameworkConfig) -> FrameworkStatus:
    """One-shot probe with guaranteed cleanup; disabled/unconfigured is not an error."""
    try:
        adapter = registry.create(config)
    except FrameworkError as error:
        return FrameworkStatus(name="", enabled=bool(config.enabled),
                               available=False, detail=error.message)
    if adapter is None:
        return FrameworkStatus(name="", enabled=False, available=False,
                               detail="disabled")
    name = str(getattr(adapter, "name", config.adapter or ""))
    try:
        status = await adapter.probe()
        if not isinstance(status, FrameworkStatus):
            raise TypeError("probe must return FrameworkStatus")
        return status
    except Exception as error:  # noqa: BLE001 - status endpoint must stay up
        return FrameworkStatus(name=name, enabled=True, available=False,
                               detail=type(error).__name__)
    finally:
        await _close_quietly(adapter)
