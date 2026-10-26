"""Local usage statistics for the client's read-only usage page.

Everything here is derived from the authoritative archive on demand: no extra
tables, no write path, and no network calls.  Token numbers are estimates based
on archived text (Chinese and English mix); they are labelled as estimates in
the UI and must never be presented as provider-billed usage.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from .archive import models as m
from .delivery import models as dm

# CJK ideographs, kana and compatibility forms: roughly 1.5 characters per token.
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff]")
_LATIN = re.compile(r"[A-Za-z0-9]")
_CJK_PER_TOKEN = 1.5
_LATIN_PER_TOKEN = 4.0

_VISIBLE_MESSAGES = ("user", "assistant")


def estimate_tokens(value: str | None) -> int:
    """Estimate tokens from archived text: CJK ~1.5 chars/token, latin ~4 chars/token."""
    text_value = value or ""
    if not text_value:
        return 0
    cjk = len(_CJK.findall(text_value))
    latin = len(_LATIN.findall(text_value))
    return int(cjk / _CJK_PER_TOKEN + latin / _LATIN_PER_TOKEN + 0.5)


def _today_start(now: datetime | None = None) -> datetime:
    """Local midnight expressed in UTC, because archive timestamps are timezone-aware UTC."""
    local = (now or datetime.now(timezone.utc)).astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def _floor_hour(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _text_column():
    return m.events.c.content.op("->>")("text")


def _model_label(model_ref: Any) -> str:
    if not isinstance(model_ref, dict):
        return "未记录模型"
    model_id = str(model_ref.get("model_id") or "").strip()
    provider_id = str(model_ref.get("provider_id") or "").strip()
    if model_id and provider_id:
        return provider_id + "/" + model_id
    return model_id or provider_id or "未记录模型"


def _message_rows(conn: Connection, account_id) -> list[tuple[str, str | None, datetime | None]]:
    return list(
        conn.execute(
            select(m.events.c.role, _text_column(), m.events.c.occurred_at).where(
                m.events.c.account_id == account_id,
                m.events.c.visibility == "visible",
                m.events.c.role.in_(_VISIBLE_MESSAGES),
            )
        ).all()
    )


def _generation_join():
    return m.generations.join(m.requests, m.generations.c.request_id == m.requests.c.id)


def _count_generations(conn: Connection, account_id, since: datetime | None = None) -> int:
    query = select(func.count()).select_from(_generation_join()).where(m.requests.c.account_id == account_id)
    if since is not None:
        query = query.where(m.generations.c.created_at >= since)
    return int(conn.scalar(query) or 0)


def _count_voice_segments(conn: Connection, account_id) -> int:
    joined = dm.speech_segments.join(m.generations, dm.speech_segments.c.generation_id == m.generations.c.id).join(
        m.requests, m.generations.c.request_id == m.requests.c.id
    )
    return int(conn.scalar(select(func.count()).select_from(joined).where(m.requests.c.account_id == account_id)) or 0)


def _stream_join():
    return dm.generation_stream_events.join(
        m.generations, dm.generation_stream_events.c.generation_id == m.generations.c.id
    ).join(m.requests, m.generations.c.request_id == m.requests.c.id)


def _count_tool_calls(conn: Connection, account_id) -> int:
    return int(
        conn.scalar(
            select(func.count())
            .select_from(_stream_join())
            .where(
                m.requests.c.account_id == account_id,
                dm.generation_stream_events.c.type == "tool.completed",
                dm.generation_stream_events.c.payload.op("->>")("stage") == "agent",
            )
        )
        or 0
    )


def _tool_ranking(conn: Connection, account_id, since: datetime) -> list[dict[str, Any]]:
    tool_col = dm.generation_stream_events.c.payload.op("->>")("tool")
    rows = conn.execute(
        select(tool_col, func.count())
        .select_from(_stream_join())
        .where(
            m.requests.c.account_id == account_id,
            dm.generation_stream_events.c.type == "tool.completed",
            dm.generation_stream_events.c.payload.op("->>")("stage") == "agent",
            dm.generation_stream_events.c.created_at >= since,
        )
        .group_by(tool_col)
        .order_by(func.count().desc())
        .limit(6)
    ).all()
    return [{"name": name or "未命名工具", "calls": int(count)} for name, count in rows]


def _window_generations(conn: Connection, account_id, since: datetime):
    return list(
        conn.execute(
            select(
                m.generations.c.id,
                m.generations.c.model_ref,
                m.generations.c.state,
                m.generations.c.created_at,
                m.generations.c.finished_at,
            )
            .select_from(_generation_join())
            .where(m.requests.c.account_id == account_id, m.generations.c.created_at >= since)
            .order_by(m.generations.c.created_at)
        ).mappings().all()
    )


def _window_assistant_rows(conn: Connection, account_id, since: datetime):
    """Assistant text with the model that produced it, for token ranking and trends."""
    return list(
        conn.execute(
            select(_text_column(), m.events.c.occurred_at, m.generations.c.model_ref)
            .select_from(m.events.outerjoin(m.generations, m.events.c.generation_id == m.generations.c.id))
            .where(
                m.events.c.account_id == account_id,
                m.events.c.visibility == "visible",
                m.events.c.role == "assistant",
                m.events.c.occurred_at >= since,
            )
        ).all()
    )


def _first_token_times(conn: Connection, account_id, since: datetime) -> dict[Any, datetime]:
    """Map each generation to the timestamp of its first streamed text delta."""
    first_delta = (
        select(
            dm.generation_stream_events.c.generation_id.label("generation_id"),
            func.min(dm.generation_stream_events.c.created_at).label("first_at"),
        )
        .where(dm.generation_stream_events.c.type == "text.delta")
        .group_by(dm.generation_stream_events.c.generation_id)
        .subquery()
    )
    rows = conn.execute(
        select(m.generations.c.id, first_delta.c.first_at)
        .select_from(_generation_join().join(first_delta, first_delta.c.generation_id == m.generations.c.id))
        .where(m.requests.c.account_id == account_id, m.generations.c.created_at >= since)
    ).all()
    return {generation_id: first_at for generation_id, first_at in rows if first_at is not None}


def usage_overview(
    conn: Connection,
    account_id,
    *,
    monitor: "ProcessMonitor | None" = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate chat/voice/tool usage for one account from already-archived data."""
    moment = now or datetime.now(timezone.utc)
    today = _today_start(moment)

    messages_total = messages_today = 0
    input_tokens = output_tokens = 0
    today_input = today_output = 0
    rows = _message_rows(conn, account_id)
    for role, text_value, occurred_at in rows:
        tokens = estimate_tokens(text_value)
        messages_total += 1
        if role == "user":
            input_tokens += tokens
        else:
            output_tokens += tokens
        if occurred_at is not None and occurred_at >= today:
            messages_today += 1
            if role == "user":
                today_input += tokens
            else:
                today_output += tokens

    # The dashboard window ends at the current hour and covers 24 hourly buckets.
    hour_floor = _floor_hour(moment)
    hours = [hour_floor - timedelta(hours=23 - index) for index in range(24)]
    points = [
        {"at": hour.isoformat(), "messages": 0, "output_tokens": 0, "model_calls": 0}
        for hour in hours
    ]
    hour_index = {hour: index for index, hour in enumerate(hours)}

    def bucket(value: datetime | None) -> int | None:
        if value is None:
            return None
        return hour_index.get(_floor_hour(value))

    for role, text_value, occurred_at in rows:
        index = bucket(occurred_at)
        if index is not None:
            points[index]["messages"] += 1

    generations = _window_generations(conn, account_id, hours[0])
    first_tokens = _first_token_times(conn, account_id, hours[0])
    responses: list[float] = []
    ttfts: list[float] = []
    model_calls: dict[str, dict[str, Any]] = {}
    for generation in generations:
        index = bucket(generation["created_at"])
        if index is not None:
            points[index]["model_calls"] += 1
        label = _model_label(generation["model_ref"])
        entry = model_calls.setdefault(label, {"model": label, "calls": 0, "tokens": 0})
        entry["calls"] += 1
        if generation["state"] == "completed" and generation["finished_at"] is not None:
            first_at = first_tokens.get(generation["id"])
            if first_at is not None:
                responses.append(max(0.0, (generation["finished_at"] - generation["created_at"]).total_seconds()))
                ttfts.append(max(0.0, (first_at - generation["created_at"]).total_seconds() * 1000))

    window_tokens = 0
    for text_value, occurred_at, model_ref in _window_assistant_rows(conn, account_id, hours[0]):
        tokens = estimate_tokens(text_value)
        window_tokens += tokens
        index = bucket(occurred_at)
        if index is not None:
            points[index]["output_tokens"] += tokens
        label = _model_label(model_ref)
        entry = model_calls.setdefault(label, {"model": label, "calls": 0, "tokens": 0})
        entry["tokens"] += tokens

    models = sorted(model_calls.values(), key=lambda item: (item["tokens"], item["calls"]), reverse=True)[:6]

    completed = sum(1 for generation in generations if generation["state"] == "completed")
    total_seconds = sum(responses)
    performance = {
        "window_hours": 24,
        "calls": len(generations),
        "success_rate": round(completed / len(generations), 4) if generations else None,
        "avg_ttft_ms": round(sum(ttfts) / len(ttfts)) if ttfts else None,
        "avg_response_ms": round(total_seconds / len(responses) * 1000) if responses else None,
        "tpm": round(window_tokens / (total_seconds / 60)) if total_seconds > 0 else None,
    }

    snapshot = monitor.snapshot() if monitor is not None else {}
    return {
        "generated_at": moment.isoformat(),
        "messages": {"total": messages_total, "today": messages_today},
        "model_calls": {
            "total": _count_generations(conn, account_id),
            "today": _count_generations(conn, account_id, since=today),
        },
        "tokens": {
            "estimated": True,
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
            "today_input": today_input,
            "today_output": today_output,
            "today_total": today_input + today_output,
        },
        "voice": {"segments": _count_voice_segments(conn, account_id)},
        "tools": {"calls": _count_tool_calls(conn, account_id), "ranking": _tool_ranking(conn, account_id, hours[0])},
        "series": {"window_hours": 24, "points": points},
        "models": models,
        "performance": performance,
        "server": snapshot,
    }


