"""Real Breeze voice smoke check against a running Gateway.

Default is read-only audio generation: settings, voice list, happy/sad previews.
Use --chat to also send one synthetic chat turn with output.voice=always; that writes a
real message to the current active conversation, so it is opt-in.
"""
import argparse
import io
import json
import time
import uuid
import wave
from pathlib import Path

import httpx


def wav_duration(raw: bytes) -> tuple[float, int, int]:
    with wave.open(io.BytesIO(raw), "rb") as stream:
        return stream.getnframes() / stream.getframerate(), stream.getframerate(), stream.getnchannels()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:18440")
    parser.add_argument("--chat", action="store_true")
    parser.add_argument("--text", default="哥哥，今天也辛苦啦。外面风有点凉，记得喝一点热水。")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    token = (root / "data/access-token.txt").read_text(encoding="utf-8").strip()
    headers = {"Authorization": "Bearer " + token}
    out_dir = root / "data/voice"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"base": args.base, "previews": {}, "chat": None}
    with httpx.Client(base_url=args.base, headers=headers, timeout=180.0) as client:
        ready = client.get("/health/ready")
        report["ready_http"] = ready.status_code
        report["dependencies"] = (ready.json().get("dependencies") if ready.status_code == 200 else None)
        settings = client.get("/api/v1/voice/settings")
        report["settings_http"] = settings.status_code
        report["voice_settings"] = settings.json() if settings.status_code == 200 else None
        voices = client.get("/api/v1/voices")
        report["voices_http"] = voices.status_code
        report["voices"] = voices.json() if voices.status_code == 200 else None
        for emotion in ("开心", "难过"):
            started = time.perf_counter()
            response = client.post("/api/v1/voice/preview", json={"text": args.text, "emotion": emotion})
            elapsed = round((time.perf_counter() - started) * 1000)
            path = out_dir / ("preview-smoke-" + ("happy" if emotion == "开心" else "sad") + ".wav")
            if response.status_code == 200:
                path.write_bytes(response.content)
                duration, rate, channels = wav_duration(response.content)
                report["previews"][emotion] = {
                    "status": response.status_code,
                    "duration_ms": round(duration * 1000),
                    "sample_rate": rate,
                    "channels": channels,
                    "total_ms": elapsed,
                    "path": str(path.relative_to(root)),
                }
            else:
                report["previews"][emotion] = {"status": response.status_code, "error": response.text[:300]}
        if args.chat:
            # The API needs a companion id; resolve it from the same endpoint the client uses.
            companions = client.get("/api/v1/companions")
            companion_id = companions.json()[0]["id"] if companions.status_code == 200 and companions.json() else None
            active = client.get("/api/v1/conversations", params={"companion_id": companion_id}) if companion_id else None
            if not active or active.status_code != 200 or not active.json():
                report["chat"] = {"error": "no active conversation"}
            else:
                conversation_id = active.json()[0]["id"]
                accepted = client.post(
                    "/api/v1/chat/requests",
                    json={
                        "request_id": str(uuid.uuid4()),
                        "conversation_id": conversation_id,
                        "input": {"text": "这是一条语音链路冒烟测试，请用一句话回应。"},
                        "output": {"voice": "always"},
                        "device_id": "smoke-voice",
                    },
                )
                report["chat"] = {"accepted_status": accepted.status_code}
                if accepted.status_code == 202:
                    generation_id = accepted.json()["generation_id"]
                    deadline = time.monotonic() + 180
                    while time.monotonic() < deadline:
                        status = client.get("/api/v1/generations/" + generation_id + "/speech").json()
                        if status.get("state") in ("ready", "failed", "skipped", "cancelled", "none"):
                            break
                        time.sleep(0.7)
                    report["chat"] = {"generation_id": generation_id, "speech": status}
                    attachment = (status or {}).get("attachment")
                    if attachment:
                        audio = client.get("/api/v1/assets/" + attachment["asset_id"])
                        if audio.status_code == 200:
                            path = out_dir / "smoke-chat.wav"
                            path.write_bytes(audio.content)
                            duration, rate, channels = wav_duration(audio.content)
                            report["chat"]["duration_ms"] = round(duration * 1000)
                            report["chat"]["sample_rate"] = rate
                            report["chat"]["channels"] = channels
                            report["chat"]["path"] = str(path.relative_to(root))
    print(json.dumps(report, ensure_ascii=True, indent=2))
    failures = []
    if report.get("ready_http") != 200:
        failures.append("ready_http")
    if (report.get("dependencies") or {}).get("tts") != "available":
        failures.append("tts_not_available")
    if report.get("settings_http") != 200 or not (report.get("voice_settings") or {}).get("available"):
        failures.append("voice_settings")
    for emotion, item in report.get("previews", {}).items():
        if item.get("status") != 200 or not item.get("duration_ms"):
            failures.append("preview_" + emotion)
    if args.chat:
        chat = report.get("chat") or {}
        if chat.get("accepted_status") != 202 or (chat.get("speech") or {}).get("state") != "ready":
            failures.append("chat_speech")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
