from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.models import Recording
from app.retention import (
    find_orphan_parts,
    run_retention_once,
    select_expired,
    select_over_quota,
    select_stale_unfinished,
)

NOW = datetime(2026, 11, 4, 12, 0, 0)
GB = 1024**3


@dataclass
class FakeRecording:
    id: int
    streamer_login: str
    started_at: datetime | None
    created_at: datetime
    size_bytes: int
    status: str = "ready"


def make(index: int, days_old: float, size_gb: float = 5, login: str = "a", status: str = "ready") -> FakeRecording:
    started = NOW - timedelta(days=days_old)
    return FakeRecording(
        id=index,
        streamer_login=login,
        started_at=started,
        created_at=started,
        size_bytes=int(size_gb * GB),
        status=status,
    )


def test_select_expired_respects_days_and_status() -> None:
    records = [
        make(1, days_old=20),
        make(2, days_old=3),
        make(3, days_old=20, status="failed"),
        make(4, days_old=20, status="assembling"),
    ]
    victims = select_expired(records, NOW, retention_days=14)
    assert [record.id for record in victims] == [1, 3]


def test_stale_unfinished_rows_are_detected() -> None:
    records = [
        make(1, days_old=2, status="pending"),
        make(2, days_old=2, status="assembling"),
        make(3, days_old=0.1, status="assembling"),
        make(4, days_old=2, status="failed"),
    ]
    victims = select_stale_unfinished(records, NOW, max_age_hours=24)
    assert [record.id for record in victims] == [1, 2]


def test_find_orphan_parts_only_old_files_outside_buffer(tmp_path: Path) -> None:
    target = tmp_path / "alice" / "2026-11-04"
    target.mkdir(parents=True)
    old_part = target / ".120000_050_1.mp4.part.mp4"
    fresh_part = target / ".120000_050_2.mp4.part.mp4"
    old_part.write_bytes(b"x")
    fresh_part.write_bytes(b"x")
    old_time = time.time() - 7 * 3600
    os.utime(old_part, (old_time, old_time))

    buffer_part = tmp_path / "_buffer" / "alice" / "s1" / ".foo.mp4.part.mp4"
    buffer_part.parent.mkdir(parents=True)
    buffer_part.write_bytes(b"x")
    os.utime(buffer_part, (old_time, old_time))

    orphans = find_orphan_parts(tmp_path, older_than_s=6 * 3600)
    assert orphans == [old_part]


async def test_retention_removes_failed_row_and_part_file(tmp_path: Path, session_factory) -> None:
    storage = tmp_path / "data"
    target = storage / "alice" / "2026-11-04"
    target.mkdir(parents=True)
    final = target / "120000_050_1.mp4"
    final.write_bytes(b"x")
    part = target / ".120000_050_1.mp4.part.mp4"
    part.write_bytes(b"x")
    old_time = time.time() - 7 * 3600
    os.utime(part, (old_time, old_time))

    created = NOW - timedelta(days=20)
    async with session_factory() as session:
        row = Recording(
            streamer_login="alice",
            status="failed",
            created_at=created,
            started_at=created,
            path=str(final),
            filename=final.name,
            size_bytes=1,
        )
        session.add(row)
        await session.commit()
        recording_id = row.id

    stats = await run_retention_once({"storage_root": str(storage), "retention_days": 14})
    assert stats["expired"] == 1
    assert stats["parts"] == 1
    assert not final.exists()
    assert not part.exists()
    async with session_factory() as session:
        assert await session.get(Recording, recording_id) is None


def test_select_over_quota_deletes_oldest_first() -> None:
    records = [
        make(1, days_old=3, size_gb=10),
        make(2, days_old=2, size_gb=10),
        make(3, days_old=1, size_gb=10),
    ]
    victims = select_over_quota(records, max_bytes=25 * GB)
    assert [record.id for record in victims] == [1]


def test_select_over_quota_is_per_streamer() -> None:
    records = [
        make(1, days_old=3, size_gb=10, login="a"),
        make(2, days_old=2, size_gb=10, login="a"),
        make(3, days_old=3, size_gb=10, login="b"),
        make(4, days_old=2, size_gb=10, login="b"),
    ]
    victims = select_over_quota(records, max_bytes=15 * GB)
    assert sorted(record.id for record in victims) == [1, 3]


def test_select_over_quota_ignores_unfinished() -> None:
    records = [
        make(1, days_old=3, size_gb=100, status="assembling"),
        make(2, days_old=2, size_gb=10, status="ready"),
    ]
    assert select_over_quota(records, max_bytes=20 * GB) == []
