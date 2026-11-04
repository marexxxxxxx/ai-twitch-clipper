from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.clip.assembler import BuildResult
from app.detect.signals import bucket_key
from app.models import Recording, SignalSample, Streamer
from app.runtime_settings import DEFAULT_WEIGHTS
from app.supervisor import (
    LiveSession,
    StreamerRef,
    TriggerError,
    effective_weights_for,
    recent_trigger_count,
)
from app.twitch.chat import ChatMessage

NOW = datetime(2026, 11, 4, 12, 0, 0)


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


class FakeSupervisor:
    def __init__(self, settings: dict[str, Any]) -> None:
        self.settings_service = FakeSettingsService(settings)
        self.assembler = FakeAssembler()
        self.states: dict[int, Any] = {}
        self.active_buffer_dirs: set[Path] = set()


def base_settings(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "storage_root": str(tmp_path),
        "pre_roll_s": 240,
        "post_roll_s": 60,
        "threshold": 0.60,
        "cooldown_s": 180,
        "weights": dict(DEFAULT_WEIGHTS),
    }
    settings.update(overrides)
    return settings


async def make_session(
    session_factory: Any,
    tmp_path: Path,
    settings: dict[str, Any],
) -> tuple[FakeSupervisor, LiveSession]:
    async with session_factory() as db:
        db.add(Streamer(id=1, login="alice", display_name="Alice", enabled=True))
        await db.commit()
    supervisor = FakeSupervisor(settings)
    streamer = StreamerRef(id=1, login="alice", display_name="Alice", enabled=True)
    live = LiveSession(supervisor, streamer, "stream-1", settings)
    live.recorder = FakeRecorder()
    return supervisor, live


def command_message(now: datetime) -> ChatMessage:
    return ChatMessage(
        ts=now,
        user="Alice",
        text="!clip",
        emotes=[],
        emote_only=False,
        hype_count=0,
        user_id="1",
        badges="broadcaster/1",
    )


async def finish_assemblies(supervisor: FakeSupervisor, live: LiveSession) -> None:
    await asyncio.wait_for(supervisor.assembler.started.wait(), timeout=5)
    supervisor.assembler.gate.set()
    await asyncio.gather(*list(live._assemblies), return_exceptions=True)


def test_effective_weights_for_shadow_mode():
    settings = {"weights": {"chat_rate": 0.53, "emote_burst": 0.51}, "shadow_mode": True}
    weights = effective_weights_for(settings)
    assert weights["chat_rate"] == 0.53
    assert weights["unique_chatters"] == 0.0


def test_effective_weights_for_live_mode_keeps_new_signals():
    settings = {
        "weights": {"chat_rate": 0.53, "unique_chatters": 0.40},
        "shadow_mode": False,
    }
    weights = effective_weights_for(settings)
    assert weights["chat_rate"] == 0.53
    assert weights["unique_chatters"] == 0.40


async def test_recent_trigger_count_is_db_backed(session_factory):
    from datetime import datetime, timedelta
    from app.models import Recording, Streamer
    from app.supervisor import recent_trigger_count

    now = datetime(2026, 11, 4, 12, 0, 0)
    async with session_factory() as session:
        alice = Streamer(login="alice", display_name="Alice")
        bob = Streamer(login="bob", display_name="Bob")
        session.add_all([alice, bob])
        await session.flush()
        session.add_all([
            Recording(streamer_id=alice.id, streamer_login="alice", status="ready", created_at=now - timedelta(minutes=5), path="", filename=""),
            Recording(streamer_id=alice.id, streamer_login="alice", status="ready", created_at=now - timedelta(hours=2), path="", filename=""),
            Recording(streamer_id=bob.id, streamer_login="bob", status="ready", created_at=now - timedelta(minutes=5), path="", filename=""),
        ])
        await session.commit()
        count = await recent_trigger_count(session, alice.id, now)
    assert count == 1


async def test_session_uses_chat_and_detector_settings(
    session_factory, tmp_path: Path
) -> None:
    settings = base_settings(
        tmp_path,
        required_ticks=3,
        max_clips_per_hour=7,
        hype_keywords="geil,lol",
        copypasta_min_users=4,
        copypasta_window_s=12,
    )
    _supervisor, live = await make_session(session_factory, tmp_path, settings)

    assert live.detector.required_ticks == 3
    assert live.detector.max_clips_per_hour == 7
    assert live.signals.copypasta_min_users == 4
    assert live.signals.copypasta_window_s == 12

    message = ChatMessage(
        ts=NOW,
        user="Alice",
        text="das war geil",
        emotes=[],
        emote_only=False,
        hype_count=0,
    )
    live.signals.on_chat(message, NOW)
    assert live.signals.buckets[bucket_key(NOW)].hype_keywords == 1


