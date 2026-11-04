from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.capture.buffer import RingBuffer
from app.capture.recorder import Segment
from app.clip.assembler import ClipAssembler, _run


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False
        self.waited = asyncio.Event()

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.Event().wait()
        return b"", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited.set()
        return self.returncode or 0


async def test_run_cancellation_reaps_process(monkeypatch) -> None:
    process = FakeProcess()

    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr("app.clip.assembler.asyncio.create_subprocess_exec", fake_exec)
    task = asyncio.create_task(_run(["sleep", "30"], timeout=30))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed is True
    assert process.waited.is_set()


async def test_run_timeout_reaps_process(monkeypatch) -> None:
    process = FakeProcess()

    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr("app.clip.assembler.asyncio.create_subprocess_exec", fake_exec)
    code, _out, err = await _run(["sleep", "30"], timeout=0.01)
    assert code == -1
    assert "Timeout" in err
    assert process.killed is True
    assert process.waited.is_set()


class FakeRecorder:
    alive = True
    ffmpeg_alive = True
    started_at = datetime(2026, 11, 4, 12, 0, 0)

    def __init__(self, segments: list[Segment], media_end: float) -> None:
        self._segments = segments
        self.media_end = media_end

    def segments(self) -> list[Segment]:
        return self._segments

    def wall_time(self, media_s: float) -> datetime:
        return self.started_at + timedelta(seconds=media_s)

    def media_time(self, wall: datetime) -> float:
        return (wall - self.started_at).total_seconds()

    def buffer_seconds(self, max_buffer_s: float = 360.0) -> float:
        return min(self.media_end, max_buffer_s)


async def test_cancelled_build_removes_part_file(tmp_path: Path, monkeypatch) -> None:
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()
    segments: list[Segment] = []
    for index in range(150):
        path = buffer_dir / f"seg_{index:05d}.ts"
        path.write_bytes(b"x")
        segments.append(
            Segment(name=path.name, path=path, start=60.0 + index * 2, end=62.0 + index * 2)
        )

    recorder = FakeRecorder(segments, media_end=360.0)
    ring = RingBuffer(buffer_dir)

    async def cancel_run(cmd: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
        Path(cmd[-1]).write_bytes(b"partial")
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.clip.assembler._run", cancel_run)

    storage_root = tmp_path / "out"
    with pytest.raises(asyncio.CancelledError):
        await ClipAssembler().build(
            recording_id=7,
            login="alice",
            stream_id="s1",
            buffer_dir=buffer_dir,
            recorder=recorder,  # type: ignore[arg-type]
            ring_buffer=ring,
            storage_root=storage_root,
            media_trigger=300.0,
            pre_roll_s=240,
            post_roll_s=60,
            score=0.8,
            components={"chat_rate": 0.8},
            weights={"chat_rate": 1.0},
            trigger_info={"kind": "auto", "reason": "Test"},
        )

    assert list(storage_root.rglob("*.part.mp4")) == []
    assert list(buffer_dir.glob("concat-*.txt")) == []
