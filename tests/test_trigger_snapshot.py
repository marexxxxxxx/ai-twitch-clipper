from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.clip.assembler import BuildResult
from app.detect.detector import TriggerDecision
from app.models import Recording, Streamer
from app.runtime_settings import DEFAULT_WEIGHTS
from app.supervisor import LiveSession, StreamerRef


class FakeSettingsService:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    async def get(self, force: bool = False) -> dict[str, Any]:
        return self.settings


class FakeRecorder:
    started_at = datetime(2026, 11, 4, 11, 0, 0)
    media_end = 300.0

    def buffer_seconds(self, max_buffer_s: float = 360.0) -> float:
        return 300.0

    def media_time(self, wall: datetime) -> float:
        return 300.0

    def wall_time(self, media_s: float) -> datetime:
        return self.started_at + timedelta(seconds=media_s)

    def segments(self) -> list:
        return []


class FakeAssembler:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None
        self.started = asyncio.Event()
        self.gate = asyncio.Event()

    async def build(self, **kwargs: Any) -> BuildResult:
        self.kwargs = kwargs
        self.started.set()
        await self.gate.wait()
        return BuildResult(status="failed", error="Testfehler")


class StalledRecorder(FakeRecorder):
    """Wanduhr läuft weiter, CSV-Medienzeit bleibt bei 260 s stehen."""

    media_end = 260.0

    def media_time(self, wall: datetime) -> float:
        return 300.0

    def buffer_seconds(self, max_buffer_s: float = 360.0) -> float:
        return 260.0


class FakeSupervisor:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings_service = FakeSettingsService(settings)
        self.assembler = FakeAssembler()
        self.states: dict[int, Any] = {}
        self.active_buffer_dirs: set[Path] = set()


async def test_components_detail_is_frozen_at_trigger(tmp_path: Path, session_factory) -> None:
    settings = {
        "storage_root": str(tmp_path),
        "pre_roll_s": 240,
        "post_roll_s": 60,
        "threshold": 0.6,
        "cooldown_s": 180,
        "weights": dict(DEFAULT_WEIGHTS),
    }
    supervisor = FakeSupervisor(settings)
    streamer = StreamerRef(id=1, login="alice", display_name="Alice", enabled=True)
    async with session_factory() as db:
        db.add(Streamer(id=1, login="alice", display_name="Alice", enabled=True))
        await db.commit()

    session = LiveSession(supervisor, streamer, "stream-1", settings)
    session.recorder = FakeRecorder()
    session.runtime.last_detail = {"marker": "trigger", "chat_rate": {"value": 12.0}}

    decision = TriggerDecision(
        trigger=True,
        score=0.75,
        components={"chat_rate": 0.9, "emote_burst": 0.4},
        reason="Test",
    )
    recording_id = await session.trigger("auto", decision, manual=False)

    await asyncio.wait_for(supervisor.assembler.started.wait(), timeout=5)
    # Nach dem Trigger läuft der Tick weiter; der Zusammenbau darf den neuen
    # Detailstand nicht mehr sehen.
    session.runtime.last_detail = {"marker": "spaeter"}
    supervisor.assembler.gate.set()
    await asyncio.gather(*list(session._assemblies), return_exceptions=True)

    captured = supervisor.assembler.kwargs
    assert captured is not None
    assert captured["components_detail"] == {"marker": "trigger", "chat_rate": {"value": 12.0}}
    assert captured["components"] == {"chat_rate": 0.9, "emote_burst": 0.4}

    async with session_factory() as db:
        row = await db.get(Recording, recording_id)
        assert row is not None
        assert row.trigger["components_detail"]["marker"] == "trigger"


async def test_trigger_time_is_clamped_to_media_end_and_manual_sets_cooldown(
    tmp_path: Path, session_factory
) -> None:
    settings = {
        "storage_root": str(tmp_path),
        "pre_roll_s": 240,
        "post_roll_s": 60,
        "threshold": 0.6,
        "cooldown_s": 180,
        "weights": dict(DEFAULT_WEIGHTS),
    }
    supervisor = FakeSupervisor(settings)
    streamer = StreamerRef(id=1, login="alice", display_name="Alice", enabled=True)
    async with session_factory() as db:
        db.add(Streamer(id=1, login="alice", display_name="Alice", enabled=True))
        await db.commit()

    session = LiveSession(supervisor, streamer, "stream-1", settings)
    session.recorder = StalledRecorder()

    recording_id = await session.trigger("manual", manual=True, reason="Test")
    assert session.detector.cooldown_remaining() > 0

    async with session_factory() as db:
        row = await db.get(Recording, recording_id)
        assert row is not None
        # Wanduhr würde 300 s liefern; der Puffer endet aber bei 260 s.
        assert row.trigger["media_trigger_s"] == 260.0

    await asyncio.wait_for(supervisor.assembler.started.wait(), timeout=5)
    supervisor.assembler.gate.set()
    await asyncio.gather(*list(session._assemblies), return_exceptions=True)