async def test_tick_persists_aggregate_detail_in_shadow_mode(
    session_factory, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.utcnow", lambda: NOW)
    settings = base_settings(
        tmp_path,
        shadow_mode=True,
        weights={**DEFAULT_WEIGHTS, "chat_rate": 0.53, "streamer_command": 0.9},
    )
    _supervisor, live = await make_session(session_factory, tmp_path, settings)
    live.signals.on_chat(command_message(NOW))

    await live.tick()

    async with session_factory() as db:
        rows = list((await db.execute(SignalSample.__table__.select())).mappings())
    assert len(rows) == 1
    row = rows[0]
    assert row["score"] == 0.0
    assert row["components"]["streamer_command"] == 1.0
    assert row["detail"]["streamer_command"]["hit"] == 1.0
    assert "Alice" not in str(row["detail"])
    assert "!clip" not in str(row["detail"])


async def test_tick_direct_streamer_trigger_in_live_mode(
    session_factory, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.utcnow", lambda: NOW)
    settings = base_settings(
        tmp_path,
        shadow_mode=False,
        streamer_command_triggers=True,
        weights={**DEFAULT_WEIGHTS, "chat_rate": 1.0, "streamer_command": 0.0},
    )
    supervisor, live = await make_session(session_factory, tmp_path, settings)
    live.signals.on_chat(command_message(NOW))

    await live.tick()
    await finish_assemblies(supervisor, live)

    async with session_factory() as db:
        rows = list((await db.execute(Recording.__table__.select())).mappings())
    assert len(rows) == 1
    assert rows[0]["trigger"]["reason"] == "Streamer/Mod-Clipping"
    assert live.detector.last_trigger_at == NOW


async def test_tick_direct_streamer_trigger_disabled_in_shadow_mode(
    session_factory, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.utcnow", lambda: NOW)
    settings = base_settings(
        tmp_path,
        shadow_mode=True,
        streamer_command_triggers=True,
        weights={**DEFAULT_WEIGHTS, "streamer_command": 1.0},
    )
    _supervisor, live = await make_session(session_factory, tmp_path, settings)
    live.signals.on_chat(command_message(NOW))

    await live.tick()

    async with session_factory() as db:
        rows = list((await db.execute(Recording.__table__.select())).mappings())
    assert rows == []
    assert live.detector.last_trigger_at is None


async def test_tick_direct_streamer_trigger_respects_setting(
    session_factory, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.utcnow", lambda: NOW)
    settings = base_settings(
        tmp_path,
        shadow_mode=False,
        streamer_command_triggers=False,
    )
    _supervisor, live = await make_session(session_factory, tmp_path, settings)
    live.signals.on_chat(command_message(NOW))

    await live.tick()

    async with session_factory() as db:
        rows = list((await db.execute(Recording.__table__.select())).mappings())
    assert rows == []


async def test_direct_trigger_marks_triggered_only_after_success(
    session_factory, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.utcnow", lambda: NOW)
    settings = base_settings(
        tmp_path,
        shadow_mode=False,
        streamer_command_triggers=True,
    )
    _supervisor, live = await make_session(session_factory, tmp_path, settings)
    live.signals.on_chat(command_message(NOW))

    async def fail_trigger(*args: Any, **kwargs: Any) -> int:
        raise TriggerError("Testfehler")

    monkeypatch.setattr(live, "trigger", fail_trigger)

    await live.tick()

    assert live.detector.last_trigger_at is None
    assert live.detector.triggers == 0


async def test_shadow_log_is_throttled_to_30_seconds(
    session_factory, tmp_path: Path, monkeypatch, caplog
) -> None:
    clock = {"now": NOW}
    monkeypatch.setattr("app.supervisor.utcnow", lambda: clock["now"])
    settings = base_settings(tmp_path, shadow_mode=True)
    _supervisor, live = await make_session(session_factory, tmp_path, settings)
    caplog.set_level(logging.INFO, logger="app.supervisor")

    live.signals.on_chat(command_message(clock["now"]))
    await live.tick()
    clock["now"] = NOW + timedelta(seconds=5)
    await live.tick()
    clock["now"] = NOW + timedelta(seconds=31)
    live.signals.on_chat(command_message(clock["now"]))
    await live.tick()

    shadow_logs = [record for record in caplog.records if "[shadow]" in record.getMessage()]
    assert len(shadow_logs) == 2
    assert "streamer_command" in shadow_logs[0].getMessage()


async def test_manual_trigger_bypasses_hourly_cap_but_counts(
    session_factory, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("app.supervisor.utcnow", lambda: NOW)
    settings = base_settings(
        tmp_path,
        shadow_mode=False,
        streamer_command_triggers=True,
        max_clips_per_hour=1,
    )
    async with session_factory() as db:
        db.add(Streamer(id=1, login="alice", display_name="Alice", enabled=True))
        await db.flush()
        db.add(
            Recording(
                streamer_id=1,
                streamer_login="alice",
                status="ready",
                created_at=NOW - timedelta(minutes=5),
                path="",
                filename="",
            )
        )
        await db.commit()

    supervisor = FakeSupervisor(settings)
    streamer = StreamerRef(id=1, login="alice", display_name="Alice", enabled=True)
    live = LiveSession(supervisor, streamer, "stream-1", settings)
    live.recorder = FakeRecorder()
    live.signals.on_chat(command_message(NOW))

    await live.tick()

    async with session_factory() as db:
        rows = list((await db.execute(Recording.__table__.select())).mappings())
    assert len(rows) == 1

    await live.trigger("manual", manual=True, reason="Manuell ausgelöst")
    await finish_assemblies(supervisor, live)

    async with session_factory() as db:
        count = await recent_trigger_count(db, 1, NOW)
    assert count == 2
    assert live.detector.cooldown_remaining(NOW) > 0
