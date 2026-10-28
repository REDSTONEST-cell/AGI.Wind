"""Unit tests for the optional memory-framework adapter interface (default off)."""
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agi_wind.memory import frameworks
from agi_wind.memory.frameworks import (
    FRAMEWORK_SETTINGS_ATTRIBUTES,
    FrameworkStatus,
    MemoryFrameworkConfig,
    MemoryFrameworkRegistry,
    framework_status,
)


class FakeAdapter:
    def __init__(self, name="fake", status=None, error=None):
        self.name = name
        self.closed = 0
        self._status = status
        self._error = error

    async def probe(self):
        if self._error is not None:
            raise self._error
        if self._status is not None:
            return self._status
        return FrameworkStatus(name=self.name, enabled=True, available=True, detail="ok")

    async def close(self):
        self.closed += 1


# ---------------------------------------------------------------- default off
def test_feature_flag_defaults_off_and_tolerates_missing_settings():
    config = MemoryFrameworkConfig()
    assert config.enabled is False
    assert config.adapter is None
    assert dict(config.options) == {}

    assert MemoryFrameworkConfig.from_mapping(None).enabled is False
    assert MemoryFrameworkConfig.from_mapping({}).enabled is False
    assert MemoryFrameworkConfig.from_mapping("garbage").enabled is False
    assert MemoryFrameworkConfig.from_mapping([1, 2]).enabled is False

    empty_settings = SimpleNamespace()
    assert MemoryFrameworkConfig.from_settings(empty_settings).enabled is False
    assert not hasattr(empty_settings, "memory_framework")
    assert FRAMEWORK_SETTINGS_ATTRIBUTES[0] == "memory_framework"


def test_config_parsing_and_secret_masking():
    config = MemoryFrameworkConfig.from_mapping({
        "enabled": True,
        "adapter": " letta ",
        "options": {
            "base_url": "http://localhost:8283",
            "api_token": "super-secret",
            "nested": {"password": "p", "model": "x"},
        },
    })
    assert config.enabled is True
    assert config.adapter == "letta"

    public = config.to_public_dict()
    assert public["enabled"] is True
    assert public["adapter"] == "letta"
    assert public["options"]["base_url"] == "http://localhost:8283"
    assert public["options"]["api_token"] == "***"
    assert public["options"]["nested"]["password"] == "***"
    assert public["options"]["nested"]["model"] == "x"

    settings = SimpleNamespace(memory_framework={"enabled": True, "adapter": "everos"})
    parsed = MemoryFrameworkConfig.from_settings(settings)
    assert parsed.enabled is True and parsed.adapter == "everos"

    alias = SimpleNamespace(memory_frameworks={"enabled": False, "adapter": "letta"})
    assert MemoryFrameworkConfig.from_settings(alias).enabled is False

    already = MemoryFrameworkConfig(enabled=True, adapter="fake")
    assert MemoryFrameworkConfig.from_settings(
        SimpleNamespace(memory_framework=already)) is already


def test_invalid_config_values_are_rejected():
    with pytest.raises(ValueError):
        MemoryFrameworkConfig(adapter="   ")
    with pytest.raises(ValueError):
        MemoryFrameworkConfig(options=["not-a-mapping"])


# ------------------------------------------------------------------- registry
def test_registry_create_is_inert_when_disabled():
    calls = []
    registry = MemoryFrameworkRegistry()
    registry.register("fake", lambda config: calls.append(config) or FakeAdapter())

    assert registry.create(MemoryFrameworkConfig()) is None
    assert registry.create(MemoryFrameworkConfig(enabled=False, adapter="fake")) is None
    assert calls == []
    assert registry.names == ("fake",)


def test_registry_reports_unconfigured_and_unknown_adapters():
    registry = MemoryFrameworkRegistry()

    with pytest.raises(frameworks.FrameworkError) as unconfigured:
        registry.create(MemoryFrameworkConfig(enabled=True))
    assert unconfigured.value.code == "FRAMEWORK_ADAPTER_UNCONFIGURED"
    assert unconfigured.value.status == 503

    with pytest.raises(frameworks.FrameworkError) as unknown:
        registry.create(MemoryFrameworkConfig(enabled=True, adapter="letta"))
    assert unknown.value.code == "FRAMEWORK_ADAPTER_UNKNOWN"

    with pytest.raises(frameworks.FrameworkError) as bad_name:
        registry.register("  ", lambda config: FakeAdapter())
    assert bad_name.value.code == "FRAMEWORK_ADAPTER_NAME_INVALID"

    with pytest.raises(frameworks.FrameworkError):
        registry.register("fake", "not-callable")


def test_registry_create_duplicate_guard_and_unregister():
    registry = MemoryFrameworkRegistry()
    created = []

    def factory(config):
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    registry.register("fake", factory)
    with pytest.raises(frameworks.FrameworkError) as duplicate:
        registry.register("fake", factory)
    assert duplicate.value.code == "FRAMEWORK_ADAPTER_DUPLICATE"

    adapter = registry.create(MemoryFrameworkConfig(enabled=True, adapter="fake"))
    assert adapter is created[0]
    assert registry.unregister("fake") is True
    assert registry.unregister("fake") is False
    assert registry.names == ()


# --------------------------------------------------------------------- status
async def test_framework_status_reports_disabled_without_creating_adapter():
    calls = []
    registry = MemoryFrameworkRegistry()
    registry.register("fake", lambda config: calls.append(config) or FakeAdapter())

    status = await framework_status(registry, MemoryFrameworkConfig())
    assert status.enabled is False
    assert status.available is False
    assert status.detail == "disabled"
    assert calls == []


async def test_framework_status_probes_adapter_and_closes_it():
    adapter = FakeAdapter()
    registry = MemoryFrameworkRegistry()
    registry.register("fake", lambda config: adapter)

    status = await framework_status(registry, MemoryFrameworkConfig(enabled=True,
                                                                   adapter="fake"))
    assert status.available is True
    assert status.name == "fake"
    assert adapter.closed == 1


async def test_framework_status_reports_failures_instead_of_raising():
    failing = FakeAdapter(error=RuntimeError("boom"))
    broken_registry = MemoryFrameworkRegistry()
    broken_registry.register("broken", lambda config: failing)

    status = await framework_status(broken_registry,
                                    MemoryFrameworkConfig(enabled=True, adapter="broken"))
    assert status.available is False
    assert status.enabled is True
    assert status.detail == "RuntimeError"
    assert failing.closed == 1

    unknown = await framework_status(MemoryFrameworkRegistry(),
                                     MemoryFrameworkConfig(enabled=True, adapter="letta"))
    assert unknown.available is False
    assert "未注册" in unknown.detail or "letta" in unknown.detail


# -------------------------------------------------------------- dependency-free
def test_importing_frameworks_pulls_in_no_framework_library():
    """A fresh interpreter importing the module must not drag in Letta/EverOS."""
    code = (
        "import sys\n"
        "import agi_wind.memory.frameworks as fw\n"
        "roots = {name.split('.')[0] for name in sys.modules}\n"
        "assert not ({'letta', 'everos', 'letta_client'} & roots), roots\n"
        "assert fw.MemoryFrameworkConfig().enabled is False\n"
    )
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
