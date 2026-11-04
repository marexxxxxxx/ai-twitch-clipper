from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.capture.buffer import covered_duration, prune_candidates, select_segments
from app.capture.recorder import Segment
from app.clip.assembler import output_filename, window_bounds


def make_segments(count: int, segment_time: float = 2.0) -> list[Segment]:
    return [
        Segment(
            name=f"seg_{index:05d}.ts",
            path=Path(f"/tmp/seg_{index:05d}.ts"),
            start=index * segment_time,
            end=(index + 1) * segment_time,
        )
        for index in range(count)
    ]


def test_window_bounds() -> None:
    assert window_bounds(media_trigger=300, pre_roll_s=240, post_roll_s=60) == (60.0, 360.0)


def test_select_full_window_takes_whole_segments() -> None:
    segments = make_segments(180)
    chosen = select_segments(segments, 10, 310)
    assert len(chosen) == 150
    assert covered_duration(chosen) == 300


def test_select_partial_window_at_buffer_edge() -> None:
    segments = make_segments(180)
    chosen = select_segments(segments, 5, 295)
    assert chosen[0].start == 4
    assert chosen[-1].end == 296
    assert 288 <= covered_duration(chosen) <= 292


def test_select_ignores_segments_outside_window() -> None:
    segments = make_segments(10)
    chosen = select_segments(segments, 6, 10)
    assert [segment.start for segment in chosen] == [6, 8]
    assert select_segments(segments, 20, 30) == []


def test_prune_keeps_ring_buffer_window() -> None:
    segments = make_segments(180)
    victims = prune_candidates(segments, media_end=360, keep_s=60)
    assert len(victims) == 150
    assert victims[-1].end <= 300


def test_prune_respects_protected_window() -> None:
    segments = make_segments(180)
    victims = prune_candidates(segments, media_end=360, keep_s=120, protected_window=(200, 360))
    assert all(not (segment.end > 200 and segment.start < 360) for segment in victims)
    assert len(victims) == 100


def test_output_filename_contains_score_and_id() -> None:
    day, name = output_filename("streamer", datetime(2026, 1, 2, 3, 4, 5), 0.873, 7)
    assert day == "2026-01-02"
    assert name == "030405_087_7.mp4"
