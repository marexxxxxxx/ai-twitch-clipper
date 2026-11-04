"""Ringpuffer-Verwaltung, Retention der Segmente und Disk-Guard."""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..storage import free_bytes
from .recorder import Segment

logger = logging.getLogger(__name__)

MAX_BUFFER_S = 360
START_MIN_FREE_BYTES = 2 * 1024**3
STOP_MIN_FREE_BYTES = 1 * 1024**3


def buffer_path(storage_root: Path | str, login: str, stream_id: str) -> Path:
    safe_stream = "".join(ch for ch in str(stream_id) if ch.isalnum() or ch in "-_") or "unknown"
    return Path(storage_root) / "_buffer" / login.lower() / safe_stream


def select_segments(
    segments: list[Segment],
    window_start: float,
    window_end: float,
) -> list[Segment]:
    """Segmente, die das Fenster überlappen – Randsegmente werden ganz genommen.

    Durch die 2-s-Segmente ergibt sich eine Länge von ``window_end-window_start``
    ± Segmentlänge. Segmente außerhalb des Fensters werden verworfen.
    """
    if window_end <= window_start:
        return []
    return sorted(
        [segment for segment in segments if segment.end > window_start and segment.start < window_end],
        key=lambda segment: segment.start,
    )


def covered_duration(segments: list[Segment]) -> float:
    return sum(segment.duration for segment in segments)


def prune_candidates(
    segments: list[Segment],
    media_end: float,
    keep_s: float = MAX_BUFFER_S,
    protected_window: tuple[float, float] | None = None,
) -> list[Segment]:
    """Segmente, die aus dem Puffer gelöscht werden dürfen.

    ``protected_window`` schützt gerade benötigte Segmente einer laufenden
    Zusammenbau-Operation.
    """
    cutoff = media_end - keep_s
    result: list[Segment] = []
    for segment in segments:
        if segment.end > cutoff:
            continue
        if protected_window is not None:
            protected_start, protected_end = protected_window
            if segment.end > protected_start and segment.start < protected_end:
                continue
        result.append(segment)
    return result


def disk_allows_start(storage_root: Path | str, minimum: int = START_MIN_FREE_BYTES) -> bool:
    return free_bytes(storage_root) >= minimum


def disk_should_stop(storage_root: Path | str, minimum: int = STOP_MIN_FREE_BYTES) -> bool:
    return free_bytes(storage_root) < minimum


@dataclass
class BufferStats:
    segments: int
    seconds: float
    bytes: int
    path: str


class RingBuffer:
    """Verwaltet den Segmentordner einer Session.

    Die eigentliche Segmentliste kommt vom :class:`SegmentRecorder`; hier
    werden nur Aufräumen, Schutzfenster und Statistik gekapselt.
    """

    def __init__(self, directory: Path, keep_s: float = MAX_BUFFER_S) -> None:
        self.directory = Path(directory)
        self.keep_s = float(keep_s)
        self._prune_lock = asyncio.Lock()
        self._protected: tuple[float, float] | None = None

    def protect(self, window_start: float, window_end: float) -> None:
        self._protected = (float(window_start), float(window_end))

    def unprotect(self) -> None:
        self._protected = None

    def stats(self, segments: list[Segment]) -> BufferStats:
        size = 0
        for segment in segments:
            try:
                size += segment.path.stat().st_size
            except OSError:
                continue
        seconds = segments[-1].end - segments[0].start if segments else 0.0
        return BufferStats(
            segments=len(segments),
            seconds=round(seconds, 2),
            bytes=size,
            path=str(self.directory),
        )

    async def prune(
        self, segments: list[Segment], media_end: float, keep_s: float | None = None
    ) -> list[Segment]:
        """Löscht alte Segmente und gibt die entfernten Segmente zurück.

        Der Aufrufer entfernt sie anschließend aus dem Recorder-Zustand, damit
        weder Segmentliste noch CSV-Auswertung über die Session hinaus wachsen.
        """
        async with self._prune_lock:
            candidates = prune_candidates(
                segments,
                media_end=media_end,
                keep_s=self.keep_s if keep_s is None else float(keep_s),
                protected_window=self._protected,
            )
            removed: list[Segment] = []
            for segment in candidates:
                try:
                    segment.path.unlink(missing_ok=True)
                    removed.append(segment)
                except OSError as exc:
                    logger.warning("Segment %s konnte nicht gelöscht werden: %s", segment.path, exc)
            if removed:
                logger.debug("%d alte Segmente aus %s entfernt", len(removed), self.directory)
            return removed

    async def clear(self) -> None:
        if not self.directory.exists():
            return
        for entry in self.directory.glob("seg_*.ts"):
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (self.directory / "index.csv").unlink(missing_ok=True)
        except OSError:
            pass


def remove_buffer_dir(directory: Path) -> None:
    """Entfernt einen Session-Puffer (nur Dateien, die wir selbst anlegen)."""
    try:
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        logger.warning("Pufferordner %s konnte nicht entfernt werden", directory)
