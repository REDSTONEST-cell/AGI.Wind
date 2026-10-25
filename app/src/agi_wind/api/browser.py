"""Short-lived browser session bridge.

The gateway's long-lived access token never reaches browser storage. A page asks the
server to issue a one-time launch ticket (Bearer), exchanges it for an HttpOnly cookie
session at the same origin, and then authenticates with that cookie plus a CSRF token.

The store is in-process and intentionally small: sessions vanish on restart, which is
the documented recovery path for a single-account local deployment.
"""

from __future__ import annotations

import secrets
import time


class BrowserSessions:
    SESSION_COOKIE = "agi_browser_session"

    def __init__(self, clock=None, ticket_ttl: float = 120.0, session_ttl: float = 12 * 3600.0):
        self.clock = clock or time.time
        self.ticket_ttl = ticket_ttl
        self.session_ttl = session_ttl
        self._tickets: dict[str, float] = {}
        self._sessions: dict[str, dict] = {}

    def issue_ticket(self) -> str:
        """Create a single-use ticket; the value is only shown once to the launching page."""
        ticket = secrets.token_urlsafe(32)
        now = self.clock()
        self._tickets = {key: expiry for key, expiry in self._tickets.items() if expiry > now}
        self._tickets[ticket] = now + self.ticket_ttl
        return ticket

    def create(self) -> tuple[str, str]:
        """Create a session directly (used for loopback-only automatic login)."""
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        self._sessions[session_id] = {"csrf": csrf_token, "expires_at": self.clock() + self.session_ttl}
        return session_id, csrf_token

    def exchange(self, ticket: str) -> tuple[str, str] | None:
        """Consume a ticket and return (session_id, csrf_token), or None when invalid."""
        expiry = self._tickets.pop(ticket, None)
        if expiry is None or expiry <= self.clock():
            return None
        return self.create()

    def validate(self, session_id: str | None) -> str | None:
        """Return the session CSRF token when the session is still valid."""
        if not session_id:
            return None
        record = self._sessions.get(session_id)
        if record is None:
            return None
        if record["expires_at"] <= self.clock():
            self._sessions.pop(session_id, None)
            return None
        return record["csrf"]

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def revoke_all(self) -> None:
        self._sessions.clear()
        self._tickets.clear()
