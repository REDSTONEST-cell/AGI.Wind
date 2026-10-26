"""Verify the realtime-call speech path streams Breeze WebSocket audio.

Requires the live local services: Gateway, Breeze TTS (HTTP+WS) and the ASR
sidecar (the call transport connects to it on connect). Run after starting
`启动本地服务.cmd` and `启动语音服务`:

    app\\.venv\\Scripts\\python.exe scripts\\verify-call-stream.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
TOKEN = (ROOT / "data" / "access-token.txt").read_text(encoding="utf-8").strip()
BASE = "http://127.0.0.1:18440"
WS = "ws://127.0.0.1:18440/api/v1/call/stream"
TEXT = (
    "哥哥，我现在是在通话里跟你说话，你听得见吗？今天风有点大，"
    "我这边把窗户关小了一点。你那边要是冷的话，记得多穿一件衣服，别总觉得自己扛得住。"
)


async def main():
    async with httpx.AsyncClient(base_url=BASE, headers={"Authorization": "Bearer " + TOKEN}, timeout=30) as client:
        status = (await client.get("/api/v1/call/status")).json()
        if not status.get("available"):
            print("SKIP: call services unavailable:", json.dumps(status, ensure_ascii=False))
            return 1
        companions = (await client.get("/api/v1/companions")).json()
        active = (await client.get("/api/v1/conversations", params={"companion_id": companions[0]["id"], "state": "active"})).json()
        if not active:
            print("SKIP: no active conversation")
            return 1
        issued = (await client.post("/api/v1/call/ticket", json={"conversation_id": active[0]["id"]})).json()

    frames = []
    marks = {}
    async with websockets.connect(WS, max_size=2**24) as ws:
        await ws.send(json.dumps({"ticket": issued["ticket"]}))
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert hello.get("type") == "ready", hello
        started = asyncio.get_running_loop().time()
        # Deliberately send raw text deltas as the browser would.
        for index in range(0, len(TEXT), 3):
            await ws.send(json.dumps({"type": "speech_text", "epoch": 0, "text": TEXT[index:index + 3]}))
            await asyncio.sleep(0.01)
        await ws.send(json.dumps({"type": "speech_flush", "epoch": 0, "text": ""}))
        deadline = started + 20
        while asyncio.get_running_loop().time() < deadline:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                break
            now = asyncio.get_running_loop().time() - started
            if isinstance(message, bytes):
                frames.append(now)
                marks.setdefault("first_audio_ms", round(now * 1000))
            else:
                value = json.loads(message)
                if value.get("type") == "error":
                    print("FAIL: call speech error", value)
                    return 1
                if value.get("type") == "speech_reset":
                    break
        await ws.send(json.dumps({"type": "cancel", "epoch": 1}))
        await asyncio.sleep(0.3)

    if not frames:
        print("FAIL: no PCM frames arrived")
        return 1
    gaps = [round((b - a) * 1000) for a, b in zip(frames, frames[1:])]
    print("frames:", len(frames), "first_audio_ms:", marks.get("first_audio_ms"), "max_gap_ms:", max(gaps) if gaps else None)
    assert marks.get("first_audio_ms", 10**9) < 2000, "first audio packet is too slow"
    assert not gaps or max(gaps) < 900, "audio arrival stalled"
    print("call speech streaming: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
