"""Short-lived browser session bridge.

The gateway's long-lived access token never reaches browser storage. A page asks the
server to issue a one-time launch ticket (Bearer), exchanges it for an HttpOnly cookie
session at the same origin, and then authenticates with that cookie plus a CSRF token.

Sessions are kept in a small JSON file so a gateway restart does not silently invalidate
pages that are still open. Without this, a message sent from a stale page was shown as
sent by the client but rejected with 401 and disappeared after refresh.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path


class BrowserSessions:
    SESSION_COOKIE = "agi_browser_session"

    def __init__(self, clock=None, ticket_ttl: float = 120.0, session_ttl: float = 12 * 3600.0, persist_path: Path | None = None):
        self.clock = clock or time.time
        self.ticket_ttl = ticket_ttl
        self.session_ttl = session_ttl
        self._tickets: dict[str, float] = {}
        self._sessions: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._persist_path = Path(persist_path) if persist_path else None
        self._load()

    def _load(self):
        if self._persist_path is None or not self._persist_path.is_file():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            now = self.clock()
            with self._lock:
                self._sessions = {
                    session_id: {"csrf": str(record.get("csrf") or ""), "expires_at": float(record.get("expires_at") or 0)}
                    for session_id, record in (data.get("sessions") or {}).items()
                    if isinstance(record, dict) and float(record.get("expires_at") or 0) > now
                }
        except (OSError, ValueError, TypeError, AttributeError):
            with self._lock:
                self._sessions = {}

    def _save(self):
        if self._persist_path is None:
            return
        with self._lock:
            payload = json.dumps({"sessions": self._sessions}, ensure_ascii=False)
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(self._persist_path)
        except OSError:
            # A failed cache write must never break login or a running page.
            pass

    def issue_ticket(self) -> str:
        """Create a single-use ticket; the value is only shown once to the launching page."""
        ticket = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._tickets = {key: expiry for key, expiry in self._tickets.items() if expiry > now}
            self._tickets[ticket] = now + self.ticket_ttl
        return ticket

    def create(self) -> tuple[str, str]:
        """Create a session directly (used for loopback-only automatic login)."""
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        with self._lock:
            self._sessions[session_id] = {"csrf": csrf_token, "expires_at": self.clock() + self.session_ttl}
        self._save()
        return session_id, csrf_token

    def exchange(self, ticket: str) -> tuple[str, str] | None:
        """Consume a ticket and return (session_id, csrf_token), or None when invalid."""
        with self._lock:
            expiry = self._tickets.pop(ticket, None)
        if expiry is None or expiry <= self.clock():
            return None
        return self.create()

    def validate(self, session_id: str | None) -> str | None:
        """Return the session CSRF token when the session is still valid."""
        if not session_id:
            return None
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return None
            if record["expires_at"] <= self.clock():
                self._sessions.pop(session_id, None)
                self._save()
                return None
            return record["csrf"]

    def revoke(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._sessions.pop(session_id, None)
        self._save()

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._tickets.clear()
        self._save()
