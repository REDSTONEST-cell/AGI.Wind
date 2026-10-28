"""CLI 进程级冒烟测试：通过子进程验证入口、帮助、未知命令与配置读写。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import yaml

APP_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = APP_ROOT.parent


def cli_env() -> dict[str, str]:
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    source = str(APP_ROOT / "src")
    environment["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    return environment


def run_cli(*arguments: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "nagi.cli", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=cli_env(),
        timeout=timeout,
    )


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def test_version_smoke():
    result = run_cli("version")
    assert result.returncode == 0, result.stderr
    assert "nagi" in result.stdout


def test_help_smoke():
    result = run_cli("--help")
    assert result.returncode == 0
    for command in ("init", "run", "status", "doctor", "conf", "password", "db", "logs"):
        assert command in result.stdout


def test_unknown_command_smoke():
    result = run_cli("no-such-command")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_conf_set_get_smoke(tmp_path):
    config = tmp_path / "config" / "app.local.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump({"voice": {"enabled": False}}), encoding="utf-8")

    result = run_cli("--root", str(tmp_path), "conf", "set", "voice.enabled", "true")
    assert result.returncode == 0, result.stderr
    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert payload["voice"]["enabled"] is True

    result = run_cli("--root", str(tmp_path), "conf", "get", "voice.enabled")
    assert result.returncode == 0, result.stderr
    assert "true" in result.stdout

    result = run_cli("--root", str(tmp_path), "conf", "set", "client_key", "hunter22")
    assert result.returncode == 1
    assert "password" in result.stderr


def test_status_smoke_reports_stopped(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "gateway-port.txt").write_text(str(free_port()), encoding="utf-8")
    result = run_cli("--root", str(tmp_path), "status")
    assert result.returncode == 1
    assert "未运行" in result.stdout
