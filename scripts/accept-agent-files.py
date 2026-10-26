"""End-to-end acceptance for the Agent computer-file tools and approval chain.

Runs against a live local gateway with the real dialogue model:
1. disables automatic memory extraction so the synthetic test conversation cannot
   become long-term memory, then creates a temporary conversation;
2. asks the model to call ``list_files``; approves the pending request and verifies
   the tool trace + final answer;
3. asks again and denies the request, verifying the turn still completes with a
   failed tool step and a normal reply;
4. deletes the temporary conversation and restores the previous active conversation,
   agent file-access mode and memory-extraction setting.
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
TARGET_DIR = str(Path.home() / "Desktop")


def call(client, method, path, **kwargs):
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:500]}")
    return response.json() if response.content else None


def wait_terminal(client, generation_id, *, approvals, allow, timeout=180):
    """Poll one generation; decide every approval it raises with the same verdict."""
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


def trace_steps(event):
    content = event.get("content") or {}
    return [step for step in content.get("trace") or [] if step.get("type") == "tool"]


def main():
    client = httpx.Client(base_url=BASE, headers=HEADERS, timeout=60)
    original_active = None
    memory_enabled = None
    file_mode = None
    conversation_id = None
    try:
        companions = call(client, "GET", "/api/v1/companions")
        companion_id = companions[0]["id"]
        active = call(client, "GET", "/api/v1/conversations", params={"companion_id": companion_id, "state": "active"})
        original_active = active[0]["id"] if active else None
        settings = call(client, "GET", "/api/v1/agent/settings")
        file_mode = settings["file_access_mode"]
        memory = call(client, "GET", "/api/v1/memory/settings")
        memory_enabled = memory.get("extraction_enabled")

        call(client, "PATCH", "/api/v1/memory/settings", json={"extraction_enabled": False})
        call(client, "PATCH", "/api/v1/agent/settings", json={"enabled": True, "file_access_mode": "ask"})

        created = call(client, "POST", "/api/v1/conversations", json={"companion_id": companion_id, "title": "agent-e2e"})
        conversation_id = created["conversation_id"]

        # Turn 1: approve one list_files request.
        prompt = f"请调用 list_files 工具列出 {TARGET_DIR} 目录（最多20项），然后只根据工具结果告诉我其中 3 个名字。"
        accepted = call(client, "POST", "/api/v1/chat/requests", json={
            "request_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "input": {"text": prompt},
            "output": {"voice": "off"},
            "device_id": "agent-e2e",
        })
        generation_id = accepted["generation_id"]
        state, approved = wait_terminal(client, generation_id, approvals=True, allow=True)
        assert state == "completed", state
        assert approved >= 1, "no approval was requested"
        event = assistant_event(client, conversation_id)
        assert event is not None
        text = (event.get("content") or {}).get("text", "")
        assert text.strip(), "empty assistant reply after approved tool call"
        steps = trace_steps(event)
        completed = [step for step in steps if step.get("tool") == "list_files" and step.get("status") == "completed"]
        assert completed, f"list_files did not complete: {steps}"
        print("turn1 reply:", text.strip()[:200].replace("\n", " "))
        print("turn1 tool:", json.dumps({key: completed[0].get(key) for key in ("tool", "status", "summary")}, ensure_ascii=False))

        # Turn 2: deny one list_files request.
        prompt = f"请再次调用 list_files 工具列出 {TARGET_DIR}，我要看看审批被拒绝时你会怎么处理。"
        accepted = call(client, "POST", "/api/v1/chat/requests", json={
            "request_id": str(uuid.uuid4()),
            "conversation_id": conversation_id,
            "input": {"text": prompt},
            "output": {"voice": "off"},
            "device_id": "agent-e2e",
        })
        generation_id = accepted["generation_id"]
        state, denied = wait_terminal(client, generation_id, approvals=True, allow=False)
        assert state == "completed", state
        assert denied >= 1, "no approval was requested for the deny turn"
        event = assistant_event(client, conversation_id)
        text = (event.get("content") or {}).get("text", "")
        assert text.strip(), "empty assistant reply after denied tool call"
        steps = trace_steps(event)
        failed = [step for step in steps if step.get("tool") == "list_files" and step.get("status") == "failed"]
        assert failed, f"denied list_files did not leave a failed trace: {steps}"
        assert failed[0].get("code") in {"TOOL_APPROVAL_DENIED", "TOOL_APPROVAL_TIMEOUT"}, failed[0]
        print("turn2 reply:", text.strip()[:200].replace("\n", " "))
        print("turn2 tool:", json.dumps({key: failed[0].get(key) for key in ("tool", "status", "code")}, ensure_ascii=False))
        print("agent file approval E2E: PASS")
    finally:
        try:
            if conversation_id:
                call(client, "DELETE", f"/api/v1/conversations/{conversation_id}")
                print("temporary conversation deleted")
        except Exception as error:  # noqa: BLE001 - cleanup must not mask the real result
            print("cleanup conversation failed:", error, file=sys.stderr)
        try:
            if original_active:
                call(client, "PATCH", f"/api/v1/conversations/{original_active}", json={"state": "active"})
                print("previous active conversation restored")
        except Exception as error:  # noqa: BLE001
            print("restore active conversation failed:", error, file=sys.stderr)
        try:
            if file_mode is not None:
                call(client, "PATCH", "/api/v1/agent/settings", json={"file_access_mode": file_mode})
            if memory_enabled is not None:
                call(client, "PATCH", "/api/v1/memory/settings", json={"extraction_enabled": memory_enabled})
        except Exception as error:  # noqa: BLE001
            print("restore settings failed:", error, file=sys.stderr)
        client.close()


if __name__ == "__main__":
    main()
