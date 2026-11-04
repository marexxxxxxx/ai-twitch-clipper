"""Retention: alte Aufnahmen, Quota je Streamer, Logs/Signale, Pufferreste."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol

from sqlalchemy import delete, select

from .db import get_sessionmaker
from .models import LogEntry, Recording, SignalSample
from .timeutil import utcnow

logger = logging.getLogger(__name__)

GB = 1024**3
ACTIVE_STATUSES = ("ready", "truncated")
#: Status, die beim nächsten Start nicht weitergeführt werden können.
INTERRUPTED_STATUSES = ("pending", "assembling")
RECONCILE_ERROR = "Beim Neustart abgebrochen"


class RetentionRecord(Protocol):
    id: int
    streamer_login: str
    started_at: datetime | None
    created_at: datetime
    size_bytes: int | None
    status: str


def select_expired(records: Iterable[RetentionRecord], now: datetime, retention_days: int) -> list[RetentionRecord]:
    cutoff = now - timedelta(days=max(1, int(retention_days)))
    return [
        record
        for record in records
        if record.status in ACTIVE_STATUSES and (record.started_at or record.created_at) < cutoff
    ]


def select_over_quota(records: Iterable[RetentionRecord], max_bytes: int) -> list[RetentionRecord]:
    """Älteste fertige Aufnahmen je Streamer, bis die Quota eingehalten ist."""
    grouped: dict[str, list[RetentionRecord]] = {}
    for record in records:
        if record.status not in ACTIVE_STATUSES:
            continue
        grouped.setdefault(record.streamer_login, []).append(record)

    victims: list[RetentionRecord] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda record: record.started_at or record.created_at)
        total = sum(int(record.size_bytes or 0) for record in ordered)
        index = 0
        while total > max_bytes and index < len(ordered):
            victim = ordered[index]
            total -= int(victim.size_bytes or 0)
            victims.append(victim)
            index += 1
    return victims


def _recording_files(path: str | None) -> list[Path]:
    if not path:
        return []
    base = Path(path)
    return [base, base.with_suffix(".json"), base.with_suffix(".jpg")]


def delete_recording_files(record: Recording) -> None:
    for file in _recording_files(record.path):
        try:
            file.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Datei %s konnte nicht gelöscht werden: %s", file, exc)


def _recording_part_files(storage_root: Path, record: Recording) -> set[Path]:
    """Mögliche ``*.part.mp4``-Reste einer abgebrochenen Aufnahme."""
    parts: set[Path] = set()
    if record.path:
        final = Path(record.path)
        parts.add(final.parent / f".{final.name}.part.mp4")
        parts.add(final.with_suffix(".mp4.part.mp4"))
    login = (record.streamer_login or "").strip()
    if login:
        created = record.created_at or utcnow()
        target = Path(storage_root) / login / created.strftime("%Y-%m-%d")
        if target.is_dir():
            # Assembler legt die Datei als ``.HHMMSS_SSS_ID.mp4.part.mp4`` ab.
            parts.update(target.glob(f".*_{record.id}.mp4.part.mp4"))
            parts.update(target.glob(f"*_{record.id}.mp4.part.mp4"))
    return parts


async def reconcile_interrupted_recordings(
    settings: dict[str, Any], session_factory: Any | None = None
) -> int:
    """Markiert beim Start hängengebliebene Aufnahmen als ``failed``.

    ``pending``/``assembling`` können nach einem Neustart nicht fortgesetzt
    werden; zugehörige ``*.part.mp4``-Reste werden entfernt.
    """
    factory = session_factory or get_sessionmaker()
    storage_root = Path(str(settings.get("storage_root") or "/data"))
    async with factory() as session:
        rows = list(
            (
                await session.execute(
                    select(Recording).where(Recording.status.in_(INTERRUPTED_STATUSES))
                )
            ).scalars()
        )
        for record in rows:
            for part in _recording_part_files(storage_root, record):
                try:
                    part.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Teil-Datei %s konnte nicht gelöscht werden: %s", part, exc)
            record.status = "failed"
            record.error = RECONCILE_ERROR
        await session.commit()
    if rows:
        logger.info("Startup-Reconcile: %d abgebrochene Aufnahme(n) als failed markiert", len(rows))
    return len(rows)


async def run_retention_once(settings: dict[str, Any], active_buffer_dirs: set[Path] | None = None) -> dict[str, int]:
    retention_days = int(settings.get("retention_days") or 14)
    max_bytes = int(float(settings.get("max_gb_per_streamer") or 50) * GB)
    storage_root = Path(str(settings.get("storage_root") or "/data"))
    now = utcnow()
    stats = {"expired": 0, "quota": 0, "signals": 0, "logs": 0, "buffers": 0}

    async with get_sessionmaker()() as session:
        rows = list((await session.execute(select(Recording))).scalars())
        expired = select_expired(rows, now, retention_days)
        expired_ids = {record.id for record in expired}
        remaining = [record for record in rows if record.id not in expired_ids]
        quota = select_over_quota(remaining, max_bytes)

        for record in {record.id: record for record in [*expired, *quota]}.values():
            delete_recording_files(record)
            await session.delete(record)
            if record.id in expired_ids:
                stats["expired"] += 1
            else:
                stats["quota"] += 1

        signal_cutoff = now - timedelta(days=2)
        result = await session.execute(delete(SignalSample).where(SignalSample.ts < signal_cutoff))
        stats["signals"] = result.rowcount or 0

        log_cutoff = now - timedelta(days=14)
        result = await session.execute(delete(LogEntry).where(LogEntry.ts < log_cutoff))
        stats["logs"] = result.rowcount or 0

        await session.commit()

    # Verwaiste Pufferordner (älter als 6 h, nicht von einer aktiven Session genutzt).
    buffer_root = storage_root / "_buffer"
    if buffer_root.exists():
        now_ts = now.timestamp()
        for path in buffer_root.glob("*/*"):
            if not path.is_dir():
                continue
            if active_buffer_dirs and path.resolve() in {p.resolve() for p in active_buffer_dirs}:
                continue
            try:
                age = now_ts - path.stat().st_mtime
            except OSError:
                continue
            if age > 6 * 3600:
                shutil.rmtree(path, ignore_errors=True)
                stats["buffers"] += 1
        # Leere Login-Ordner entfernen.
        for login_dir in buffer_root.glob("*"):
            if login_dir.is_dir() and not any(login_dir.iterdir()):
                login_dir.rmdir()

    if any(stats.values()):
        logger.info("Retention: %s", stats)
    return stats


class RetentionLoop:
    def __init__(self, settings_service: Any, interval_s: int = 3600) -> None:
        self.settings_service = settings_service
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.active_buffer_dirs: set[Path] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="retention")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(20)
        while not self._stop.is_set():
            try:
                settings = await self.settings_service.get(force=True)
                await run_retention_once(settings, set(self.active_buffer_dirs))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Retention-Lauf fehlgeschlagen")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass
