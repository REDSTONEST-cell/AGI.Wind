from pathlib import Path

from nagi.api.browser import BrowserSessions


def test_browser_sessions_survive_restart(tmp_path: Path):
    path = tmp_path / "browser-sessions.json"
    first = BrowserSessions(persist_path=path)
    session_id, csrf = first.create()
    sibling_id, sibling_csrf = first.create()

    restarted = BrowserSessions(persist_path=path)
    assert restarted.validate(session_id) == csrf
    assert restarted.validate(sibling_id) == sibling_csrf

    restarted.revoke(session_id)
    assert BrowserSessions(persist_path=path).validate(session_id) is None
    assert BrowserSessions(persist_path=path).validate(sibling_id) == sibling_csrf


def test_browser_sessions_without_persist_path_stay_ephemeral():
    sessions = BrowserSessions()
    session_id, _ = sessions.create()
    assert sessions.validate(session_id) is not None
    assert not sessions._persist_path
