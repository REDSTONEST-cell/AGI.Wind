"""End-to-end acceptance for the Agent command-line tool and approval chain.

Same safety contract as accept-agent-files.py: temporary conversation, memory
extraction disabled during the test, settings/active conversation restored
afterwards. The model runs a harmless echo command after approval, then a second
command is denied and must not execute.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:18440"
TOKEN = (ROOT / "data" / "access-token.txt").read_text(encoding="utf-8").strip()
HEADERS = {"Authorization": "Bearer " + TOKEN}


def call(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:500]}")
    return response.json() if response.content else None


def wait_terminal(client, generation_id, *, allow, timeout=180):
    deadline = time.monotonic() + timeout
    decided = 0
    while time.monotonic() < deadline:
        items = call(client, "GET", "/api/v1/agent/approvals")["items"]
        for item in items:
            if item.get("generation_id") != str(generation_id):
                continue
            print("approval:", item["operation"], json.dumps(item["details"], ensure_ascii=False))
            call(client, "POST", f"/api/v1/agent/approvals/{item['id']}", json={"allow": allow})
            decided += 1
        state = call(client, "GET", f"/api/v1/generations/{generation_id}")["state"]
        if state in {"completed", "failed", "cancelled", "interrupted"}:
            return state, decided
        time.sleep(0.5)
    raise TimeoutError("generation did not finish")


def assistant_event(client, conversation_id):
    rows = call(client, "GET", f"/api/v1/conversations/{conversation_id}/events", params={"limit": 200})
    for row in reversed(rows):
        if row["role"] == "assistant":
            return row
    return None


def steps(event):
    content = event.get("content") or {}
    return [step for step in content.get("trace") or [] if step.get("type") == "tool"]


def main():
    client = httpx.Client(base_url=BASE, headers=HEADERS, timeout=60)
    original_active = None
    memory_enabled = None
    command_mode = None
    conversation_id = None
    try:
        companion_id = call(client, "GET", "/api/v1/companions")[0]["id"]
        active = call(client, "GET", "/api/v1/conversations", params={"companion_id": companion_id, "state": "active"})
        original_active = active[0]["id"] if active else None
        settings = call(client, "GET", "/api/v1/agent/settings")
        command_mode = settings["command_access_mode"]
        memory_enabled = call(client, "GET", "/api/v1/memory/settings").get("extraction_enabled")

        call(client, "PATCH", "/api/v1/memory/settings", json={"extraction_enabled": False})
        call(client, "PATCH", "/api/v1/agent/settings", json={"enabled": True, "command_access_mode": "ask"})

        conversation_id = call(client, "POST", "/api/v1/conversations", json={"companion_id": companion_id, "title": "agent-command-e2e"})["conversation_id"]

        prompt = "请用 run_command 工具执行命令 `echo agent-command-ok`，然后只告诉我退出码和输出。"
        accepted = call(client, "POST", "/api/v1/chat/requests", json={
            "request_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "input": {"text": prompt},
            "output": {"voice": "off"},
            "device_id": "agent-command-e2e",
        })
        state, approved = wait_terminal(client, accepted["generation_id"], allow=True)
        assert state == "completed", state
        assert approved >= 1, "no command approval was requested"
        event = assistant_event(client, conversation_id)
        text = (event.get("content") or {}).get("text", "")
        assert text.strip(), "empty assistant reply after approved command"
        trace = steps(event)
        completed = [step for step in trace if step.get("tool") == "run_command" and step.get("status") == "completed"]
        assert completed, f"run_command did not complete: {trace}"
        saved = completed[0].get("result") or {}
        assert saved.get("exit_code") == 0, saved
        assert "stdout" not in saved, "raw stdout must not be copied into the durable trace"
        print("turn1 reply:", text.strip()[:160].replace("\n", " "))
        print("turn1 tool:", json.dumps({key: completed[0].get(key) for key in ("tool", "status", "summary")}, ensure_ascii=False), "trace_result:", json.dumps(saved, ensure_ascii=False))

        prompt = "请再用 run_command 执行 `echo denied-command`，我要验证被拒绝时不会执行。"
        accepted = call(client, "POST", "/api/v1/chat/requests", json={
            "request_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "input": {"text": prompt},
            "output": {"voice": "off"},
            "device_id": "agent-command-e2e",
        })
        state, denied = wait_terminal(client, accepted["generation_id"], allow=False)
        assert state == "completed", state
        assert denied >= 1, "no command approval was requested for the deny turn"
        event = assistant_event(client, conversation_id)
        text = (event.get("content") or {}).get("text", "")
        assert text.strip(), "empty assistant reply after denied command"
        failed = [step for step in steps(event) if step.get("tool") == "run_command" and step.get("status") == "failed"]
        assert failed, "denied command left no failed trace"
        assert failed[0].get("code") in {"TOOL_APPROVAL_DENIED", "TOOL_APPROVAL_TIMEOUT"}, failed[0]
        print("turn2 reply:", text.strip()[:160].replace("\n", " "))
        print("turn2 tool:", json.dumps({key: failed[0].get(key) for key in ("tool", "status", "code")}, ensure_ascii=False))
        print("agent command approval E2E: PASS")
    finally:
        try:
            if conversation_id:
                call(client, "DELETE", f"/api/v1/conversations/{conversation_id}")
                print("temporary conversation deleted")
        except Exception as error:  # noqa: BLE001
            print("cleanup conversation failed:", error, file=sys.stderr)
        try:
            if original_active:
                call(client, "PATCH", f"/api/v1/conversations/{original_active}", json={"state": "active"})
                print("previous active conversation restored")
        except Exception as error:  # noqa: BLE001
            print("restore active conversation failed:", error, file=sys.stderr)
        try:
            if command_mode is not None:
                call(client, "PATCH", "/api/v1/agent/settings", json={"command_access_mode": command_mode})
            if memory_enabled is not None:
                call(client, "PATCH", "/api/v1/memory/settings", json={"extraction_enabled": memory_enabled})
        except Exception as error:  # noqa: BLE001
            print("restore settings failed:", error, file=sys.stderr)
        client.close()


if __name__ == "__main__":
    main()
