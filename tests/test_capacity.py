from __future__ import annotations

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import capacity as capacity_module
from app.api import health
from app.capacity import capacity_snapshot, memory_guard_reason, memory_usage
from app.config import AppConfig
from app.models import Setting, Streamer
from app.runtime_settings import (
    SettingsService,
    SettingsValidationError,
    default_values,
    validate_updates,
)
from app.supervisor import Supervisor
from app.timeutil import utcnow
from app.twitch.types import Channel

EXPECTED_KEYS = {
    "max_concurrent",
    "active",
    "queued",
    "offline",
    "enabled",
    "total",
    "processes",
    "mem_used_mb",
    "mem_limit_mb",
    "mem_available_mb",
    "guard_active",
    "guard_reason",
    "poll_batch_size",
    "gql_requests_last_cycle",
}

MB = 1024**2


class FakeTwitch:
    def __init__(self, channels: dict[str, Channel]) -> None:
        self.channels = channels
        self.gql_requests_last_cycle = 0
        self.calls = 0

    async def fetch_channels_batch(self, logins: list[str]) -> dict[str, Channel]:
        self.calls += 1
        return {login: self.channels[login] for login in logins if login in self.channels}

    def reset_request_counter(self) -> None:
        self.gql_requests_last_cycle = 0


class FakeSession:
    """Ersetzt LiveSession: belegt den Slot, bis der Test ihn freigibt."""

    instances: list["FakeSession"] = []

    def __init__(self, supervisor: Supervisor, streamer, stream_id: str, settings: dict) -> None:
        self.supervisor = supervisor
        self.streamer = streamer
        self.stream_id = stream_id
        self.settings = settings
        self.release = asyncio.Event()
        self.runtime = supervisor.states.setdefault(
            streamer.id, SimpleNamespace(streamer_id=streamer.id, login=streamer.login)
        )
        self.runtime.login = streamer.login
        FakeSession.instances.append(self)

    async def run(self) -> None:
        self.runtime.state = "live"
        self.runtime.session_started_at = utcnow()
        await self.release.wait()
        self.runtime.state = "offline"

    async def stop(self) -> None:
        self.runtime.state = "offline"
        self.release.set()


class FakeSettingsService:
    def __init__(self, values: dict) -> None:
        self.values = values

    async def get(self, force: bool = False) -> dict:
        return dict(self.values)


async def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def count_states(supervisor: Supervisor, name: str) -> int:
    return sum(1 for state in supervisor.states.values() if state.state == name)


async def build_supervisor(
    session_factory,
    tmp_path,
    logins: list[str],
    settings: dict | None = None,
) -> Supervisor:
    async with session_factory() as session:
        for login in logins:
            session.add(Streamer(login=login, display_name=login, enabled=True))
        for key, value in (settings or {}).items():
            session.add(Setting(key=key, value=value, updated_at=utcnow()))
        await session.commit()

    channels = {
        login: Channel(
            id=f"id-{login}",
            login=login,
            display_name=login,
            is_live=True,
            viewers=5,
            started_at=utcnow(),
            stream_id=f"stream-{login}",
        )
        for login in logins
    }
    config = AppConfig(storage_root=tmp_path, data_dir=tmp_path)
    supervisor = Supervisor(FakeTwitch(channels), SettingsService(ttl=0.0), config=config)
    supervisor.channel_cache = dict(channels)
    supervisor.clips_cache = {login: [] for login in logins}
    supervisor.cache_updated_at = time.monotonic()
    supervisor.cache_version = 1
    supervisor.enabled_logins = list(logins)
    supervisor.total_streamers = len(logins)
    return supervisor


# -- Speicher-Guard --------------------------------------------------------
def _patch_memory(tmp_path, monkeypatch, limit: int | str, current: int) -> None:
    max_path = tmp_path / "memory.max"
    current_path = tmp_path / "memory.current"
    max_path.write_text(str(limit))
    current_path.write_text(str(current))
    monkeypatch.setattr(capacity_module, "MEMORY_MAX_PATH", max_path)
    monkeypatch.setattr(capacity_module, "MEMORY_CURRENT_PATH", current_path)


