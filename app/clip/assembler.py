"""Fensterwahl, Warten auf den Nachlauf, Zusammenbau, Thumbnail, Sidecar."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..capture.buffer import RingBuffer, covered_duration, select_segments
from ..capture.recorder import Segment, SegmentRecorder
from ..storage import atomic_write_text, ensure_dir
from ..timeutil import iso, utcnow

logger = logging.getLogger(__name__)

WINDOW_TOLERANCE_S = 10.0
WAIT_EXTRA_S = 300.0


@dataclass(slots=True)
class BuildResult:
    status: str
    path: Path | None = None
    thumbnail_path: Path | None = None
    duration_s: float = 0.0
    size_bytes: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    window_start_rel: float = 0.0
    window_end_rel: float = 0.0
    segment_count: int = 0
    truncated: bool = False
    error: str | None = None
    sidecar: dict[str, Any] = field(default_factory=dict)


def window_bounds(media_trigger: float, pre_roll_s: int, post_roll_s: int) -> tuple[float, float]:
    return media_trigger - float(pre_roll_s), media_trigger + float(post_roll_s)


def output_filename(login: str, trigger_at: datetime, score: float, recording_id: int) -> tuple[str, str]:
    """Liefert (Unterordner, Dateiname) gemäß Spec."""
    day = trigger_at.strftime("%Y-%m-%d")
    clock = trigger_at.strftime("%H%M%S")
    score100 = int(round(max(0.0, min(1.0, float(score))) * 100))
    return day, f"{clock}_{score100:03d}_{recording_id}.mp4"


def _concat_line(path: Path) -> str:
    escaped = str(path).replace("'", "'\\''")
    return f"file '{escaped}'\n"


async def _run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return -1, "", f"Timeout nach {timeout:.0f}s: {' '.join(cmd[:3])}"
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def probe_duration(path: Path) -> float:
    code, out, _err = await _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=30,
    )
    if code == 0 and out:
        try:
            return float(out.splitlines()[0])
        except ValueError:
            return 0.0
    return 0.0


async def probe_video(path: Path) -> bool:
    code, out, _err = await _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        timeout=30,
    )
    return code == 0 and "video" in out


async def make_thumbnail(video: Path, target: Path, at_ratio: float = 0.2) -> bool:
    duration = await probe_duration(video)
    points = [max(0.0, duration * at_ratio), max(0.0, min(duration - 0.5, 1.0)), 0.0]
    for point in points:
        code, _out, err = await _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{point:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(target),
            ],
            timeout=60,
        )
        if code == 0 and target.exists() and target.stat().st_size > 0:
            return True
        logger.warning("Thumbnail fehlgeschlagen bei %.1fs: %s", point, err[-300:])
    return False


class ClipAssembler:
    """Baut aus dem Ringpuffer eine ca. 5-minütige MP4."""

    def __init__(self, ffmpeg_bin: str = "ffmpeg") -> None:
        self.ffmpeg_bin = ffmpeg_bin

    async def wait_for_window(
        self,
        recorder: SegmentRecorder,
        window_end_rel: float,
        timeout_s: float | None = None,
    ) -> tuple[bool, float]:
        """Wartet, bis der Rekorder das Fensterende erreicht hat.

        Rückgabe: (vollständig_erreicht, verfügbare_medienzeit)
        """
        if recorder is None:
            return False, 0.0
        start_media = recorder.media_end
        timeout = timeout_s or (max(0.0, window_end_rel - start_media) + WAIT_EXTRA_S)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            available = recorder.media_end
            if available >= window_end_rel:
                return True, available
            if not recorder.alive and not recorder.ffmpeg_alive:
                return False, recorder.media_end
            if asyncio.get_running_loop().time() >= deadline:
                return False, recorder.media_end
            await asyncio.sleep(1.0)

    async def build(
        self,
        *,
        recording_id: int,
        login: str,
        stream_id: str,
        buffer_dir: Path,
        recorder: SegmentRecorder | None,
        ring_buffer: RingBuffer,
        storage_root: Path,
        media_trigger: float,
        pre_roll_s: int,
        post_roll_s: int,
        score: float,
        components: dict[str, float],
        weights: dict[str, float],
        trigger_info: dict[str, Any],
        components_detail: dict[str, Any] | None = None,
        wait_timeout_s: float | None = None,
    ) -> BuildResult:
        window_start_rel, window_end_rel = window_bounds(media_trigger, pre_roll_s, post_roll_s)
        if window_start_rel < 0:
            window_start_rel = 0.0

        ring_buffer.protect(window_start_rel, window_end_rel)
        try:
            complete = True
            available = 0.0
            if recorder is not None:
                complete, available = await self.wait_for_window(recorder, window_end_rel, wait_timeout_s)
            else:
                available = window_end_rel

            segments = recorder.segments() if recorder is not None else []
            if not segments:
                # Rekorder vorbei: Segmente aus dem Verzeichnis rekonstruieren.
                segments = _segments_from_index(buffer_dir)
            chosen = select_segments(segments, window_start_rel, window_end_rel)
            if not chosen:
                return BuildResult(
                    status="failed",
                    error="Keine Segmente im Trigger-Fenster vorhanden.",
                    window_start_rel=window_start_rel,
                    window_end_rel=window_end_rel,
                )

            actual_start = chosen[0].start
            actual_end = chosen[-1].end
            planned = window_end_rel - window_start_rel
            actual = actual_end - actual_start
            truncated = (not complete) or (actual_end < window_end_rel - 1.0) or (actual < planned - 2.0)

            window_start_wall = recorder.wall_time(window_start_rel) if recorder is not None else None
            window_end_wall = recorder.wall_time(window_end_rel) if recorder is not None else None

            trigger_at = utcnow()
            if recorder is not None:
                trigger_at = recorder.wall_time(media_trigger)
            day, filename = output_filename(login, trigger_at, score, recording_id)
            output_dir = ensure_dir(Path(storage_root) / login / day)
            final_path = output_dir / filename
            temp_path = output_dir / f".{filename}.part.mp4"

            concat_fd, concat_name = tempfile.mkstemp(prefix="concat-", suffix=".txt", dir=str(buffer_dir))
            os.close(concat_fd)
            concat_file = Path(concat_name)
            try:
                concat_file.write_text("".join(_concat_line(segment.path) for segment in chosen), encoding="utf-8")
                code, _out, err = await _run(
                    [
                        self.ffmpeg_bin,
                        "-hide_banner",
                        "-loglevel",
                        "warning",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(concat_file),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        "-f",
                        "mp4",
                        str(temp_path),
                    ],
                    timeout=300,
                )
                if code != 0 or not temp_path.exists() or temp_path.stat().st_size == 0:
                    logger.warning("concat/mp4 fehlgeschlagen, versuche MPEG-TS-Remux: %s", err[-400:])
                    fallback = await self._fallback_remux(chosen, concat_file, temp_path, output_dir)
                    if not fallback:
                        return BuildResult(
                            status="failed",
                            error=f"ffmpeg concat fehlgeschlagen: {err[-500:] or 'unbekannter Fehler'}",
                            window_start_rel=window_start_rel,
                            window_end_rel=window_end_rel,
                            segment_count=len(chosen),
                        )
            finally:
                concat_file.unlink(missing_ok=True)

            duration = await probe_duration(temp_path)
            if duration <= 0:
                duration = covered_duration(chosen)
            if not await probe_video(temp_path):
                temp_path.unlink(missing_ok=True)
                return BuildResult(
                    status="failed",
                    error="Erzeugte Datei enthält keinen Video-Stream.",
                    window_start_rel=window_start_rel,
                    window_end_rel=window_end_rel,
                    segment_count=len(chosen),
                )

            os.replace(temp_path, final_path)
            size_bytes = final_path.stat().st_size
            thumbnail = final_path.with_suffix(".jpg")
            thumb_ok = await make_thumbnail(final_path, thumbnail)

            status = "truncated" if truncated else "ready"
            window_start_wall = (
                recorder.wall_time(actual_start) if recorder is not None else window_start_wall
            )
            window_end_wall = recorder.wall_time(actual_end) if recorder is not None else window_end_wall

            sidecar_data: dict[str, Any] = {
                "recording_id": recording_id,
                "streamer": login,
                "stream_id": stream_id,
                "created_at": iso(utcnow()),
                "status": status,
                "score": round(float(score), 4),
                "components": {key: round(float(value), 4) for key, value in components.items()},
                "weights": {key: round(float(value), 4) for key, value in weights.items()},
                "components_detail": components_detail or {},
                "trigger": trigger_info,
                "window": {
                    "start": iso(window_start_wall),
                    "end": iso(window_end_wall),
                    "pre_roll_s": pre_roll_s,
                    "post_roll_s": post_roll_s,
                    "planned_duration_s": round(window_end_rel - window_start_rel, 2),
                    "media_start_s": round(actual_start, 2),
                    "media_end_s": round(actual_end, 2),
                },
                "segments": {
                    "count": len(chosen),
                    "first": chosen[0].name,
                    "last": chosen[-1].name,
                    "duration_s": round(covered_duration(chosen), 2),
                },
                "file": {
                    "filename": filename,
                    "duration_s": round(duration, 3),
                    "size_bytes": size_bytes,
                    "thumbnail": thumbnail.name if thumb_ok else None,
                },
            }
            atomic_write_text(final_path.with_suffix(".json"), json.dumps(sidecar_data, ensure_ascii=False, indent=2))

            return BuildResult(
                status=status,
                path=final_path,
                thumbnail_path=thumbnail if thumb_ok else None,
                duration_s=duration,
                size_bytes=size_bytes,
                window_start=window_start_wall,
                window_end=window_end_wall,
                window_start_rel=window_start_rel,
                window_end_rel=window_end_rel,
                segment_count=len(chosen),
                truncated=truncated,
                sidecar=sidecar_data,
            )
        finally:
            ring_buffer.unprotect()

    async def _fallback_remux(
        self,
        chosen: list[Segment],
        concat_file: Path,
        temp_path: Path,
        output_dir: Path,
    ) -> bool:
        """Einmaliger Remux-Versuch über MPEG-TS, falls MP4-copy scheitert."""
        ts_path = output_dir / (temp_path.stem + ".ts")
        try:
            code, _out, err = await _run(
                [
                    self.ffmpeg_bin,
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c",
                    "copy",
                    "-f",
                    "mpegts",
                    str(ts_path),
                ],
                timeout=300,
            )
            if code != 0 or not ts_path.exists():
                logger.warning("TS-Remux fehlgeschlagen: %s", err[-300:])
                return False
            code, _out, err = await _run(
                [
                    self.ffmpeg_bin,
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-y",
                    "-i",
                    str(ts_path),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(temp_path),
                ],
                timeout=300,
            )
            return code == 0 and temp_path.exists() and temp_path.stat().st_size > 0
        finally:
            ts_path.unlink(missing_ok=True)


def _segments_from_index(buffer_dir: Path) -> list[Segment]:
    """Rebuild segment list from index.csv after the recorder has stopped."""
    index = Path(buffer_dir) / "index.csv"
    if not index.exists():
        return []
    segments: list[Segment] = []
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.strip().split(",")
        if len(parts) != 3:
            continue
        name, raw_start, raw_end = parts
        try:
            start, end = float(raw_start), float(raw_end)
        except ValueError:
            continue
        path = Path(buffer_dir) / name
        if path.exists() and end > start:
            segments.append(Segment(name=name, path=path, start=start, end=end))
    return sorted(segments, key=lambda segment: segment.start)
