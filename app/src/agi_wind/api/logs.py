"""Read the gateway's console logs for the client terminal drawer.

The gateway is normally started by ``scripts/start-local.ps1`` with stdout/stderr
redirected into the ignored data directory.  This module tails those files with byte
offsets so the browser can append new lines without reloading the whole log.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_READ_BYTES = 2 * 1024 * 1024
SQL_PASSWORD = re.compile(r"(postgresql(?:\+\w+)?://[^:\s/]+:)[^@\s]+@", re.IGNORECASE)
BEARER = re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+")
ASSIGNMENT = re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|client[_-]?key|secret|password)[\"']?\s*[:=]\s*)[\"']?[^\s\"']{6,}")


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    for value in secrets:
        if value and len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    text = SQL_PASSWORD.sub(r"\1[REDACTED]@", text)
    text = BEARER.sub(r"\1[REDACTED]", text)
    text = ASSIGNMENT.sub(r"\1[REDACTED]", text)
    return text


class SystemLogReader:
    """Byte-offset tailer for ``data/gateway.stdout.log`` and ``gateway.stderr.log``."""

    def __init__(self, data_dir: str | Path, secrets: tuple[str, ...] = ()):
        self.data_dir = Path(data_dir)
        self.secrets = tuple(value for value in secrets if value)
        self.paths = {
            "stdout": self.data_dir / "gateway.stdout.log",
            "stderr": self.data_dir / "gateway.stderr.log",
        }

    def _chunk(self, path: Path, offset: int | None, tail: int) -> tuple[list[str], int, bool]:
        if not path.is_file():
            return [], 0, False
        size = path.stat().st_size
        reset = False
        if offset is None:
            start = max(0, size - MAX_READ_BYTES)
            limited_tail = True
        elif offset > size:
            start = max(0, size - MAX_READ_BYTES)
            reset = True
            limited_tail = True
        else:
            start = offset
            limited_tail = False
        with path.open("rb") as stream:
            stream.seek(start)
            raw = stream.read(min(MAX_READ_BYTES, max(0, size - start)))
        end = raw.rfind(b"\n")
        if end < 0:
            # No complete line yet; stop before the pending bytes so nothing is lost.
            return [], offset if offset is not None else 0, reset
        raw = raw[: end + 1]
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if limited_tail and start > 0 and lines:
            lines = lines[1:]  # the first line may begin mid-line
        if limited_tail:
            lines = lines[-max(1, tail):]
        next_offset = start + len(raw)
        return lines, next_offset, reset

    def read(self, tail: int = 300, stdout_offset: int | None = None, stderr_offset: int | None = None, secrets: tuple[str, ...] | None = None) -> dict:
        active_secrets = tuple(secrets) if secrets is not None else self.secrets
        offsets: dict[str, int] = {}
        lines: list[dict[str, str]] = []
        reset = False
        for name in ("stdout", "stderr"):
            supplied = stdout_offset if name == "stdout" else stderr_offset
            chunks, offset, was_reset = self._chunk(self.paths[name], supplied, tail)
            offsets[name] = offset
            reset = reset or was_reset
            for line in chunks:
                lines.append({"stream": name, "text": _redact(line, active_secrets)})
        return {"lines": lines, "offsets": offsets, "reset": reset}
