"""Local secret storage for provider credentials.

Keys are stored in the ignored data directory, never in YAML, the index or the browser.
Environment variables keep precedence so deployments can inject secrets without files.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from threading import Lock


class SecretStore:
    """Small JSON-backed store for provider credentials.

    Only the server reads values; HTTP responses expose ``credential_configured`` booleans.
    """

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "secrets.json"
        self._lock = Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Windows ignores POSIX modes; the parent directory stays ignored anyway.
        temporary.replace(self.path)

    def get(self, ref: str | None) -> str | None:
        if not ref:
            return None
        value = os.environ.get(ref)
        if value:
            return value
        with self._lock:
            return self._read().get("values", {}).get(ref)

    def set(self, ref: str, value: str) -> None:
        with self._lock:
            payload = self._read()
            payload.setdefault("version", 1)
            payload.setdefault("values", {})[ref] = value
            self._write(payload)

    def delete(self, ref: str) -> None:
        with self._lock:
            payload = self._read()
            if payload.get("values", {}).pop(ref, None) is not None:
                self._write(payload)

    def configured(self, ref: str | None) -> bool:
        return bool(self.get(ref))

    def last4(self, ref: str | None) -> str | None:
        """Return a non-reversible display hint; keys shorter than 8 chars stay hidden."""
        value = self.get(ref)
        if not value or len(value) < 8:
            return None
        return value[-4:]


def credential_ref_for(provider_id: str) -> str:
    """Stable environment-style reference derived from the provider id."""
    cleaned = "".join(character if character.isalnum() else "_" for character in provider_id).upper()
    return "NAGI_PROVIDER_" + (cleaned or "DEFAULT")