def _patch_meminfo(
    tmp_path, monkeypatch, available_mb: int, total_mb: int = 8192
) -> None:
    path = tmp_path / "meminfo"
    path.write_text(
        f"MemTotal: {total_mb * 1024} kB\nMemAvailable: {available_mb * 1024} kB\n",
        encoding="ascii",
    )
    monkeypatch.setattr(capacity_module, "MEMINFO_PATH", path)


def test_memory_guard_trips_above_85_percent(tmp_path, monkeypatch) -> None:
    _patch_memory(tmp_path, monkeypatch, limit=1000 * MB, current=900 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=4096)

    reason = memory_guard_reason()
    assert reason is not None
    assert "Speicher" in reason


def test_memory_guard_inactive_below_threshold(tmp_path, monkeypatch) -> None:
    _patch_memory(tmp_path, monkeypatch, limit=1000 * MB, current=500 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=4096)

    assert memory_guard_reason() is None


def test_memory_usage_reads_current_even_without_limit(tmp_path, monkeypatch) -> None:
    _patch_memory(tmp_path, monkeypatch, limit="max", current=512 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=4096)

    used, limit = memory_usage()
    assert used == 512 * MB
    assert limit is None


def test_memory_guard_inactive_without_limit(tmp_path, monkeypatch) -> None:
    _patch_memory(tmp_path, monkeypatch, limit="max", current=999 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=4096)

    assert memory_guard_reason() is None


def test_memory_guard_without_limit_blocks_on_low_memavailable(tmp_path, monkeypatch) -> None:
    _patch_memory(tmp_path, monkeypatch, limit="max", current=512 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=512)

    reason = memory_guard_reason()
    assert reason is not None
    assert "Host-Speicher" in reason
    assert "512 MB" in reason

    used, limit = memory_usage()
    assert used > 0
    assert limit is None


@pytest.mark.asyncio
async def test_start_warns_when_host_fallback_and_limit_above_20(
    tmp_path, monkeypatch, caplog
) -> None:
    _patch_memory(tmp_path, monkeypatch, limit="max", current=512 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=4096)
    supervisor = Supervisor(
        FakeTwitch({}),
        FakeSettingsService({"max_concurrent_streams": 40}),
        config=AppConfig(storage_root=tmp_path, data_dir=tmp_path),
    )

    with caplog.at_level(logging.WARNING):
        await supervisor._warn_memory_guard_start()  # noqa: SLF001

    assert "Host hat nur" in caplog.text
    assert "Limit prüfen" in caplog.text


# -- Snapshot / Contract ---------------------------------------------------
def make_state(name: str) -> SimpleNamespace:
    return SimpleNamespace(streamer_id=0, login="x", state=name, queue_position=None)


class FakeSupervisor:
    def __init__(self) -> None:
        self.states = {
            1: make_state("live"),
            2: make_state("connecting"),
            3: make_state("queued"),
            4: make_state("offline"),
        }
        self.enabled_logins = ["a", "b", "c"]
        self.total_streamers = 5
        self.gql_requests_last_cycle = 2


@pytest.mark.asyncio
async def test_capacity_snapshot_has_exact_contract(tmp_path, monkeypatch) -> None:
    _patch_memory(tmp_path, monkeypatch, limit=200 * MB, current=50 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=2048)
    monkeypatch.setattr(capacity_module, "count_child_processes", lambda: 3)

    snapshot = await capacity_snapshot(FakeSupervisor(), {"max_concurrent_streams": 7})

    assert set(snapshot) == EXPECTED_KEYS
    assert snapshot == {
        "max_concurrent": 7,
        "active": 2,
        "queued": 1,
        "offline": 1,
        "enabled": 3,
        "total": 5,
        "processes": 3,
        "mem_used_mb": 50,
        "mem_limit_mb": 200,
        "mem_available_mb": 2048,
        "guard_active": False,
        "guard_reason": None,
        "poll_batch_size": 15,
        "gql_requests_last_cycle": 2,
    }


@pytest.mark.asyncio
async def test_capacity_snapshot_reports_guard_without_cgroup_limit(
    tmp_path, monkeypatch
) -> None:
    _patch_memory(tmp_path, monkeypatch, limit="max", current=300 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=512)
    monkeypatch.setattr(capacity_module, "count_child_processes", lambda: 0)

    snapshot = await capacity_snapshot(FakeSupervisor(), {"max_concurrent_streams": 40})

    assert snapshot["mem_limit_mb"] is None
    assert snapshot["mem_used_mb"] == 300
    assert snapshot["mem_available_mb"] == 512
    assert snapshot["guard_active"] is True
    assert snapshot["guard_reason"] is not None


