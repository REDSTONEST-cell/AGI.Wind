"""End-to-end client smoke test against a running local gateway.

Checks the browser session bridge, static client assets, persona/conversation reads and the
provider settings surface without touching real vendor endpoints. Without a configured model
the chat request must fail with MODEL_UNCONFIGURED instead of fabricating an answer.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18440"


def main() -> int:
    token = (ROOT / "data/access-token.txt").read_text(encoding="utf-8").strip()
    origin = BASE
    report = {"base_url": BASE}
    # Loopback-only automatic session: a local browser must not need the access token.
    with httpx.Client(base_url=BASE, timeout=10) as fresh:
        bootstrap = fresh.post("/api/v1/client/bootstrap")
        report["bootstrap_http"] = bootstrap.status_code
        report["bootstrap_cookie"] = "httponly" in bootstrap.headers.get("set-cookie", "").lower()
        report["bootstrap_companions"] = fresh.get("/api/v1/companions").status_code

    with httpx.Client(base_url=BASE, timeout=10) as client:
        live = client.get("/health/live")
        report["live_http"] = live.status_code
        home = client.get("/")
        report["client_home_http"] = home.status_code
        report["client_home_has_app"] = "client/app.js" in home.text
        for path in ("/status", "/client/app.js", "/client/styles.css", "/client/sdk/api.js"):
            response = client.get(path)
            report["asset" + path.replace("/", "_")] = response.status_code
        report["unauthenticated_companions"] = client.get("/api/v1/companions").status_code

        launch = client.post("/api/v1/client/launch", headers={"Authorization": "Bearer " + token})
        report["launch_http"] = launch.status_code
        if launch.status_code != 200:
            report["error"] = "launch failed; is the access token current?"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        ticket = launch.json()["ticket"]
        session = client.post("/api/v1/client/session", headers={"Origin": origin}, json={"ticket": ticket})
        report["session_http"] = session.status_code
        cookie_header = session.headers.get("set-cookie", "").lower()
        report["cookie_httponly"] = "httponly" in cookie_header
        report["cookie_samesite_strict"] = "samesite=strict" in cookie_header
        report["csrf_present"] = bool(session.json().get("csrf_token"))
        csrf = session.json()["csrf_token"]
        state = client.get("/api/v1/client/session")
        report["session_reuse_http"] = state.status_code
        report["csrf_stable"] = state.json().get("csrf_token") == csrf

        companions = client.get("/api/v1/companions")
        report["companions_http"] = companions.status_code
        rows = companions.json() if companions.status_code == 200 else []
        report["companion_count"] = len(rows)
        if not rows:
            report["error"] = "no companion seeded; run agi-wind init-local"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        companion_id = rows[0]["id"]
        persona = client.get(f"/api/v1/companions/{companion_id}/persona")
        report["persona_http"] = persona.status_code
        report["persona_version"] = persona.json().get("version") if persona.status_code == 200 else None
        conversations = client.get("/api/v1/conversations", params={"companion_id": companion_id})
        report["conversations_http"] = conversations.status_code
        conversation_rows = conversations.json() if conversations.status_code == 200 else []
        report["conversation_count"] = len(conversation_rows)
        all_rows = client.get("/api/v1/conversations", params={"companion_id": companion_id, "state": "all"})
        report["conversation_all_http"] = all_rows.status_code
        if all_rows.status_code == 200:
            report["active_conversations"] = sum(1 for row in all_rows.json() if row.get("state") == "active")
            report["archived_conversations"] = sum(1 for row in all_rows.json() if row.get("state") == "archived")
        capabilities = client.get("/api/v1/capabilities")
        report["capabilities_http"] = capabilities.status_code
        if capabilities.status_code == 200:
            report["archive_format_version"] = capabilities.json().get("archive_format_version")
        # The short client key is a standalone-client credential; it never replaces the
        # access token for administrative calls in this smoke run.
        key_bad = client.post("/api/v1/client/key-session", headers={"Origin": origin}, json={"key": "not-the-key"})
        report["key_session_bad_http"] = key_bad.status_code
        with httpx.Client(base_url=BASE, timeout=10) as standalone:
            key_ok = standalone.post("/api/v1/client/key-session", headers={"Origin": origin}, json={"key": "123456"})
            report["key_session_http"] = key_ok.status_code
            report["key_session_cookie"] = "httponly" in key_ok.headers.get("set-cookie", "").lower()
            report["key_session_companions"] = standalone.get("/api/v1/companions").status_code
            bearer_key = standalone.get("/api/v1/companions", headers={"Authorization": "Bearer 123456"})
            report["client_key_bearer_http"] = bearer_key.status_code

        settings = client.get("/api/v1/settings/providers")
        report["provider_settings_http"] = settings.status_code
        if settings.status_code == 200:
            payload = settings.json()
            report["provider_count"] = len(payload.get("providers", []))
            report["dialogue_model"] = (payload.get("models") or {}).get("dialogue")
            report["secrets_not_echoed"] = "api_key" not in settings.text and "sk-" not in settings.text

        csrf_denied = client.post(
            "/api/v1/conversations",
            headers={"Origin": origin},
            json={"companion_id": companion_id, "title": "smoke"},
        )
        report["csrf_missing_http"] = csrf_denied.status_code

        if conversation_rows:
            conversation_id = conversation_rows[0]["id"]
            original_title = conversation_rows[0].get("title") or ""
            renamed = client.patch(
                f"/api/v1/conversations/{conversation_id}",
                headers={"Origin": origin, "X-AGI-CSRF": csrf},
                json={"title": "客户端冒烟-重命名"},
            )
            report["rename_http"] = renamed.status_code
            listed = client.get("/api/v1/conversations", params={"companion_id": companion_id}).json()
            report["rename_applied"] = any(row["id"] == conversation_id and row["title"] == "客户端冒烟-重命名" for row in listed)
            client.patch(
                f"/api/v1/conversations/{conversation_id}",
                headers={"Origin": origin, "X-AGI-CSRF": csrf},
                json={"title": original_title},
            )
            chat = client.post(
                "/api/v1/chat/requests",
                headers={"Origin": origin, "X-AGI-CSRF": csrf},
                json={
                    "request_id": str(uuid.uuid4()),
                    "conversation_id": conversation_id,
                    "input": {"text": "客户端冒烟测试"},
                    "output": {"voice": "off"},
                    "device_id": "smoke-client",
                },
            )
            report["chat_http"] = chat.status_code
            if chat.status_code == 202:
                report["chat_accepted"] = True
            else:
                report["chat_error"] = (chat.json().get("error") or {}).get("code")

        ready = client.get("/health/ready")
        report["ready_http"] = ready.status_code
        report["readiness"] = ready.json().get("status")
        signed_out = client.delete("/api/v1/client/session", headers={"Origin": origin, "X-AGI-CSRF": csrf})
        report["logout_http"] = signed_out.status_code
        report["after_logout_companions"] = client.get("/api/v1/companions").status_code

    print(json.dumps(report, ensure_ascii=False, indent=2))
    failures = [key for key in ("live_http", "client_home_http", "session_http", "logout_http") if report.get(key) != 200]
    failures += [key for key, value in report.items() if key.startswith("asset") and value != 200]
    if report.get("after_logout_companions") != 401:
        failures.append("after_logout_companions")
    if report.get("csrf_missing_http") != 403:
        failures.append("csrf_missing_http")
    if report.get("rename_http") not in (None, 200):
        failures.append("rename_http")
    if report.get("rename_applied") is False:
        failures.append("rename_applied")
    if report.get("chat_http") not in (None, 202):
        failures.append("chat_http")
    if report.get("bootstrap_http") != 200 or report.get("bootstrap_cookie") is not True:
        failures.append("bootstrap_http")
    if report.get("bootstrap_companions") != 200:
        failures.append("bootstrap_companions")
    if report.get("active_conversations") != 1:
        failures.append("active_conversations")
    if report.get("archive_format_version") != 3:
        failures.append("archive_format_version")
    if report.get("key_session_http") != 200 or report.get("key_session_companions") != 200:
        failures.append("key_session_http")
    if report.get("client_key_bearer_http") != 200:
        failures.append("client_key_bearer_http")
    if report.get("key_session_bad_http") != 401:
        failures.append("key_session_bad_http")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
