from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.capture.recorder import SegmentRecorder
from app.timeutil import utcnow


def test_refresh_parses_index_and_computes_times(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    (buffer_dir / "seg_00000.ts").write_bytes(b"x")
    (buffer_dir / "seg_00001.ts").write_bytes(b"x")
    (buffer_dir / "index.csv").write_text(
        "seg_00000.ts,0.000000,2.023222\n"
        "seg_00001.ts,2.023222,4.046444\n"
        "seg_00002.ts,4.046444,6.069666\n",  # Datei fehlt noch -> ignorieren
        encoding="utf-8",
    )

    recorder = SegmentRecorder(login="tester", quality="best", buffer_dir=buffer_dir)
    segments = recorder.segments()
    assert len(segments) == 2
    assert recorder.media_start == 0.0
    assert recorder.media_end == 4.046444
    assert recorder.buffer_seconds() == 4.046444
    assert segments[0].duration > 2.0
    # Wandzeit wird aus Startzeit + Offset + Medienzeit gebildet.
    assert recorder.wall_time(0) >= recorder.started_at - timedelta(seconds=1)
    assert recorder.wall_time(2) > recorder.wall_time(0)


def test_refresh_ignores_garbage(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    (buffer_dir / "index.csv").write_text("kaputt\nseg_00000.ts,0,0\n", encoding="utf-8")
    recorder = SegmentRecorder(login="tester", quality="best", buffer_dir=buffer_dir)
    assert recorder.segments() == []
    assert recorder.media_end == 0.0


def test_uptime_is_positive(tmp_path: Path) -> None:
    recorder = SegmentRecorder(login="tester", quality="best", buffer_dir=tmp_path)
    recorder.started_at = utcnow() - timedelta(seconds=30)
    assert 29 <= recorder.uptime_s() <= 31