def test_capacity_endpoint_returns_contract(tmp_path, monkeypatch) -> None:
    _patch_meminfo(tmp_path, monkeypatch, available_mb=4096)
    monkeypatch.setattr(capacity_module, "count_child_processes", lambda: 0)
    app = FastAPI()
    app.include_router(health.router)
    app.state.settings_service = FakeSettingsService({"max_concurrent_streams": 40})
    app.state.supervisor = FakeSupervisor()

    with TestClient(app) as client:
        response = client.get("/api/capacity")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == EXPECTED_KEYS
    assert payload["active"] == 2
    assert payload["queued"] == 1
    assert payload["poll_batch_size"] == 15
    assert payload["mem_available_mb"] == 4096
    assert payload["guard_active"] is False
    assert payload["guard_reason"] is None


# -- Laufzeit-Einstellung --------------------------------------------------
@pytest.mark.asyncio
async def test_max_concurrent_streams_validation() -> None:
    assert default_values()["max_concurrent_streams"] == 40
    clean = await validate_updates({"max_concurrent_streams": 12})
    assert clean == {"max_concurrent_streams": 12}
    for invalid in (0, 201, "viele"):
        with pytest.raises(SettingsValidationError):
            await validate_updates({"max_concurrent_streams": invalid})


# -- Admission Control / Queue ---------------------------------------------
@pytest.mark.asyncio
async def test_limit_one_queues_second_and_promotes_it(
    session_factory, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.LiveSession", FakeSession)
    FakeSession.instances.clear()
    _patch_meminfo(tmp_path, monkeypatch, available_mb=4096)
    supervisor = await build_supervisor(
        session_factory, tmp_path, ["alpha", "beta"], {"max_concurrent_streams": 1}
    )
    try:
        await supervisor._reconcile()  # noqa: SLF001
        assert await wait_until(lambda: count_states(supervisor, "live") == 1)
        assert await wait_until(lambda: count_states(supervisor, "queued") == 1)
        assert len(FakeSession.instances) == 1

        queued_state = next(s for s in supervisor.states.values() if s.state == "queued")
        assert queued_state.queue_position == 0
        assert queued_state.as_dict()["queue_position"] == 0

        first = FakeSession.instances[0]
        first.release.set()
        assert await wait_until(lambda: first.runtime.state == "offline")

        await supervisor._reconcile()  # noqa: SLF001
        assert await wait_until(lambda: count_states(supervisor, "live") == 1)
        assert await wait_until(lambda: len(FakeSession.instances) == 2)
        second = FakeSession.instances[1]
        assert second.streamer.id != first.streamer.id
        assert second.runtime.state == "live"
        assert count_states(supervisor, "queued") == 0
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_memory_guard_prevents_new_session(
    session_factory, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.LiveSession", FakeSession)
    FakeSession.instances.clear()
    _patch_memory(tmp_path, monkeypatch, limit=1000 * MB, current=900 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=4096)

    supervisor = await build_supervisor(
        session_factory, tmp_path, ["alpha"], {"max_concurrent_streams": 40}
    )
    try:
        await supervisor._reconcile()  # noqa: SLF001
        assert await wait_until(lambda: count_states(supervisor, "queued") == 1)
        state = next(iter(supervisor.states.values()))
        assert state.state == "queued"
        assert state.last_error is not None and "Speicher" in state.last_error
        assert FakeSession.instances == []
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_memory_guard_blocks_without_cgroup_limit(
    session_factory, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.LiveSession", FakeSession)
    FakeSession.instances.clear()
    _patch_memory(tmp_path, monkeypatch, limit="max", current=512 * MB)
    _patch_meminfo(tmp_path, monkeypatch, available_mb=256)

    supervisor = await build_supervisor(
        session_factory, tmp_path, ["alpha"], {"max_concurrent_streams": 40}
    )
    try:
        await supervisor._reconcile()  # noqa: SLF001
        assert await wait_until(lambda: count_states(supervisor, "queued") == 1)
        state = next(iter(supervisor.states.values()))
        assert state.state == "queued"
        assert state.last_error is not None and "Host-Speicher" in state.last_error
        assert FakeSession.instances == []
    finally:
        await supervisor.stop()
