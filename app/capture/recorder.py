"""Segmentrekorder: streamlink | ffmpeg → 2-s-MPEG-TS-Segmente."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..timeutil import utcnow

logger = logging.getLogger(__name__)

SEGMENT_RE = re.compile(r"^(?P<name>[^,]+),(?P<start>-?\d+(?:\.\d+)?),(?P<end>-?\d+(?:\.\d+)?)\s*$")


class RecorderError(Exception):
    """Aufnahme konnte nicht gestartet werden."""


@dataclass(slots=True)
class Segment:
    name: str
    path: Path
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class SegmentRecorder:
    """Schreibt einen laufenden Twitch-Stream in 2-Sekunden-Segmente.

    Die Zeitbasis ist der ffmpeg-Index (Sekunden relativ zum ersten Segment).
    ``started_at`` ist die Wanduhrzeit beim Prozessstart; die Startverzögerung
    von streamlink wird über ``wall_offset`` korrigiert, sobald das erste
    Segment bekannt ist.
    """

    def __init__(
        self,
        login: str,
        quality: str,
        buffer_dir: Path,
        segment_time: int = 2,
        streamlink_bin: str = "streamlink",
        ffmpeg_bin: str = "ffmpeg",
    ) -> None:
        self.login = login.strip().lower()
        self.quality = quality
        self.buffer_dir = Path(buffer_dir)
        self.segment_time = int(segment_time)
        self.streamlink_bin = streamlink_bin
        self.ffmpeg_bin = ffmpeg_bin

        self.index_path = self.buffer_dir / "index.csv"
        self.started_at: datetime = utcnow()
        self.started_monotonic = 0.0
        self.wall_offset = 0.0
        self._segments: dict[str, Segment] = {}
        self._first_segment_seen = False

        self._streamlink: asyncio.subprocess.Process | None = None
        self._ffmpeg: asyncio.subprocess.Process | None = None
        self._drain_tasks: list[asyncio.Task[None]] = []
        self._monitor_task: asyncio.Task[None] | None = None
        self._stderr_tail: list[str] = []
        self._stop_requested = False
        self._stopped = False
        self._last_error: str | None = None

    # -- Properties ---------------------------------------------------------
    @property
    def alive(self) -> bool:
        return (
            self._ffmpeg is not None
            and self._ffmpeg.returncode is None
            and self._streamlink is not None
            and self._streamlink.returncode is None
        )

    @property
    def ffmpeg_alive(self) -> bool:
        return self._ffmpeg is not None and self._ffmpeg.returncode is None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # -- Lebenszyklus -------------------------------------------------------
    async def start(self, timeout: float = 45.0) -> bool:
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        # Reste eines abgebrochenen Laufs entfernen.
        for old in self.buffer_dir.glob("seg_*.ts"):
            old.unlink(missing_ok=True)
        self.index_path.unlink(missing_ok=True)

        read_fd, write_fd = os.pipe()
        streamlink_args = [
            self.streamlink_bin,
            "--stdout",
            "--twitch-disable-ads",
            "--stream-timeout",
            "60",
            f"twitch.tv/{self.login}",
            self.quality,
        ]
        ffmpeg_args = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-i",
            "pipe:0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_time),
            "-segment_format",
            "mpegts",
            "-segment_list",
            str(self.index_path),
            "-segment_list_type",
            "csv",
            "-reset_timestamps",
            "1",
            str(self.buffer_dir / "seg_%05d.ts"),
        ]

        self.started_at = utcnow()
        self.started_monotonic = asyncio.get_running_loop().time()
        try:
            self._streamlink = await asyncio.create_subprocess_exec(
                *streamlink_args,
                stdout=write_fd,
                stderr=asyncio.subprocess.PIPE,
            )
            os.close(write_fd)
            write_fd = -1
            self._ffmpeg = await asyncio.create_subprocess_exec(
                *ffmpeg_args,
                stdin=read_fd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            await self.stop()
            missing = getattr(exc, "filename", None)
            if not missing:
                missing = self.streamlink_bin if self._streamlink is None else self.ffmpeg_bin
            raise RecorderError(f"Programm nicht gefunden: {missing}") from exc
        except OSError as exc:
            await self.stop()
            raise RecorderError(f"Prozessstart fehlgeschlagen: {exc}") from exc
        finally:
            try:
                os.close(read_fd)
            except OSError:
                pass
            if write_fd != -1:
                try:
                    os.close(write_fd)
                except OSError:
                    pass

        assert self._streamlink.stderr is not None
        assert self._ffmpeg.stderr is not None
        self._drain_tasks = [
            asyncio.create_task(
                self._drain_stderr(self._streamlink.stderr, "streamlink"),
                name=f"streamlink-stderr:{self.login}",
            ),
            asyncio.create_task(
                self._drain_stderr(self._ffmpeg.stderr, "ffmpeg"),
                name=f"ffmpeg-stderr:{self.login}",
            ),
        ]
        self._monitor_task = asyncio.create_task(self._monitor(), name=f"segment-monitor:{self.login}")

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            self.refresh()
            if self._segments:
                logger.info(
                    "Aufnahme läuft: %s (erstes Segment nach %.1fs)",
                    self.login,
                    asyncio.get_running_loop().time() - self.started_monotonic,
                )
                return True
            if not self.ffmpeg_alive:
                self._last_error = self._last_stderr() or "ffmpeg wurde beendet."
                await self.stop()
                raise RecorderError(f"Aufnahme für {self.login} nicht gestartet: {self._last_error}")
            await asyncio.sleep(0.5)
        self._last_error = self._last_stderr() or "Timeout beim Warten auf das erste Segment."
        await self.stop()
        raise RecorderError(f"Aufnahme für {self.login} nicht gestartet: {self._last_error}")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_requested = True
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None

        for process in (self._ffmpeg, self._streamlink):
            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        for process in (self._ffmpeg, self._streamlink):
            if process is None:
                continue
            try:
                await asyncio.wait_for(process.wait(), timeout=6)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        for task in self._drain_tasks:
            task.cancel()
        if self._drain_tasks:
            await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        self._drain_tasks = []
        self.refresh()

    # -- Index / Zeit -------------------------------------------------------
    def refresh(self) -> None:
        try:
            content = self.index_path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            return
        for line in content.splitlines():
            match = SEGMENT_RE.match(line.strip())
            if not match:
                continue
            name = match.group("name").strip()
            if name in self._segments:
                continue
            try:
                start = float(match.group("start"))
                end = float(match.group("end"))
            except ValueError:
                continue
            if end <= start:
                continue
            path = self.buffer_dir / name
            if not path.exists():
                continue
            self._segments[name] = Segment(name=name, path=path, start=start, end=end)
            if not self._first_segment_seen:
                self._first_segment_seen = True
                elapsed = (utcnow() - self.started_at).total_seconds()
                # Medienzeit 0 liegt um die Startverzögerung vor dem ersten Segment.
                self.wall_offset = max(0.0, elapsed - end)

    def segments(self) -> list[Segment]:
        self.refresh()
        return sorted(self._segments.values(), key=lambda segment: segment.start)

    @property
    def media_end(self) -> float:
        segments = self.segments()
        return segments[-1].end if segments else 0.0

    @property
    def media_start(self) -> float:
        segments = self.segments()
        return segments[0].start if segments else 0.0

    def buffer_seconds(self, max_buffer_s: float = 360.0) -> float:
        end = self.media_end
        start = max(0.0, end - max_buffer_s)
        return max(0.0, end - start)

    def wall_time(self, media_s: float) -> datetime:
        return self.started_at + timedelta(seconds=self.wall_offset + media_s)

    def media_time(self, wall: datetime) -> float:
        return (wall - self.started_at).total_seconds() - self.wall_offset

    def uptime_s(self) -> float:
        return max(0.0, (utcnow() - self.started_at).total_seconds())

    # -- Intern -------------------------------------------------------------
    async def _monitor(self) -> None:
        try:
            while True:
                self.refresh()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Segment-Monitor für %s fehlgeschlagen", self.login)

    async def _drain_stderr(self, stream: asyncio.StreamReader, name: str) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                self._stderr_tail.append(f"{name}: {text}")
                del self._stderr_tail[:-40]
                lowered = text.lower()
                if "error" in lowered or "failed" in lowered or "unable" in lowered:
                    logger.warning("%s [%s]: %s", name, self.login, text)
                else:
                    logger.debug("%s [%s]: %s", name, self.login, text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("stderr-Drain für %s beendet", name, exc_info=True)

    def _last_stderr(self) -> str:
        return " | ".join(self._stderr_tail[-3:])
