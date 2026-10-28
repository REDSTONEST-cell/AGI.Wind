"""CLI 单元测试：不依赖真实 PostgreSQL / 网络，全部使用临时目录与 monkeypatch。"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from nagi import cli


def write_config(root: Path, payload: dict) -> Path:
    config = root / "config" / "app.local.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return config


def read_config(root: Path) -> dict:
    return yaml.safe_load((root / "config" / "app.local.yaml").read_text(encoding="utf-8")) or {}


def test_version_prints_name_and_python(capsys):
    code = cli.main(["--root", str(Path.cwd()), "version"])
    assert code == 0
    output = capsys.readouterr().out
    assert "nagi" in output
    assert "Python" in output


def test_help_lists_every_top_level_command(capsys):
    code = cli.main(["--root", str(Path.cwd()), "help"])
    assert code == 0
    output = capsys.readouterr().out
    for command in ("init", "run", "status", "doctor", "conf", "password", "db", "logs", "version"):
        assert command in output


def test_unknown_command_exits_2(capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--root", str(Path.cwd()), "definitely-not-a-command"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_init_creates_working_directory_config_and_token(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_init_database", lambda root: None)
    code = cli.init_command(tmp_path, argparse.Namespace(skip_database=False))
    assert code == 0
    assert (tmp_path / "data" / "access-token.txt").is_file()
    config = read_config(tmp_path)
    assert Path(config["data_dir"]).name == "data"
    assert config["proactive"] == {"enabled": False}
    assert config["runtime"]["preferred"] == "direct_vendor"


def test_init_skip_database_does_not_call_database(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_init_database", lambda root: called.append(root))
    code = cli.init_command(tmp_path, argparse.Namespace(skip_database=True))
    assert code == 0
    assert called == []
    assert (tmp_path / "config" / "app.local.yaml").is_file()


def test_conf_set_then_get_roundtrip(tmp_path, capsys):
    write_config(tmp_path, {"voice": {"enabled": False}})
    assert cli.conf_set_command(tmp_path, argparse.Namespace(key="voice.enabled", value="true")) == 0
    assert read_config(tmp_path)["voice"]["enabled"] is True
    capsys.readouterr()
    assert cli.conf_get_command(tmp_path, argparse.Namespace(key="voice.enabled")) == 0
    assert "true" in capsys.readouterr().out


def test_conf_set_rejects_invalid_value_and_keeps_file(tmp_path, capsys):
    write_config(tmp_path, {"voice": {"enabled": False}})
    code = cli.conf_set_command(tmp_path, argparse.Namespace(key="voice.enabled", value="not-a-bool"))
    assert code == 1
    assert read_config(tmp_path)["voice"]["enabled"] is False
    assert "配置校验失败" in capsys.readouterr().err


def test_conf_rejects_secret_keys(tmp_path, capsys):
    write_config(tmp_path, {})
    code = cli.conf_set_command(tmp_path, argparse.Namespace(key="client_key", value="hunter22"))
    assert code == 1
    assert "password" in capsys.readouterr().err

    assert cli.conf_get_command(tmp_path, argparse.Namespace(key="access_token")) == 1


def test_conf_get_missing_key_returns_1(tmp_path, capsys):
    write_config(tmp_path, {"voice": {"enabled": False}})
    assert cli.conf_get_command(tmp_path, argparse.Namespace(key="voice.voice_id")) == 1
    assert "未设置" in capsys.readouterr().out


def test_conf_list_hides_secrets(tmp_path, capsys):
    write_config(
        tmp_path,
        {
            "voice": {"enabled": True, "base_url": "http://127.0.0.1:8137"},
            "client_key": "synthetic-private-key",
            "access_token": "synthetic-private-token",
        },
    )
    assert cli.conf_list_command(tmp_path, argparse.Namespace()) == 0
    output = capsys.readouterr().out
    assert "voice.enabled = true" in output
    assert "synthetic-private" not in output


def test_password_updates_client_key_without_printing_it(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, {"data_dir": str(tmp_path / "data")})
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "correct-horse-battery")
    code = cli._password_interactive(tmp_path)
    assert code == 0
    assert read_config(tmp_path)["client_key"] == "correct-horse-battery"
    captured = capsys.readouterr()
    assert "correct-horse-battery" not in captured.out + captured.err
    assert "访问密码已更新" in captured.out


def test_password_rejects_short_and_mismatched(tmp_path, monkeypatch, capsys):
    write_config(tmp_path, {})
    answers = iter(["short", "short"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli._password_interactive(tmp_path) == 1
    assert "至少" in capsys.readouterr().err

    answers = iter(["long-enough-pass", "different-pass"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli._password_interactive(tmp_path) == 1
    assert "不一致" in capsys.readouterr().err
    assert "client_key" not in read_config(tmp_path)


def test_status_reports_live_gateway(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "gateway-port.txt").write_text("18001", encoding="utf-8")

    monkeypatch.setattr(cli, "_port_listening", lambda port: True)

    def fake_http(url, timeout=2.0):
        if url.endswith("/health/live"):
            return 200, {"status": "live"}, None
        return 200, {"status": "ready", "dependencies": {"database": "available", "dialogue": "configured"}}, None

    monkeypatch.setattr(cli, "_http_json", fake_http)
    assert cli.status_command(tmp_path, argparse.Namespace(port=None)) == 0
    output = capsys.readouterr().out
    assert "运行中" in output
    assert "18001" in output
    assert "database=available" in output


def test_status_reports_stopped_gateway(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_port_listening", lambda port: False)
    assert cli.status_command(tmp_path, argparse.Namespace(port=18002)) == 1
    assert "未运行" in capsys.readouterr().out


def test_logs_redacts_configured_secret(tmp_path, capsys):
    write_config(
        tmp_path,
        {"data_dir": str(tmp_path / "data"), "client_key": "super-secret-value"},
    )
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "gateway.stdout.log").write_text("启动 super-secret-value 完成\n", encoding="utf-8")
    assert cli.logs_command(tmp_path, argparse.Namespace(follow=False, tail=50)) == 0
    output = capsys.readouterr().out
    assert "super-secret-value" not in output
    assert "[REDACTED]" in output


def test_doctor_reports_failures_and_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_doctor_collect",
        lambda root, args: [
            ("ok", "Python 版本", "3.12.14"),
            ("fail", "数据库", "无法连接：synthetic-failure"),
        ],
    )
    assert cli.doctor_command(tmp_path, argparse.Namespace(port=None)) == 1
    output = capsys.readouterr().out
    assert "1 项失败" in output
    assert "数据库" in output


def test_doctor_provider_presence_never_prints_secret(tmp_path):
    from nagi.core.secrets import SecretStore
    from nagi.core.settings import Settings

    settings = Settings.model_validate(
        {
            "data_dir": str(tmp_path),
            "providers": {
                "vendor": {
                    "base_url": "https://vendor.invalid/v1",
                    "credential_ref": "VENDOR_KEY",
                }
            },
        }
    )
    store = SecretStore(tmp_path)
    store.set("VENDOR_KEY", "synthetic-provider-secret")
    items = cli._doctor_providers(settings, tmp_path)
    text = " ".join(f"{title}:{detail}" for _, title, detail in items)
    assert "已配置" in text
    assert "synthetic-provider-secret" not in text
    assert any(level == "warn" for level, title, _ in items if "对话模型" in title)
