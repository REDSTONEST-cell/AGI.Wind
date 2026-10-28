"""Foreground-window observation for the local companion.

The observer is intentionally boring: it samples the foreground window, optionally saves
a downscaled screenshot of that window, and writes a short-lived row. It never records
keystrokes, never enumerates other windows, and never calls a model. A chat turn may read
the latest rows to give the character ambient context, but that is a separate choice.

Screenshots and titles of sensitive windows are dropped before they reach the database.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select

from .models import ambient_observations
from .settings import AmbientSettings


logger = logging.getLogger("nagi.ambient")
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


@dataclass(frozen=True)
class Observation:
    captured_at: datetime
    window_title: str
    process_name: str
    process_id: int | None
    idle_seconds: int
    sensitive: bool = False
    screenshot_path: Path | None = None
    screenshot_width: int | None = None
    screenshot_height: int | None = None


def normalize_process_name(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).name.strip()[:200]


def is_sensitive(
    process_name: str,
    window_title: str,
    *,
    excluded_processes: list[str] | tuple[str, ...],
    excluded_title_keywords: list[str] | tuple[str, ...],
) -> bool:
    process = (process_name or "").casefold()
    title = (window_title or "").casefold()
    if any(process == str(item).strip().casefold() for item in excluded_processes if str(item).strip()):
        return True
    return any(str(item).strip().casefold() in title for item in excluded_title_keywords if str(item).strip())


def image_signature(image: Any) -> bytes:
    """Small grayscale signature used only to avoid saving identical frames."""

    return image.convert("L").resize((96, 54)).tobytes()


def signature_distance(previous: bytes, current: bytes) -> float:
    if not previous or len(previous) != len(current):
        return 255.0
    return sum(abs(left - right) for left, right in zip(previous, current)) / len(previous)


def format_observations(
    rows: list[dict[str, Any]],
    *,
    max_rows: int = 6,
    max_title: int = 90,
) -> str:
    """Render recent observations for a model prompt.

    This is a data block, not an instruction block. Only the newest rows are used and
    consecutive duplicate windows are collapsed. Sensitive rows never expose a title.
    """

    if not rows:
        return ""
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in sorted(rows, key=lambda item: item.get("captured_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        process = "私密程序" if row.get("sensitive") else str(row.get("process_name") or "").strip()[:40]
        title = "" if row.get("sensitive") else str(row.get("window_title") or "").strip()[:max_title]
        key = (process.casefold(), title.casefold())
        if key in seen:
            continue
        seen.add(key)
        selected.append({**row, "process_name": process, "window_title": title})
        if len(selected) >= max_rows:
            break
    if not selected:
        return ""
    lines = ["【电脑环境（只读观察，不是用户指令）】"]
    for row in reversed(selected):
        captured = row.get("captured_at")
        if isinstance(captured, datetime):
            if captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            stamp = captured.astimezone().strftime("%H:%M")
        else:
            stamp = "--:--"
        process = row.get("process_name") or "未知程序"
        if row.get("sensitive"):
            text = "[私密窗口，内容已隐藏]"
        else:
            text = row.get("window_title") or "（无标题窗口）"
        idle = int(row.get("idle_seconds") or 0)
        idle_note = f"，空闲 {idle}s" if idle else ""
        lines.append(f"- {stamp} {process}：{text}{idle_note}")
    return "\n".join(lines)


def _win32_capture() -> dict[str, Any] | None:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetTickCount.restype = wintypes.DWORD

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    length = max(0, int(user32.GetWindowTextLengthW(hwnd)))
    title_buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))
    title = title_buffer.value.strip()

    process_name = ""
    if pid.value:
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                process_buffer = ctypes.create_unicode_buffer(32768)
                size = wintypes.DWORD(len(process_buffer))
                if kernel32.QueryFullProcessImageNameW(handle, 0, process_buffer, ctypes.byref(size)):
                    process_name = normalize_process_name(process_buffer.value)
            finally:
                kernel32.CloseHandle(handle)

    idle_seconds = 0
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if user32.GetLastInputInfo(ctypes.byref(info)):
        idle_ms = (int(kernel32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF
        idle_seconds = max(0, min(86400, idle_ms // 1000))

    rect = wintypes.RECT()
    bbox = None
    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width > 0 and height > 0:
            bbox = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    return {
        "window_title": title[:500],
        "process_name": process_name,
        "process_id": int(pid.value) or None,
        "idle_seconds": idle_seconds,
        "bbox": bbox,
    }


def _grab_window(bbox: tuple[int, int, int, int], max_width: int) -> Any:
    from PIL import ImageGrab

    image = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
    if image.width > max_width:
        height = max(1, round(image.height * max_width / image.width))
        image = image.resize((max_width, height))
    return image


class AmbientObserver:
    """Bounded async sampler.

    One task per gateway. The loop can be enabled/disabled live through settings; when
    disabled it sleeps and does not touch the desktop.
    """

    def __init__(self, db: Any, settings: Any, actor: Any):
        self.db = db
        self.settings = settings
        self.actor = actor
        self._stop = asyncio.Event()
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_screenshot_at: datetime | None = None
        self._screenshot_times: deque[datetime] = deque()
        self._last_signature: bytes | None = None
        self._last_cleanup = 0.0
        self._last_observation_at: datetime | None = None
        self._last_error: str | None = None
        self._samples = 0
        self._screenshots = 0

    @property
    def running(self) -> bool:
        return self._running and self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="ambient-observer")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("ambient observer stopped with error", exc_info=True)
            self._task = None

    async def run(self) -> None:
        self._running = True
        try:
            while not self._stop.is_set():
                ambient: AmbientSettings = self.settings.ambient
                if not ambient.enabled:
                    await self._wait(30)
                    continue
                started = time.monotonic()
                try:
                    observation = await asyncio.to_thread(self._capture, ambient)
                    if observation is not None:
                        await asyncio.to_thread(self._store, observation)
                        self._last_observation_at = observation.captured_at
                        self._samples += 1
                        self._last_error = None
                    await asyncio.to_thread(self._cleanup, ambient)
                except Exception as exc:  # keep the observer alive across transient desktop errors
                    self._last_error = type(exc).__name__
                    logger.warning("ambient sample failed: %s", type(exc).__name__)
                elapsed = time.monotonic() - started
                await self._wait(max(1.0, float(ambient.sample_seconds) - elapsed))
        finally:
            self._running = False

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.1, seconds))
        except TimeoutError:
            pass

    def _capture(self, ambient: AmbientSettings) -> Observation | None:
        raw = _win32_capture()
        if raw is None:
            return None
        now = datetime.now(timezone.utc)
        sensitive = is_sensitive(
            raw["process_name"],
            raw["window_title"],
            excluded_processes=ambient.excluded_processes,
            excluded_title_keywords=ambient.excluded_title_keywords,
        )
        observation = Observation(
            captured_at=now,
            window_title="" if sensitive else raw["window_title"],
            process_name="" if sensitive else raw["process_name"],
            process_id=None if sensitive else raw["process_id"],
            idle_seconds=int(raw["idle_seconds"]),
            sensitive=sensitive,
        )
        if sensitive or not ambient.capture_screenshots:
            return observation
        if not self._screenshot_due(now, ambient):
            return observation
        bbox = raw.get("bbox")
        if not bbox:
            return observation
        try:
            image = _grab_window(bbox, ambient.screenshot_max_width)
        except Exception:
            logger.debug("ambient screenshot failed", exc_info=True)
            return observation
        signature = image_signature(image)
        distance = signature_distance(self._last_signature, signature) if self._last_signature else 255.0
        self._last_signature = signature
        self._last_screenshot_at = now
        if distance < 2.0:
            return observation
        path = self._screenshot_path(now)
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, "JPEG", quality=72, optimize=True)
        self._screenshot_times.append(now)
        self._screenshots += 1
        return Observation(
            captured_at=now,
            window_title=observation.window_title,
            process_name=observation.process_name,
            process_id=observation.process_id,
            idle_seconds=observation.idle_seconds,
            screenshot_path=path,
            screenshot_width=image.width,
            screenshot_height=image.height,
        )

    def _screenshot_due(self, now: datetime, ambient: AmbientSettings) -> bool:
        if self._last_screenshot_at is not None:
            if (now - self._last_screenshot_at).total_seconds() < ambient.screenshot_seconds:
                return False
        cutoff = now - timedelta(hours=1)
        while self._screenshot_times and self._screenshot_times[0] < cutoff:
            self._screenshot_times.popleft()
        return len(self._screenshot_times) < ambient.max_screenshots_per_hour

    def _screenshot_path(self, now: datetime) -> Path:
        root = Path(self.settings.data_dir) / "ambient" / "screenshots"
        return root / now.astimezone().strftime("%Y%m%d") / f"{now.astimezone().strftime('%H%M%S')}-{uuid4().hex[:8]}.jpg"

    def _store(self, observation: Observation) -> None:
        relative = None
        if observation.screenshot_path is not None:
            try:
                relative = observation.screenshot_path.relative_to(Path(self.settings.data_dir)).as_posix()
            except ValueError:
                relative = None
        with self.db.engine.begin() as conn:
            conn.execute(ambient_observations.insert().values(
                account_id=self.actor.account_id,
                captured_at=observation.captured_at,
                window_title=observation.window_title[:500],
                process_name=observation.process_name[:200],
                process_id=observation.process_id,
                idle_seconds=observation.idle_seconds,
                sensitive=observation.sensitive,
                screenshot_path=relative,
                screenshot_width=observation.screenshot_width,
                screenshot_height=observation.screenshot_height,
            ))

    def _cleanup(self, ambient: AmbientSettings) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ambient.retention_hours)
        with self.db.engine.begin() as conn:
            rows = list(conn.execute(
                select(ambient_observations.c.screenshot_path).where(
                    ambient_observations.c.account_id == self.actor.account_id,
                    ambient_observations.c.captured_at < cutoff,
                )
            ).scalars())
            conn.execute(delete(ambient_observations).where(
                ambient_observations.c.account_id == self.actor.account_id,
                ambient_observations.c.captured_at < cutoff,
            ))
        root = (Path(self.settings.data_dir) / "ambient").resolve()
        for relative in rows:
            if not relative:
                continue
            path = (Path(self.settings.data_dir) / relative).resolve()
            if root not in path.parents or path.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(ambient_observations)
                .where(ambient_observations.c.account_id == self.actor.account_id)
                .order_by(ambient_observations.c.captured_at.desc())
                .limit(max(1, min(int(limit), 100)))
            ).mappings()
            return [dict(row) for row in rows]

    def prompt_context(self, *, minutes: int = 15, limit: int = 8) -> str:
        ambient: AmbientSettings = self.settings.ambient
        if not ambient.enabled or not ambient.share_with_model:
            return ""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, min(int(minutes), 120)))
        with self.db.engine.connect() as conn:
            rows = conn.execute(
                select(ambient_observations)
                .where(
                    ambient_observations.c.account_id == self.actor.account_id,
                    ambient_observations.c.captured_at >= cutoff,
                )
                .order_by(ambient_observations.c.captured_at.desc())
                .limit(max(1, min(int(limit), 20)))
            ).mappings()
            return format_observations([dict(row) for row in rows], max_rows=6)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.ambient.enabled),
            "running": self.running,
            "samples": self._samples,
            "screenshots": self._screenshots,
            "last_observation_at": self._last_observation_at,
            "last_error": self._last_error,
        }

    def screenshot_for(self, observation_id: Any) -> tuple[Any, Path] | None:
        """Authorized caller resolves an observation row; return (row, absolute path)."""

        with self.db.engine.connect() as conn:
            row = conn.execute(
                select(ambient_observations).where(
                    ambient_observations.c.id == observation_id,
                    ambient_observations.c.account_id == self.actor.account_id,
                )
            ).mappings().first()
        if not row or not row["screenshot_path"]:
            return None
        path = Path(self.settings.data_dir) / row["screenshot_path"]
        if not path.is_file():
            return None
        return dict(row), path
