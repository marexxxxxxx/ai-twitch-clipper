from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.retention import select_expired, select_over_quota

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
    ]
    victims = select_expired(records, NOW, retention_days=14)
    assert [record.id for record in victims] == [1]


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