def process_working_set_bytes() -> int | None:
    """Current process working set; None when the platform probe is unavailable."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class _ProcessMemoryCountersEx(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = _ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            current = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(current, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return None
        if sys.platform.startswith("linux"):
            fields = Path("/proc/self/statm").read_text(encoding="ascii").split()
            return int(fields[1]) * 4096
        import resource

        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except Exception:
        return None


class ProcessMonitor:
    """Gateway process facts for stat cards; CPU is sampled between two snapshot calls."""

    def __init__(self):
        self.started_at = datetime.now(timezone.utc)
        self._wall = time.monotonic()
        self._cpu = time.process_time()
        self._cpu_percent = 0.0

    def snapshot(self) -> dict[str, Any]:
        wall, cpu = time.monotonic(), time.process_time()
        elapsed = wall - self._wall
        if elapsed >= 0.5:
            self._cpu_percent = max(0.0, min(100.0, (cpu - self._cpu) / elapsed * 100))
            self._wall, self._cpu = wall, cpu
        memory = process_working_set_bytes()
        return {
            "uptime_seconds": max(0, int((datetime.now(timezone.utc) - self.started_at).total_seconds())),
            "cpu_percent": round(self._cpu_percent, 1),
            "memory_bytes": memory,
            "started_at": self.started_at.isoformat(),
        }
