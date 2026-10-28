"""Nagi 命令行入口：初始化、前台运行、诊断与本机数据维护。

设计约束：
- 只依赖标准库 + 项目已有依赖（argparse/sqlalchemy/alembic/PyYAML），不引入新依赖。
- 所有输出面向中文用户；密钥、令牌、密码、数据库口令永不打印。
- 既有命令（init-local、serve、memory-worker、archive、index）保持可用，
  init-local/serve 作为 init/run 的兼容别名保留。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import platform
import secrets
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import yaml
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from . import __version__
from .archive.repository import Database
from .core.types import Actor, Scope

DEFAULT_PORT = 18440
DEFAULT_HOST = "127.0.0.1"
CONFIG_RELATIVE = Path("config/app.local.yaml")
ALEMBIC_INI = Path("app/alembic.ini")
ALEMBIC_SCRIPTS = Path("app/migrations")
SECRET_SEGMENTS = {
    "password",
    "passwd",
    "secret",
    "token",
    "key",
    "client_key",
    "access_token",
    "api_key",
    "credential_ref",
    "credentials",
    "authorization",
    "cookie",
}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _short_error(exc: BaseException, limit: int = 200) -> str:
    text_value = " ".join(str(exc).split())
    return text_value[:limit] + ("…" if len(text_value) > limit else "")


def _config_path(root: Path) -> Path:
    return root / CONFIG_RELATIVE


def _load_raw_config(root: Path) -> dict:
    path = _config_path(root)
    if not path.is_file():
        raise RuntimeError(f"未找到配置文件 {path}；请先运行 nagi init")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"配置文件格式无效：{path}")
    return payload


def _write_config(root: Path, payload: dict) -> Path:
    path = _config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def _load_settings(root: Path):
    from .core.settings import load_settings

    return load_settings(_config_path(root))


def _load_settings_or_none(root: Path):
    try:
        return _load_settings(root)
    except Exception:  # 诊断命令需要在配置损坏时仍能继续
        return None


def _effective_data_dir(root: Path, settings) -> Path:
    """配置里的 data_dir 若是绝对路径则使用，否则回落到 <root>/data。"""
    if settings is not None:
        configured = Path(settings.data_dir)
        if configured.is_absolute():
            return configured
    return root / "data"


def _state_path(root: Path, name: str) -> Path:
    return _effective_data_dir(root, _load_settings_or_none(root)) / name


def _read_state(root: Path, name: str) -> str | None:
    path = _state_path(root, name)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _read_state_int(root: Path, name: str) -> int | None:
    value = _read_state(root, name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _write_state(root: Path, name: str, value: object) -> None:
    path = _state_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def _resolve_port(root: Path, explicit: int | None) -> int:
    if explicit:
        return explicit
    value = _read_state_int(root, "gateway-port.txt")
    return value or DEFAULT_PORT


def _resolve_host(root: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    return _read_state(root, "gateway-host.txt") or os.environ.get("AGI_HOST") or DEFAULT_HOST


def _port_listening(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()


def _pid_alive(pid: int) -> bool:
    try:
        if os.name == "nt":  # pragma: no cover - 仅在 Windows 上执行
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                return bool(ok) and exit_code.value == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:  # pragma: no cover - 防御性兜底，不因进程探测崩溃
        return False


def _http_json(url: str, timeout: float = 2.0) -> tuple[int | None, dict | None, str | None]:
    """请求本机 HTTP 端点；HTTP 错误也返回响应体，连接失败返回错误文本。"""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    code: int | None = None
    raw = b""
    try:
        with opener.open(request, timeout=timeout) as response:
            code = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read()
    except Exception as exc:  # URLError/超时/连接拒绝
        return None, None, _short_error(exc)
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        payload = None
    return code, payload if isinstance(payload, dict) else None, None


def _database_url(root: Path, settings=None) -> str:
    """按与网关一致的优先级解析数据库地址（不打印口令）。"""
    if os.environ.get("AGI_DATABASE_URL"):
        return os.environ["AGI_DATABASE_URL"]
    local_file = root / "data" / "local-db.json"
    if local_file.is_file():
        try:
            return json.loads(local_file.read_text(encoding="utf-8"))["url"]
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError(f"data/local-db.json 无效：{_short_error(exc)}") from exc
    resolved = settings if settings is not None else _load_settings_or_none(root)
    if resolved is not None:
        return resolved.database_url.get_secret_value()
    raise RuntimeError("尚未初始化数据库；请先运行 nagi init")


def _make_engine(url: str):
    connect_args = {"connect_timeout": 3} if str(url).startswith("postgresql") else {}
    return create_engine(url, hide_parameters=True, connect_args=connect_args)


def _alembic_head(root: Path) -> str | None:
    config = Config(str(root / ALEMBIC_INI))
    config.set_main_option("script_location", str(root / ALEMBIC_SCRIPTS))
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(config).get_current_head()


def _alembic_current(engine) -> str | None:
    with engine.connect() as connection:
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------


def migrate(engine, root):
    cfg = Config(str(root / ALEMBIC_INI))
    cfg.set_main_option("script_location", str(root / ALEMBIC_SCRIPTS))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def local_context(root):
    data = root / "data"
    url = _database_url(root)
    identity = json.loads((data / "local-identity.json").read_text(encoding="utf-8"))
    return (
        Database(create_engine(url, hide_parameters=True)),
        Actor.model_validate(identity["actor"]),
        Scope.model_validate(identity["scope"]),
    )


def _ensure_files(root: Path) -> None:
    """创建工作目录、访问令牌与默认配置；不依赖数据库。"""
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    token_file = data / "access-token.txt"
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    config = _config_path(root)
    if not config.exists():
        _write_config(
            root,
            {
                "data_dir": str(data).replace("\\", "/"),
                "providers": {},
                "models": {},
                "runtime": {"preferred": "direct_vendor", "fallback_policy": "explicit_admin_selection"},
                "embedding_device": "cpu",
                "asr_device": "cpu",
                "proactive": {"enabled": False},
            },
        )
    print(f"✓ 工作目录已就绪：{data}")
    print(f"✓ 配置文件：{config}")
    print("  访问令牌保存在 data/access-token.txt（不会显示其值）。")


def _init_database(root: Path) -> None:
    """创建本地 PostgreSQL 库（如需要）、执行迁移并写入开发身份。"""
    data = root / "data"
    local_file = data / "local-db.json"
    url = os.environ.get("AGI_DATABASE_URL")
    if not url and local_file.is_file():
        url = json.loads(local_file.read_text(encoding="utf-8"))["url"]
    if not url:
        source_file = data / "dev-db.json"
        if not source_file.exists():
            raise RuntimeError(
                "找不到本地 PostgreSQL 配置（data/dev-db.json）；"
                "请先运行 scripts/setup-dev-postgres.py，或设置 AGI_DATABASE_URL 指向可用的 PostgreSQL"
            )
        try:
            source = json.loads(source_file.read_text(encoding="utf-8"))["url"]
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError(f"data/dev-db.json 无效：{_short_error(exc)}") from exc
        target = make_url(source).set(database="agi_wind_local")
        admin = create_engine(
            target.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        try:
            with admin.connect() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname='agi_wind_local'")
                ).first()
                if not exists:
                    conn.exec_driver_sql('CREATE DATABASE "agi_wind_local"')
        finally:
            admin.dispose()
        url = target.render_as_string(hide_password=False)
        local_file.write_text(json.dumps({"url": url}), encoding="utf-8")
    engine = create_engine(url, hide_parameters=True)
    try:
        migrate(engine, root)
        identity_file = data / "local-identity.json"
        if not identity_file.exists():
            actor = Actor(account_id=uuid4(), user_id=uuid4())
            scope = Database(engine).seed(actor)
            from .archive.deletions import DeletionLedger

            DeletionLedger(data / "governance").initialize()
            identity_file.write_text(
                json.dumps(
                    {
                        "actor": actor.model_dump(mode="json"),
                        "scope": scope.model_dump(mode="json"),
                        "development_only": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        engine.dispose()
    print("✓ 本地数据库、迁移与开发身份已初始化。")


def init_command(root: Path, args: argparse.Namespace) -> int:
    _ensure_files(root)
    if getattr(args, "skip_database", False):
        print("! 已跳过数据库初始化（--skip-database）；运行 nagi init 可补全。")
        return 0
    try:
        _init_database(root)
    except Exception as exc:
        print(f"✗ 数据库初始化未完成：{_short_error(exc)}", file=sys.stderr)
        print("  配置与访问令牌已就绪；请确认 PostgreSQL 已启动后重新运行 nagi init。", file=sys.stderr)
        return 1
    return 0


def init_local(root: Path) -> int:
    """兼容旧入口：同 init（保留函数名供脚本/测试引用）。"""
    return init_command(root, argparse.Namespace(skip_database=False))


def init_local_command(root: Path, args: argparse.Namespace) -> int:
    print("提示：init-local 已更名为 init，此别名继续可用。", file=sys.stderr)
    return init_command(root, args)


# ---------------------------------------------------------------------------
# 运行网关
# ---------------------------------------------------------------------------


def _clear_own_pid(root: Path, pid: int) -> None:
    try:
        recorded = _read_state_int(root, "gateway.pid")
        if recorded == pid:
            _state_path(root, "gateway.pid").unlink(missing_ok=True)
    except OSError:
        pass


def run_command(root: Path, args: argparse.Namespace) -> int:
    config = _config_path(root)
    if not config.is_file():
        print(f"✗ 缺少配置文件 {config}；请先运行 nagi init", file=sys.stderr)
        return 1
    if getattr(args, "reset_password", False):
        code = _password_interactive(root)
        if code != 0:
            return code
    port = _resolve_port(root, getattr(args, "port", None))
    host = _resolve_host(root, getattr(args, "host", None))
    live_pid = _read_state_int(root, "gateway.pid")
    if live_pid and _pid_alive(live_pid) and _port_listening(port):
        print(
            f"✗ 网关已在运行（pid {live_pid}，端口 {port}）；"
            "如需重启请先停止现有进程（scripts/stop-local.ps1）。",
            file=sys.stderr,
        )
        return 1
    if _port_listening(port):
        print(f"✗ 端口 {port} 已被其他程序占用，未启动网关。", file=sys.stderr)
        return 1

    os.environ["AGI_CONFIG"] = str(config)
    if getattr(args, "host", None):
        os.environ["AGI_HOST"] = args.host
    from .api.app import from_env
    import uvicorn

    try:
        application = from_env()
    except RuntimeError as exc:
        print(f"✗ 网关启动前提未满足：{_short_error(exc)}", file=sys.stderr)
        return 1
    settings = application.state.settings
    if host not in ("127.0.0.1", "localhost", "::1") and settings.client_key.get_secret_value() == "123456":
        print(
            "警告：网关将监听非本机地址，但客户端密钥仍是默认值 123456；"
            "请先运行 nagi password 设置访问密码。",
            flush=True,
        )

    _write_state(root, "gateway.pid", os.getpid())
    _write_state(root, "gateway-port.txt", port)
    _write_state(root, "gateway-host.txt", host)
    print(f"正在启动网关：http://{host}:{port}（Ctrl+C 停止）", flush=True)
    reload_enabled = bool(getattr(args, "reload", False))
    try:
        if reload_enabled:
            print("提示：--reload 开发模式由 uvicorn 重新导入应用，不支持托管停止。", flush=True)
            uvicorn.run(
                "nagi.api.app:from_env",
                factory=True,
                host=host,
                port=port,
                reload=True,
                log_level="info",
                access_log=False,
            )
            return 0
        server = uvicorn.Server(
            uvicorn.Config(application, host=host, port=port, log_level="info", access_log=False)
        )
        application.state.request_shutdown = lambda: setattr(server, "should_exit", True)
        server.run()
        return 0
    except KeyboardInterrupt:
        print("\n网关已停止。")
        return 0
    finally:
        _clear_own_pid(root, os.getpid())


def serve_command(root: Path, args: argparse.Namespace) -> int:
    """兼容旧入口：同 run（旧脚本以 serve --port 调用）。"""
    if not getattr(args, "reset_password", False):
        args.reset_password = False
    return run_command(root, args)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def status_command(root: Path, args: argparse.Namespace) -> int:
    port = _resolve_port(root, getattr(args, "port", None))
    host = _read_state(root, "gateway-host.txt") or DEFAULT_HOST
    pid = _read_state_int(root, "gateway.pid")
    print(f"网关地址：http://127.0.0.1:{port}（绑定 {host}）")
    if pid:
        print(f"进程：{'运行中' if _pid_alive(pid) else '已退出'}（pid {pid}）")
    else:
        print("进程：没有 pid 记录（可能由其他方式启动）")

    if not _port_listening(port):
        print("状态：未运行")
        print("提示：使用 nagi run 或 scripts/start-local.ps1 启动网关。")
        return 1
    code, payload, error = _http_json(f"http://127.0.0.1:{port}/health/live")
    if code != 200 or (payload or {}).get("status") != "live":
        print(f"状态：端口 {port} 已监听，但 /health/live 未响应" + (f"（{error}）" if error else ""))
        return 1
    print("状态：运行中（live）")
    ready_code, ready, _ = _http_json(f"http://127.0.0.1:{port}/health/ready")
    if ready:
        dependencies = ready.get("dependencies") or {}
        summary = "，".join(f"{key}={value}" for key, value in dependencies.items())
        print(f"就绪：{ready.get('status', 'unknown')}（HTTP {ready_code}）")
        if summary:
            print(f"依赖：{summary}")
    else:
        print("就绪：/health/ready 未响应")
    return 0


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------


def _doctor_python():
    version = sys.version_info
    ok = version >= (3, 12) and version < (3, 13)
    return [("ok" if ok else "fail", "Python 版本", f"{platform.python_version()}（要求 3.12.x）")]


def _doctor_layout(root: Path):
    missing = [str(path) for path in (ALEMBIC_INI, ALEMBIC_SCRIPTS) if not (root / path).exists()]
    if missing:
        return [("fail", "项目结构", "缺少 " + "、".join(missing))]
    return [("ok", "项目结构", "app/alembic.ini 与 app/migrations 已就绪")]


def _doctor_config(root: Path):
    config = _config_path(root)
    if not config.is_file():
        return [("fail", "配置文件", f"缺少 {config}；请先运行 nagi init")], None
    try:
        settings = _load_settings(root)
    except Exception as exc:
        return [("fail", "配置文件", f"解析失败：{_short_error(exc)}")], None
    return [("ok", "配置文件", f"{config} 可解析")], settings


def _doctor_data(root: Path, settings):
    data = _effective_data_dir(root, settings)
    items = []
    try:
        data.mkdir(parents=True, exist_ok=True)
        probe = data / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        items.append(("ok", "数据目录", f"{data}（可写）"))
    except OSError as exc:
        items.append(("fail", "数据目录", f"{data} 不可写：{_short_error(exc)}"))
    identity = data / "local-identity.json"
    items.append(
        ("ok" if identity.is_file() else "fail", "开发身份", "已初始化" if identity.is_file() else "缺失（运行 nagi init）")
    )
    token = data / "access-token.txt"
    items.append(
        ("ok" if token.is_file() else "fail", "访问令牌", "已保存" if token.is_file() else "缺失（运行 nagi init）")
    )
    return items


def _doctor_database(root: Path, settings):
    try:
        url = _database_url(root, settings)
    except Exception as exc:
        return [("fail", "数据库", f"无法确定连接地址：{_short_error(exc)}")]
    engine = None
    try:
        engine = _make_engine(url)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        return [("fail", "数据库", f"无法连接：{_short_error(exc)}")]
    items = [("ok", "数据库", "连接正常")]
    try:
        current = _alembic_current(engine)
    except Exception:  # 数据库可用但迁移表缺失
        current = None
    try:
        head = _alembic_head(root)
    except Exception as exc:
        head = None
        items.append(("warn", "迁移", f"无法读取迁移脚本：{_short_error(exc)}"))
    try:
        if head:
            if current == head:
                items.append(("ok", "迁移", f"已是最新（{head}）"))
            elif current is None:
                items.append(("fail", "迁移", f"数据库尚未执行迁移（head {head}），请运行 nagi db upgrade"))
            else:
                items.append(("fail", "迁移", f"数据库 {current} → head {head}，请运行 nagi db upgrade"))
    finally:
        if engine is not None:
            engine.dispose()
    return items


def _doctor_providers(settings, data_dir: Path):
    from .core.secrets import SecretStore

    store = SecretStore(data_dir)
    items = []
    if not settings.providers:
        items.append(("warn", "模型厂商", "尚未配置任何厂商（可在网页设置中填写）"))
    for provider_id, provider in settings.providers.items():
        configured = bool(store.get(provider.credential_ref))
        if configured:
            items.append(("ok", f"厂商密钥 {provider_id}", "已配置"))
        else:
            items.append(
                ("warn", f"厂商密钥 {provider_id}", f"未配置（环境变量 {provider.credential_ref}）")
            )
    if settings.models.dialogue is None:
        items.append(("warn", "对话模型", "尚未选择对话模型"))
    return items


def _doctor_voice(settings):
    if not settings.voice.enabled:
        return [("ok", "语音 TTS", "未启用（可选）")]
    url = settings.voice.base_url.rstrip("/") + "/health"
    code, _, error = _http_json(url)
    if code == 200:
        return [("ok", "语音 TTS", f"可用（{url}）")]
    return [("warn", "语音 TTS", f"无法连接 {url}" + (f"：{error}" if error else ""))]


def _doctor_asr(root: Path):
    if not (root / "config" / "asr.local.json").is_file():
        return [("ok", "实时识别 ASR", "未安装（可选）")]
    url = "http://127.0.0.1:8139/health"
    code, _, error = _http_json(url)
    if code == 200:
        return [("ok", "实时识别 ASR", f"可用（{url}）")]
    return [("warn", "实时识别 ASR", f"无法连接 {url}" + (f"：{error}" if error else ""))]


def _doctor_disk(root: Path):
    try:
        free = shutil.disk_usage(str(root)).free
    except OSError as exc:
        return [("warn", "磁盘空间", f"无法读取：{_short_error(exc)}")]
    gigibytes = free / (1024**3)
    if free < 500 * 1024**2:
        return [("fail", "磁盘空间", f"剩余 {gigibytes:.1f} GiB，低于 0.5 GiB")]
    if free < 2 * 1024**3:
        return [("warn", "磁盘空间", f"剩余 {gigibytes:.1f} GiB，建议清理")]
    return [("ok", "磁盘空间", f"剩余 {gigibytes:.1f} GiB")]


def _doctor_port(root: Path, args: argparse.Namespace):
    port = _resolve_port(root, getattr(args, "port", None))
    if not _port_listening(port):
        return [("ok", "端口", f"{port} 可用")]
    code, payload, _ = _http_json(f"http://127.0.0.1:{port}/health/live")
    if code == 200 and (payload or {}).get("status") == "live":
        return [("warn", "端口", f"{port} 已被运行中的网关占用（启动新实例前先停止）")]
    return [("fail", "端口", f"{port} 已被其他程序占用")]


def _doctor_collect(root: Path, args: argparse.Namespace):
    items = []
    items.extend(_doctor_python())
    items.extend(_doctor_layout(root))
    config_items, settings = _doctor_config(root)
    items.extend(config_items)
    items.extend(_doctor_data(root, settings))
    if settings is not None:
        items.extend(_doctor_database(root, settings))
        items.extend(_doctor_providers(settings, _effective_data_dir(root, settings)))
        items.extend(_doctor_voice(settings))
    else:
        items.append(("warn", "数据库", "跳过（配置文件不可用）"))
    items.extend(_doctor_asr(root))
    items.extend(_doctor_disk(root))
    items.extend(_doctor_port(root, args))
    return items


def doctor_command(root: Path, args: argparse.Namespace) -> int:
    print(f"Nagi 运行诊断（root: {root}）")
    print("-" * 52)
    items = _doctor_collect(root, args)
    icons = {"ok": "✓", "warn": "!", "fail": "✗"}
    for level, title, detail in items:
        print(f"{icons.get(level, '?')} {title}：{detail}")
    failures = sum(1 for level, _, _ in items if level == "fail")
    warnings = sum(1 for level, _, _ in items if level == "warn")
    print("-" * 52)
    print(f"检查完成：{len(items)} 项，{failures} 项失败，{warnings} 项警告。")
    if failures:
        print("请先修复失败项；详细排查见 docs/ops/runbook.md。")
        return 1
    return 0


# ---------------------------------------------------------------------------
# 配置项（安全、无密钥）
# ---------------------------------------------------------------------------


def _forbidden_conf_key(key: str) -> bool:
    for segment in key.lower().split("."):
        if segment in SECRET_SEGMENTS or segment.endswith(("_token", "_key", "_secret", "_password")):
            return True
    return False


def _flatten_conf(prefix: str, node: object):
    if isinstance(node, dict):
        if not node:
            yield prefix, {}
            return
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_conf(child, value)
        return
    yield prefix, node


def _format_conf_value(value: object) -> str:
    text_value = json.dumps(value, ensure_ascii=False, default=str)
    return text_value[:160] + ("…" if len(text_value) > 160 else "")


def conf_list_command(root: Path, args: argparse.Namespace) -> int:
    try:
        payload = _load_raw_config(root)
    except RuntimeError as exc:
        print(f"✗ {_short_error(exc)}", file=sys.stderr)
        return 1
    print(f"配置文件：{_config_path(root)}")
    rows = [row for row in _flatten_conf("", payload) if not _forbidden_conf_key(row[0])]
    if not rows:
        print("（没有可显示的安全配置项）")
        return 0
    for key, value in rows:
        print(f"{key} = {_format_conf_value(value)}")
    print("提示：密钥/令牌/密码不会列出；访问密码请用 nagi password 设置。")
    return 0


def conf_get_command(root: Path, args: argparse.Namespace) -> int:
    key = args.key
    if _forbidden_conf_key(key):
        print(f"✗ 拒绝读取敏感配置项 {key}；密钥不会通过 CLI 输出。", file=sys.stderr)
        return 1
    try:
        payload = _load_raw_config(root)
    except RuntimeError as exc:
        print(f"✗ {_short_error(exc)}", file=sys.stderr)
        return 1
    current: object = payload
    for segment in key.split("."):
        if not isinstance(current, dict) or segment not in current:
            print(f"未设置：{key}")
            return 1
        current = current[segment]
    print(f"{key} = {_format_conf_value(current)}")
    return 0


def conf_set_command(root: Path, args: argparse.Namespace) -> int:
    key, raw_value = args.key, args.value
    if _forbidden_conf_key(key):
        print(f"✗ 拒绝设置敏感配置项 {key}；访问密码请用 nagi password。", file=sys.stderr)
        return 1
    try:
        value = yaml.safe_load(raw_value)
    except yaml.YAMLError as exc:
        print(f"✗ 无法解析值：{_short_error(exc)}", file=sys.stderr)
        return 1
    try:
        payload = _load_raw_config(root)
    except RuntimeError as exc:
        print(f"✗ {_short_error(exc)}", file=sys.stderr)
        return 1
    candidate = json.loads(json.dumps(payload, default=str))
    target = candidate
    segments = key.split(".")
    for segment in segments[:-1]:
        if not isinstance(target.get(segment), dict):
            target[segment] = {}
        target = target[segment]
    target[segments[-1]] = value
    from .core.settings import Settings

    try:
        Settings.model_validate(candidate)
    except Exception as exc:
        print(f"✗ 配置校验失败，未写入：{_short_error(exc)}", file=sys.stderr)
        return 1
    path = _write_config(root, candidate)
    print(f"✓ 已更新 {key} = {_format_conf_value(value)}（{path.relative_to(root)}）")
    print("  部分配置需要重启网关后生效。")
    return 0


# ---------------------------------------------------------------------------
# 访问密码
# ---------------------------------------------------------------------------


def _set_password_value(root: Path, value: str) -> Path:
    payload = _load_raw_config(root)
    payload["client_key"] = value
    return _write_config(root, payload)


def _password_interactive(root: Path) -> int:
    if not _config_path(root).is_file():
        print(f"✗ 缺少配置文件 {_config_path(root)}；请先运行 nagi init", file=sys.stderr)
        return 1
    try:
        first = getpass.getpass("请输入新的访问密码（至少 8 位，输入不回显）: ")
        confirm = getpass.getpass("请再次输入以确认: ")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        return 1
    if len(first) < 8:
        print("✗ 访问密码至少需要 8 位。", file=sys.stderr)
        return 1
    if first != confirm:
        print("✗ 两次输入不一致，未修改。", file=sys.stderr)
        return 1
    path = _set_password_value(root, first)
    print(f"✓ 访问密码已更新（{path.relative_to(root)}）。重启网关后生效。")
    print("  提示：密码仅用于局域网/远程客户端登录；本机回环访问仍可自动登录。")
    return 0


def password_command(root: Path, args: argparse.Namespace) -> int:
    return _password_interactive(root)


# ---------------------------------------------------------------------------
# 数据库维护
# ---------------------------------------------------------------------------


def db_upgrade_command(root: Path, args: argparse.Namespace) -> int:
    try:
        url = _database_url(root)
    except RuntimeError as exc:
        print(f"✗ {_short_error(exc)}", file=sys.stderr)
        return 1
    engine = None
    try:
        engine = create_engine(url, hide_parameters=True)
        current = _alembic_current(engine)
        head = _alembic_head(root)
        if head and current == head:
            print(f"✓ 数据库迁移已是最新（{head}）。")
            return 0
        print(f"正在升级数据库迁移：{current or '（空）'} → {head or '（未知）'}")
        migrate(engine, root)
        print(f"✓ 数据库迁移完成（{_alembic_current(engine)}）。")
        return 0
    except Exception as exc:
        print(f"✗ 数据库迁移失败：{_short_error(exc)}", file=sys.stderr)
        print("  请确认 PostgreSQL 已启动（scripts/setup-dev-postgres.py）且连接配置正确。", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def db_status_command(root: Path, args: argparse.Namespace) -> int:
    try:
        url = _database_url(root)
    except RuntimeError as exc:
        print(f"✗ {_short_error(exc)}", file=sys.stderr)
        return 1
    engine = None
    try:
        engine = create_engine(url, hide_parameters=True)
        current = _alembic_current(engine)
    except Exception as exc:
        print(f"✗ 无法连接数据库：{_short_error(exc)}", file=sys.stderr)
        return 1
    try:
        head = _alembic_head(root)
    except Exception as exc:
        head = None
        print(f"! 无法读取迁移脚本：{_short_error(exc)}", file=sys.stderr)
    finally:
        if engine is not None:
            engine.dispose()
    print(f"数据库当前版本：{current or '（空，尚未迁移）'}")
    print(f"代码迁移目标版本：{head or '（未知）'}")
    if head and current == head:
        print("状态：已是最新。")
    elif head:
        print("状态：存在待执行迁移，运行 nagi db upgrade。")
    return 0


def db_backup_command(root: Path, args: argparse.Namespace) -> int:
    settings = _load_settings_or_none(root)
    data = _effective_data_dir(root, settings)
    backups = Path(args.output).expanduser() if getattr(args, "output", None) else data / "backups"
    try:
        if backups.suffix.lower() != ".zip":
            backups.mkdir(parents=True, exist_ok=True)
            target = backups / f"nagi-{datetime.now():%Y%m%d-%H%M%S}.zip"
            counter = 1
            while target.exists():
                target = backups / f"nagi-{datetime.now():%Y%m%d-%H%M%S}-{counter}.zip"
                counter += 1
        else:
            target = backups
            target.parent.mkdir(parents=True, exist_ok=True)
        db, actor, scope = local_context(root)
        try:
            from .archive.backup import BackupService

            manifest = asyncio.run(BackupService(db, data / "assets").create(actor, scope.companion_id, target))
        finally:
            db.engine.dispose()
    except Exception as exc:
        print(f"✗ 备份失败：{_short_error(exc)}", file=sys.stderr)
        return 1
    print(f"✓ 备份已创建：{target}")
    if isinstance(manifest, dict):
        records = manifest.get("records") or manifest.get("event_count")
        if records is not None:
            print(f"  记录数：{records}")
    return 0


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def _log_secrets(settings, data_dir: Path) -> tuple[str, ...]:
    values: list[str] = []
    if settings is not None:
        if settings.access_token:
            values.append(settings.access_token.get_secret_value())
        if settings.client_key:
            values.append(settings.client_key.get_secret_value())
        from .core.secrets import SecretStore

        store = SecretStore(data_dir)
        for provider in settings.providers.values():
            value = store.get(provider.credential_ref)
            if value:
                values.append(value)
    return tuple(values)


def logs_command(root: Path, args: argparse.Namespace) -> int:
    from .api.logs import SystemLogReader

    settings = _load_settings_or_none(root)
    data_dir = _effective_data_dir(root, settings)
    reader = SystemLogReader(data_dir, _log_secrets(settings, data_dir))
    if not any(path.is_file() for path in reader.paths.values()):
        print(
            f"✗ 未找到网关日志（{data_dir / 'gateway.stdout.log'}）；"
            "网关可能不是由 start-local.ps1 / nagi run 启动的。",
            file=sys.stderr,
        )
        return 1
    if not getattr(args, "follow", False):
        result = reader.read(tail=args.tail)
        for line in result["lines"]:
            print(f"[{line['stream']}] {line['text']}")
        return 0
    print("正在跟踪网关日志（Ctrl+C 退出）…", flush=True)
    offsets = None
    try:
        while True:
            kwargs: dict = {"tail": args.tail}
            if offsets:
                kwargs["stdout_offset"] = offsets.get("stdout", 0)
                kwargs["stderr_offset"] = offsets.get("stderr", 0)
            result = reader.read(**kwargs)
            if result.get("reset"):
                print("[日志已轮转，重新跟随]", flush=True)
            for line in result["lines"]:
                print(f"[{line['stream']}] {line['text']}", flush=True)
            offsets = result.get("offsets") or offsets
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n已停止跟踪。", flush=True)
        return 0


# ---------------------------------------------------------------------------
# 版本 / 帮助
# ---------------------------------------------------------------------------


def version_command(root: Path, args: argparse.Namespace) -> int:
    print(f"nagi {__version__}（Python {platform.python_version()}）")
    return 0


def _print_help(parser: argparse.ArgumentParser) -> int:
    parser.print_help()
    return 0


# ---------------------------------------------------------------------------
# 既有命令（保持可用）
# ---------------------------------------------------------------------------


def memory_worker_command(root: Path, args: argparse.Namespace) -> int:
    from .memory.runtime import run_worker

    asyncio.run(run_worker(root, once=args.once))
    return 0


def archive_command(root: Path, args: argparse.Namespace) -> int:
    from .archive.imports import ImportService

    if args.operation == "verify":
        report = ImportService.validate(args.path)
        print(json.dumps({"valid": True, "records": report["records"]}, ensure_ascii=False))
        return 0
    db, actor, scope = local_context(root)
    try:
        if args.operation == "export":
            from .archive.exports import ExportService

            path = asyncio.run(
                ExportService(db, root / "data" / "assets").export(actor, scope.companion_id, args.output)
            )
            print(str(path))
        elif args.operation == "import":
            from .archive.deletions import DeletionLedger

            ledger = DeletionLedger(args.latest_ledger) if args.latest_ledger else None
            report = asyncio.run(ImportService(db, root / "data" / "assets").restore(args.path, args.mode, ledger))
            print(json.dumps(report, ensure_ascii=False, default=str))
    finally:
        db.engine.dispose()
    return 0


def index_command(root: Path, args: argparse.Namespace) -> int:
    from .archive.backup import IndexService

    db, actor, scope = local_context(root)
    try:
        asyncio.run(IndexService(db).rebuild_keywords(scope))
    finally:
        db.engine.dispose()
    print("✓ 关键词索引已重建；原始事件与人物档案保留。向量索引未配置。")
    return 0


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nagi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Nagi 伴侣网关命令行工具：初始化、运行、诊断与本地数据维护。",
        epilog=(
            "示例：\n"
            "  nagi init\n"
            "  nagi run --host 127.0.0.1 --port 18440\n"
            "  nagi status\n"
            "  nagi doctor\n"
            "  nagi conf list\n"
            "  nagi conf set voice.enabled true\n"
            "  nagi password\n"
            "  nagi db backup\n"
            "  nagi logs --follow"
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="项目根目录（默认仓库根）",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="命令")

    init = commands.add_parser("init", help="初始化工作目录、配置、访问令牌与数据库")
    init.add_argument("--skip-database", action="store_true", help="只创建工作目录与配置，不初始化 PostgreSQL")
    init.set_defaults(handler=init_command)

    init_local = commands.add_parser("init-local", help="兼容旧命令：同 init")
    init_local.add_argument("--skip-database", action="store_true", help=argparse.SUPPRESS)
    init_local.set_defaults(handler=init_local_command)

    run = commands.add_parser("run", help="前台启动网关")
    run.add_argument("--host", default=None, help="监听地址；局域网服务用 0.0.0.0，默认 127.0.0.1")
    run.add_argument("--port", type=int, default=None, help=f"监听端口，默认 {DEFAULT_PORT}")
    run.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载")
    run.add_argument("--reset-password", action="store_true", help="启动前交互式重设访问密码")
    run.set_defaults(handler=run_command)

    serve = commands.add_parser("serve", help="兼容旧命令：同 run")
    serve.add_argument("--host", default=None, help="监听地址；局域网服务用 0.0.0.0，默认 127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认 {DEFAULT_PORT}")
    serve.add_argument("--reload", action="store_true", help=argparse.SUPPRESS)
    serve.add_argument("--reset-password", action="store_true", help=argparse.SUPPRESS)
    serve.set_defaults(handler=serve_command)

    status = commands.add_parser("status", help="查看网关进程与端口健康状态")
    status.add_argument("--port", type=int, default=None, help="覆盖状态检查端口")
    status.set_defaults(handler=status_command)

    doctor = commands.add_parser("doctor", help="检查运行环境、配置、数据库、密钥与端口")
    doctor.add_argument("--port", type=int, default=None, help="覆盖端口检查")
    doctor.set_defaults(handler=doctor_command)

    conf = commands.add_parser("conf", help="查看/修改安全配置项（不接触密钥）")
    conf_commands = conf.add_subparsers(dest="conf_operation", required=True, metavar="操作")
    conf_list = conf_commands.add_parser("list", help="列出可显示的安全配置项")
    conf_list.set_defaults(handler=conf_list_command)
    conf_get = conf_commands.add_parser("get", help="读取一个配置项（点号路径）")
    conf_get.add_argument("key", help="点号路径，例如 voice.enabled")
    conf_get.set_defaults(handler=conf_get_command)
    conf_set = conf_commands.add_parser("set", help="设置一个配置项（点号路径）")
    conf_set.add_argument("key", help="点号路径，例如 voice.enabled")
    conf_set.add_argument("value", help="值，按 YAML 解析，例如 true / 0.0.0.0 / '[a, b]'")
    conf_set.set_defaults(handler=conf_set_command)

    password = commands.add_parser("password", help="交互式设置/重置访问密码")
    password.set_defaults(handler=password_command)

    db = commands.add_parser("db", help="数据库迁移与备份")
    db_commands = db.add_subparsers(dest="db_operation", required=True, metavar="操作")
    db_upgrade = db_commands.add_parser("upgrade", help="执行待应用的数据库迁移")
    db_upgrade.set_defaults(handler=db_upgrade_command)
    db_status = db_commands.add_parser("status", help="查看数据库迁移版本")
    db_status.set_defaults(handler=db_status_command)
    db_backup = db_commands.add_parser("backup", help="导出可移植备份（zip）")
    db_backup.add_argument("--output", type=Path, default=None, help="备份文件或目录，默认 data/backups")
    db_backup.set_defaults(handler=db_backup_command)

    logs = commands.add_parser("logs", help="查看网关日志（可选持续跟踪）")
    logs.add_argument("--follow", action="store_true", help="持续跟踪新日志，Ctrl+C 退出")
    logs.add_argument("--tail", type=int, default=200, help="首次显示的末尾行数，默认 200")
    logs.set_defaults(handler=logs_command)

    version = commands.add_parser("version", help="显示版本")
    version.set_defaults(handler=version_command)

    help_command = commands.add_parser("help", help="显示帮助")
    help_command.set_defaults(handler=lambda root, args: _print_help(parser))

    memory_worker = commands.add_parser("memory-worker", help="运行记忆后台工作器")
    memory_worker.add_argument("--once", action="store_true", help="执行一次记忆后台周期后退出")
    memory_worker.set_defaults(handler=memory_worker_command)

    archive = commands.add_parser("archive", help="档案校验、导出与导入")
    archive_commands = archive.add_subparsers(dest="operation", required=True, metavar="操作")
    verify = archive_commands.add_parser("verify", help="校验导出包")
    verify.add_argument("path", type=Path)
    verify.set_defaults(handler=archive_command)
    export = archive_commands.add_parser("export", help="导出档案")
    export.add_argument("--output", required=True, type=Path)
    export.set_defaults(handler=archive_command)
    restore = archive_commands.add_parser("import", help="导入档案")
    restore.add_argument("path", type=Path)
    restore.add_argument("--mode", choices=["new_namespace", "clone"], default="new_namespace")
    restore.add_argument("--latest-ledger", type=Path)
    restore.set_defaults(handler=archive_command)

    index = commands.add_parser("index", help="维护检索索引")
    index.add_argument("operation", choices=["rebuild"])
    index.set_defaults(handler=index_command)

    return parser


def _force_utf8_output() -> None:
    """重定向输出时统一用 UTF-8，避免 Windows 控制台代码页把中文写成乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.root.resolve()
    handler = getattr(args, "handler", None)
    if handler is None:  # pragma: no cover - argparse 的子命令均为必填
        parser.print_help()
        return 2
    try:
        return int(handler(root, args) or 0)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except RuntimeError as exc:
        print(f"✗ {_short_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
