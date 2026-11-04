from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from app.capture.recorder import INDEX_MAX_ENTRIES, SegmentRecorder, redact_urls
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


def _line(index: int) -> str:
    return f"seg_{index:05d}.ts,{index * 2}.000000,{index * 2 + 2}.000000\n"


def _make_files(directory: Path, indexes: list[int]) -> None:
    for index in indexes:
        (directory / f"seg_{index:05d}.ts").write_bytes(b"x")


def test_refresh_reads_only_new_lines(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    _make_files(buffer_dir, [0, 1, 2])
    index = buffer_dir / "index.csv"
    index.write_text("".join(_line(i) for i in range(3)), encoding="utf-8")

    recorder = SegmentRecorder(login="tester", quality="best", buffer_dir=buffer_dir)
    assert len(recorder.segments()) == 3
    assert recorder._index_offset == index.stat().st_size

    _make_files(buffer_dir, [3])
    with index.open("a", encoding="utf-8") as handle:
        handle.write(_line(3))
    assert len(recorder.segments()) == 4
    assert recorder._index_offset == index.stat().st_size


def test_refresh_detects_rotation_and_rebuilds_state(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    _make_files(buffer_dir, list(range(11)))
    index = buffer_dir / "index.csv"
    index.write_text("".join(_line(i) for i in range(10)), encoding="utf-8")

    recorder = SegmentRecorder(login="tester", quality="best", buffer_dir=buffer_dir)
    assert len(recorder.segments()) == 10

    # ffmpeg -segment_list_size schreibt die Liste um: letzte 5 + neue Zeile.
    index.write_text("".join(_line(i) for i in range(6, 11)), encoding="utf-8")
    names = [segment.name for segment in recorder.segments()]
    assert names == [f"seg_{i:05d}.ts" for i in range(6, 11)]
    assert recorder._index_offset == index.stat().st_size


def test_forget_removes_pruned_segments(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    _make_files(buffer_dir, [0, 1, 2])
    (buffer_dir / "index.csv").write_text("".join(_line(i) for i in range(3)), encoding="utf-8")
    recorder = SegmentRecorder(login="tester", quality="best", buffer_dir=buffer_dir)
    recorder.refresh()
    recorder.forget(["seg_00000.ts", "seg_00001.ts"])
    assert [segment.name for segment in recorder.segments()] == ["seg_00002.ts"]
    assert recorder.media_start == 4


def test_24h_index_is_incremental_and_state_bounded(tmp_path: Path) -> None:
    """43.200 Zeilen (24 h / 2 s): steady-state Refresh und segments() < 10 ms."""
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    total = 43_200
    _make_files(buffer_dir, list(range(total - INDEX_MAX_ENTRIES, total)))
    index = buffer_dir / "index.csv"
    index.write_text("".join(_line(i) for i in range(total)), encoding="utf-8")

    recorder = SegmentRecorder(login="tester", quality="best", buffer_dir=buffer_dir)
    recorder.refresh()  # einmaliger Initial-Scan; im Betrieb beginnt die Datei leer
    assert len(recorder.segments()) == INDEX_MAX_ENTRIES
    assert len(recorder._segments) <= INDEX_MAX_ENTRIES

    started = time.perf_counter()
    for _ in range(20):
        recorder.refresh()
    refresh_avg = (time.perf_counter() - started) / 20

    started = time.perf_counter()
    for _ in range(5):
        recorder.segments()
    segments_avg = (time.perf_counter() - started) / 5

    assert refresh_avg < 0.010, f"refresh zu langsam: {refresh_avg * 1000:.2f} ms"
    assert segments_avg < 0.010, f"segments zu langsam: {segments_avg * 1000:.2f} ms"


def test_redact_urls_hides_signatures() -> None:
    text = (
        "error opening https://video-weaver.example.hls.ttvnw.net/v1/playlist/abc.m3u8"
        "?sig=secret&token=supersecret failed"
    )
    redacted = redact_urls(text)
    assert "secret" not in redacted
    assert "abc.m3u8" not in redacted
    assert "https://video-weaver.example.hls.ttvnw.net/…" in redacted
    assert redacted.startswith("error opening")
    assert redacted.endswith(" failed")
