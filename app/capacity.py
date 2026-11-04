"""Kapazität: Speicher-Guard (cgroup v2), Prozess-Zählung und Snapshot.

Der Supervisor nutzt :func:`memory_guard_reason`, um bei knappem Speicher
keine neuen Aufnahmen zu starten. ``/api/capacity`` und das Dashboard nutzen
:func:`capacity_snapshot`, dessen Schlüssel ein fester Contract für die UI
sind.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .twitch.gql import BATCH_CHUNK_SIZE

logger = logging.getLogger(__name__)

#: cgroup-v2-Dateien; in Tests auf tmp_path umgebogen.
MEMORY_MAX_PATH = Path("/sys/fs/cgroup/memory.max")
MEMORY_CURRENT_PATH = Path("/sys/fs/cgroup/memory.current")

#: Ab diesem Anteil der Speichergrenze werden keine Sessions mehr gestartet.
MEMORY_GUARD_RATIO = 0.85

DEFAULT_MAX_CONCURRENT = 40
MAX_CONCURRENT_MIN = 1
MAX_CONCURRENT_MAX = 200

MB = 1024**2


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return ""


def memory_usage() -> tuple[int, int | None]:
    """Liefert ``(belegt_bytes, limit_bytes)``; ``limit`` ist ohne cgroup-Limit None."""
    raw_limit = _read_text(MEMORY_MAX_PATH)
    if not raw_limit or raw_limit == "max":
        return 0, None
    try:
        limit = int(raw_limit)
    except ValueError:
        return 0, None
    if limit <= 0:
        return 0, None
    try:
        current = max(0, int(_read_text(MEMORY_CURRENT_PATH)))
    except ValueError:
        current = 0
    return current, limit


def memory_guard_reason() -> str | None:
    """Kurzer Hinweis, wenn der cgroup-Speicher über der Guard-Grenze liegt."""
    current, limit = memory_usage()
    if limit is None or current <= 0:
        return None
    ratio = current / limit
    if ratio <= MEMORY_GUARD_RATIO:
        return None
    return (
        f"Speichergrenze fast erreicht ({ratio * 100:.0f} % von "
        f"{int(limit / MB)} MB); neue Aufnahme zurückgestellt."
    )


def _process_table() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, comm) aus /proc; bei Fehlern leer."""
    table: dict[int, tuple[int, str]] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            # Format: ``pid (comm) state ppid ...``; ``comm`` darf Klammern enthalten.
            open_paren = stat.find("(")
            close_paren = stat.rfind(")")
            if open_paren < 0 or close_paren < 0:
                continue
            pid = int(stat[:open_paren].strip())
            comm = stat[open_paren + 1 : close_paren]
            fields = stat[close_paren + 1 :].split()
            ppid = int(fields[1])
        except (ValueError, IndexError):
            continue
        table[pid] = (ppid, comm)
    return table


def count_child_processes(root_pid: int | None = None) -> int:
    """Anzahl laufender ``streamlink``/``ffmpeg``-Nachfahren (Fallback 0)."""
    root = os.getpid() if root_pid is None else int(root_pid)
    table = _process_table()
    if not table:
        return 0
    children: dict[int, list[int]] = {}
    for pid, (ppid, _comm) in table.items():
        children.setdefault(ppid, []).append(pid)

    count = 0
    stack = list(children.get(root, []))
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        comm = table.get(pid, (0, ""))[1].lower()
        if "streamlink" in comm or "ffmpeg" in comm:
            count += 1
        stack.extend(children.get(pid, []))
    return count


def max_concurrent_streams(settings: dict[str, Any] | None) -> int:
    raw = (settings or {}).get("max_concurrent_streams", DEFAULT_MAX_CONCURRENT)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_CONCURRENT
    return max(MAX_CONCURRENT_MIN, min(MAX_CONCURRENT_MAX, value))


def _last_cycle_requests(supervisor: Any) -> int:
    value = getattr(supervisor, "gql_requests_last_cycle", None)
    if value is None:
        twitch = getattr(supervisor, "twitch", None)
        value = getattr(twitch, "gql_requests_last_cycle", None)
        if value is None:
            value = getattr(twitch, "requests_last_cycle", 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def capacity_snapshot(supervisor: Any, settings: dict[str, Any] | None) -> dict[str, Any]:
    """Kapazitäts-Contract für UI und Dashboard (Schlüssel sind stabil)."""
    states = list(getattr(supervisor, "states", {}).values())
    active = sum(1 for state in states if getattr(state, "state", "") in ("connecting", "live"))
    queued = sum(1 for state in states if getattr(state, "state", "") == "queued")
    offline = sum(1 for state in states if getattr(state, "state", "") == "offline")

    enabled = len(getattr(supervisor, "enabled_logins", None) or [])
    total = int(getattr(supervisor, "total_streamers", 0) or 0)
    if total < enabled:
        total = enabled

    used, limit = memory_usage()
    return {
        "max_concurrent": max_concurrent_streams(settings),
        "active": active,
        "queued": queued,
        "offline": offline,
        "enabled": enabled,
        "total": total,
        "processes": count_child_processes(),
        "mem_used_mb": int(used / MB),
        "mem_limit_mb": int(limit / MB) if limit is not None else None,
        "poll_batch_size": BATCH_CHUNK_SIZE,
        "gql_requests_last_cycle": _last_cycle_requests(supervisor),
    }
