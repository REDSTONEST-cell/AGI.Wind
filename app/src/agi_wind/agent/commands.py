"""Controlled command execution for the local Agent.

Windows has no OS-level sandbox for the gateway process, so this module does not
pretend to isolate commands. The safety boundary is explicit: the tool is off by
default, every execution can require a human approval, commands run with the
gateway user's privileges, and time/output are bounded. The design mirrors
AstrBot's "Agent Computer Use" shell tool, with per-command approval replacing
the member/administrator permission matrix.
"""
import asyncio
import locale
from pathlib import Path
import subprocess
import sys

from pydantic import Field

from ..core.errors import AppError
from ..core.types import DTO
from .runner import ToolSpec

MAX_COMMAND_CHARS = 4000
MAX_PREVIEW_CHARS = 500


class CommandArgs(DTO):
    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS)
    cwd: str = Field(default="", max_length=1000)


def _decode_output(data: bytes) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


class CommandTools:
    def __init__(self, data_dir, settings, approvals, actor, generation_id, check_current):
        self.data_dir = Path(data_dir).resolve()
        self.base = self.data_dir / "agent-workspace"
        self.settings, self.approvals = settings, approvals
        self.actor, self.generation_id, self.check_current = actor, generation_id, check_current
        self.permits = {}

    def mode(self):
        policy = self.settings()
        if not policy.enabled or policy.command_access_mode == "disabled":
            raise AppError("TOOL_NOT_ALLOWED", "命令行工具已关闭", 403)
        return policy.command_access_mode

    def workspace(self):
        self.base.mkdir(parents=True, exist_ok=True)
        return self.base

    def resolve_cwd(self, value):
        if not value.strip():
            return self.workspace()
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace() / path
        path = path.resolve()
        if not path.is_dir():
            raise AppError("COMMAND_CWD_INVALID", "命令工作目录不存在", 422)
        return path

    @staticmethod
    def preview(command):
        return command if len(command) <= MAX_PREVIEW_CHARS else command[:MAX_PREVIEW_CHARS] + "…"

    def token(self, args):
        return args.model_dump_json()

    def authorization(self):
        async def authorize(args):
            mode = self.mode()
            if mode == "full":
                return
            policy = self.settings()
            cwd = self.resolve_cwd(args.cwd)
            details = {
                "command": self.preview(args.command),
                "cwd": str(cwd),
                "timeout_seconds": policy.command_timeout_seconds,
            }
            await self.approvals.request(self.actor, self.generation_id, "run_command", details)
            await self.check_current()
            self.mode()
            self.permits[self.token(args)] = str(cwd)
        return authorize

    async def execute(self, args):
        mode = self.mode()
        policy = self.settings()
        token = self.token(args)
        approved_cwd = self.permits.pop(token, None)
        cwd = self.resolve_cwd(args.cwd)
        if mode != "full" and approved_cwd != str(cwd):
            raise AppError("TOOL_APPROVAL_REQUIRED", "需要先批准此命令", 403)
        await self.check_current()
        return await asyncio.to_thread(
            self.run,
            args.command,
            cwd,
            policy.command_timeout_seconds,
            policy.command_max_output_chars,
        )

    @staticmethod
    def run(command, cwd, timeout, max_output):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
            )
        except OSError:
            raise AppError("COMMAND_START_FAILED", "命令启动失败", 422) from None
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            CommandTools.terminate(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                stdout, stderr = b"", b""
            raise AppError("COMMAND_TIMEOUT", "命令执行超时，已被终止", 408) from None
        out_text, err_text = _decode_output(stdout), _decode_output(stderr)
        truncated = len(out_text) > max_output or len(err_text) > max_output
        return {
            "command": command,
            "cwd": str(cwd),
            "exit_code": process.returncode,
            "stdout": out_text[:max_output],
            "stderr": err_text[:max_output],
            "truncated": truncated,
        }

    @staticmethod
    def terminate(process):
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=10,
                )
            else:
                process.kill()
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def specs(self):
        if self.settings().command_access_mode == "disabled":
            return []
        return [
            ToolSpec(
                "run_command",
                "在网关所在电脑执行一条命令行（Windows 用 cmd，其他系统用 /bin/sh），返回退出码、stdout 和 stderr。"
                "命令以网关进程用户权限运行，可修改文件或联网；逐次批准模式下每次执行前都会请求用户确认。"
                "超时或输出长度受限。不要执行破坏性、提权、后台常驻或需要长期等待的命令。",
                CommandArgs,
                self.execute,
                self.authorization(),
            )
        ]
