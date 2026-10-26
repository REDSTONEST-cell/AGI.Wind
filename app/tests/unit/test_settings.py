from copy import deepcopy
import json
import pytest
from pydantic import ValidationError


def config():
    return {
        "providers": {"vendor": {"base_url": "https://vendor.invalid/v1", "credential_ref": "VENDOR_KEY", "supported_parameters": ["temperature", "max_output_tokens"]}},
        "models": {"dialogue": {"provider_id": "vendor", "model_id": "configure-me", "parameters": {"temperature": 0.5}}},
    }


def test_unknown_model_role_is_rejected():
    from agi_wind.core.settings import Settings
    value = config()
    value["models"]["secret_role"] = value["models"]["dialogue"]
    with pytest.raises(ValidationError):
        Settings.model_validate(value)


def test_unsupported_parameter_is_actionable():
    from agi_wind.core.settings import Settings
    value = config()
    value["models"]["dialogue"]["parameters"]["reasoning_effort"] = "high"
    with pytest.raises(ValidationError, match="unsupported parameter"):
        Settings.model_validate(value)


def test_secrets_never_appear_in_dump_repr_or_error():
    from agi_wind.core.settings import Settings
    value = config()
    value["access_token"] = "synthetic-private-token"
    value["database_url"] = "postgresql+psycopg://user:synthetic-password@127.0.0.1/test"
    settings = Settings.model_validate(value)
    assert "synthetic-private" not in settings.model_dump_json() + repr(settings)
    assert "synthetic-password" not in settings.model_dump_json() + repr(settings)
    value["max_image_bytes"] = "synthetic-private-token"
    with pytest.raises(ValidationError) as error:
        Settings.model_validate(value)
    assert "synthetic-private-token" not in str(error.value)


def test_credential_reference_not_value_is_loaded(tmp_path, monkeypatch):
    from agi_wind.core.settings import load_settings
    import yaml
    monkeypatch.setenv("VENDOR_KEY", "synthetic-key")
    path = tmp_path / "app.yaml"
    path.write_text(yaml.safe_dump(config()), encoding="utf-8")
    settings = load_settings(path)
    assert settings.providers["vendor"].credential_ref == "VENDOR_KEY"
    assert "synthetic-key" not in settings.model_dump_json()


def test_unknown_provider_and_unsafe_runtime_fallback_are_rejected():
    from agi_wind.core.settings import Settings
    value = config()
    value["models"]["dialogue"]["provider_id"] = "missing"
    with pytest.raises(ValidationError, match="unknown provider"):
        Settings.model_validate(value)
    value = config()
    value["runtime"] = {"preferred": "letta", "fallback_policy": "silent"}
    with pytest.raises(ValidationError):
        Settings.model_validate(value)
