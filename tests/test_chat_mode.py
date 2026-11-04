from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import AppConfig
from app.db import build_engine
from app.detect.detector import Detector
from app.detect.signals import (
    BUCKET_S,
    MIN_HISTORY_S,
    MODE_SETTLE_S,
    SessionSignals,
    bucket_key,
)
from app.models import Base, Streamer
from app.runtime_settings import default_values
from app.twitch.chat import (
    ChatMessage,
    ChatMode,
    ChatReader,
    _parse_tags,
    parse_privmsg,
    parse_roomstate,
)
from app.web import routes as web_routes

NOW = datetime(2026, 11, 4, 12, 0, 0)

OPEN = ChatMode()
SUBS_ONLY = ChatMode(subs_only=True)
EMOTE_ONLY = ChatMode(emote_only=True)

#: Nach einem Moduswechsel: 120 s Settle + 300 s Basisaufbau.
REBUILD_S = MODE_SETTLE_S + MIN_HISTORY_S


def at(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def epoch(value: datetime) -> float:
    return value.replace(tzinfo=timezone.utc).timestamp()


def message(ts: datetime, emote_only: bool = False) -> ChatMessage:
    return ChatMessage(ts=ts, user="tester", text="KEKW" if emote_only else "hi", emotes=[], emote_only=emote_only, hype_count=0)


def fill(
    signals: SessionSignals,
    start: datetime,
    end: datetime,
    per_bucket: int,
    *,
    emote_only: bool = False,
) -> None:
    current = bucket_key(start)
    end_key = int(epoch(end))
    while current <= end_key:
        for index in range(per_bucket):
            signals.on_chat(message(at(current + 0.5 + index), emote_only=emote_only))
        current += BUCKET_S


def fill_every(
    signals: SessionSignals,
    start: datetime,
    end: datetime,
    step_s: int,
    *,
    emote_only: bool = False,
) -> None:
    current = start
    while current <= end:
        signals.on_chat(message(current, emote_only=emote_only))
        current += timedelta(seconds=step_s)


def line(tags: str) -> str:
    return f"@{tags} :tmi.twitch.tv ROOMSTATE #chan"


def build_review_transition(signals: SessionSignals, switch_at: datetime) -> None:
    """Review-Fall: offener Chat → dünne Sub-only-Phase → offen mit Ramp-up."""
    started = switch_at - timedelta(minutes=30)
    signals.note_mode(OPEN, started)
    fill(signals, started, switch_at - timedelta(minutes=10), 4)

    # Sub-only ist dünn: 1 Nachricht alle 15 s.
    signals.note_mode(SUBS_ONLY, switch_at - timedelta(minutes=10))
    fill_every(signals, switch_at - timedelta(minutes=10), switch_at, 15)

    # Zurück auf offen: 60 s Ramp-up (1/15 s), danach normal 4 pro 5-s-Bucket.
    signals.note_mode(OPEN, switch_at)
    fill_every(signals, switch_at + timedelta(seconds=5), switch_at + timedelta(seconds=50), 15)
    fill(signals, switch_at + timedelta(seconds=60), switch_at + timedelta(seconds=480), 4)


# -- ROOMSTATE-Parsing ------------------------------------------------------
def test_parse_roomstate_subs_only() -> None:
    mode = parse_roomstate(line("emote-only=0;followers-only=-1;r9k=0;slow=0;subs-only=1"))
    assert mode == ChatMode(subs_only=True, emote_only=False, followers_only=-1, slow=0)
    assert mode is not None and mode.restricted and mode.label == "Sub-only"


def test_parse_roomstate_emote_only() -> None:
    mode = parse_roomstate(line("emote-only=1;followers-only=-1;slow=0;subs-only=0"))
    assert mode is not None and mode.emote_only and mode.restricted
    assert mode.label == "Emote-only"


def test_parse_roomstate_followers_and_slow() -> None:
    followers = parse_roomstate(line("followers-only=10;slow=0;subs-only=0;emote-only=0"))
    assert followers is not None and followers.followers_only == 10
    assert followers.label == "Followers-only 10 min"

    hours = parse_roomstate(line("followers-only=1440;slow=0;subs-only=0;emote-only=0"))
    assert hours is not None and hours.followers_only == 1440
    assert hours.label == "Followers-only 24 h"

    slow = parse_roomstate(line("followers-only=-1;slow=30;subs-only=0;emote-only=0"))
    assert slow is not None and slow.slow == 30
    assert slow.label == "Slow 30 s"


def test_parse_roomstate_r9k() -> None:
    mode = parse_roomstate(line("emote-only=0;followers-only=-1;r9k=1;slow=0;subs-only=0"))
    assert mode is not None and mode.r9k and mode.restricted
    assert mode.label == "r9k"


def test_parse_roomstate_open_chat_and_defaults() -> None:
    mode = parse_roomstate(line("emote-only=0;followers-only=-1;slow=0;subs-only=0"))
    assert mode == OPEN
    assert mode is not None and not mode.restricted and mode.label == ""

    # Fehlende Tags fallen auf Defaults (offener Chat) zurück.
    assert parse_roomstate(line("room-id=123")) == OPEN


def test_parse_roomstate_combined_label() -> None:
    mode = parse_roomstate(line("emote-only=1;followers-only=10;slow=30;subs-only=1;r9k=1"))
    assert mode is not None
    assert mode.label == "Sub-only · Emote-only · Followers-only 10 min · Slow 30 s · r9k"


def test_parse_roomstate_partial_uses_base() -> None:
    base = ChatMode(subs_only=False, emote_only=True, followers_only=10, slow=30, r9k=True)
    mode = parse_roomstate(line("subs-only=1"), base=base)
    assert mode is not None
    assert mode.subs_only is True
    # Die übrigen Felder bleiben aus dem bisherigen Modus erhalten.
    assert mode.emote_only is True
    assert mode.followers_only == 10
    assert mode.slow == 30
    assert mode.r9k is True

    # Explizit gesendete Änderung überschreibt nur das eine Feld.
    updated = parse_roomstate(line("slow=0"), base=mode)
    assert updated is not None
    assert updated.slow == 0
    assert updated.subs_only is True and updated.emote_only is True and updated.followers_only == 10


def test_parse_roomstate_ignores_non_roomstate() -> None:
    assert parse_roomstate("PING :tmi.twitch.tv") is None
    assert parse_roomstate("@display-name=A :a!a@a.tmi.twitch.tv PRIVMSG #chan :hi") is None
    assert parse_privmsg(line("subs-only=1")) is None


def test_parse_tags_unescapes_in_one_pass() -> None:
    tags = _parse_tags(r"a=\\s;b=foo\:bar;c=\\:;d=line\r\nend")

    # ``\\s`` ist ein literaler Backslash + s, kein Leerzeichen.
    assert tags["a"] == r"\s"
    assert tags["b"] == "foo;bar"
    assert tags["c"] == r"\:"
    assert tags["d"] == "line\r\nend"


# -- ChatReader-Callback ----------------------------------------------------
def test_chat_reader_mode_callback_only_on_change() -> None:
    calls: list[ChatMode] = []
    reader = ChatReader("chan", lambda _message: None, on_mode=calls.append)
    first = line("emote-only=0;followers-only=-1;slow=0;subs-only=1")
    same = line("emote-only=0;followers-only=-1;slow=0;subs-only=1")
    changed = line("emote-only=0;followers-only=-1;slow=0;subs-only=0")

    reader._apply_mode(parse_roomstate(first, reader.mode))  # type: ignore[arg-type]
    reader._apply_mode(parse_roomstate(same, reader.mode))  # type: ignore[arg-type]
    assert len(calls) == 1
    assert calls[0].label == "Sub-only"
    assert reader.mode == SUBS_ONLY

    # Partial-ROOMSTATE (nur slow) erhält subs-only aus dem bisherigen Modus.
    reader._apply_mode(parse_roomstate(line("slow=30"), reader.mode))  # type: ignore[arg-type]
    assert len(calls) == 2
    assert reader.mode == ChatMode(subs_only=True, slow=30)

    reader._apply_mode(parse_roomstate(changed, reader.mode))  # type: ignore[arg-type]
    assert len(calls) == 3
    assert calls[-1] == OPEN


# -- Signale ----------------------------------------------------------------
def test_mode_settle_suppresses_chat_and_old_basis_is_discarded() -> None:
    started = NOW - timedelta(minutes=30)
    signals = SessionSignals(started_at=started)
    signals.note_mode(OPEN, started)

    # Offene Phase: gleichmäßiger Chat, damit die Basis steht.
    t_sub_on = NOW - timedelta(minutes=10)
    fill(signals, started, t_sub_on, 4)

    # Sub-only ist leise – die alte offene Basis würde hier einen falschen Median ergeben.
    signals.note_mode(SUBS_ONLY, t_sub_on)
    assert signals.chat_mode == SUBS_ONLY
    assert signals.mode_changed_at == t_sub_on
    fill_every(signals, t_sub_on, NOW, 15)

    # Zurückschalten auf offen: erst Ramp-up, dann normaler Chat.
    signals.note_mode(OPEN, NOW)
    assert signals.buckets == {}, "Buckets der Sub-only-Phase müssen verworfen sein"
    assert signals._phase_start() == NOW + timedelta(seconds=MODE_SETTLE_S)
    fill_every(signals, NOW + timedelta(seconds=5), NOW + timedelta(seconds=50), 15)
    fill(signals, NOW + timedelta(seconds=60), NOW + timedelta(seconds=480), 4)

    # Settle + Basisaufbau: an allen Ticks vor der Reife bleibt der Score 0.
    for offset in (10, MODE_SETTLE_S - 1, MODE_SETTLE_S + 5, 180, 300, REBUILD_S - 1):
        detail = signals.chat_rate_signal(NOW + timedelta(seconds=offset))
        assert detail.score == 0.0, offset

    # Ab der Reife steht die neue offene Basis (Median 4), kein False Positive.
    mature = signals.chat_rate_signal(NOW + timedelta(seconds=REBUILD_S))
    assert mature.samples >= 12
    assert mature.median == 4.0
    assert mature.score == 0.0

    # Und ab hier reagiert das Signal wieder normal auf einen echten Burst.
    for index in range(30):
        signals.on_chat(message(NOW + timedelta(seconds=REBUILD_S + 5, milliseconds=index)))
    burst = signals.chat_rate_signal(NOW + timedelta(seconds=REBUILD_S + 10))
    assert burst.score > 0.6


def test_suppressed_remaining_covers_settle_and_basis() -> None:
    signals = SessionSignals(started_at=NOW - timedelta(hours=1))
    signals.note_mode(OPEN, NOW - timedelta(hours=1))
    signals.note_mode(SUBS_ONLY, NOW)

    assert signals.suppressed_remaining(NOW) == MODE_SETTLE_S + MIN_HISTORY_S
    assert signals.suppressed_remaining(NOW + timedelta(seconds=MODE_SETTLE_S)) == MIN_HISTORY_S
    assert signals.suppressed_remaining(NOW + timedelta(seconds=REBUILD_S)) == 0.0
    assert signals.suppressed_remaining(NOW + timedelta(seconds=REBUILD_S + 60)) == 0.0

    # Ohne Moduswechsel gibt es nichts anzuzeigen.
    fresh = SessionSignals(started_at=NOW - timedelta(hours=1))
    fresh.note_mode(OPEN, NOW)
    assert fresh.suppressed_remaining(NOW) == 0.0


def test_emote_only_ramp_up_does_not_burst() -> None:
    started = NOW - timedelta(minutes=30)
    signals = SessionSignals(started_at=started)
    signals.note_mode(OPEN, started)
    t_switch = NOW

    # Offener Chat: Text, kaum Emotes → Emote-Median 0.
    fill(signals, started, t_switch - timedelta(minutes=10), 2)

    # Emote-only an: 60 s Ramp-up (1 Emote / 15 s), danach 2 Emotes / Bucket.
    signals.note_mode(EMOTE_ONLY, t_switch)
    fill_every(signals, t_switch + timedelta(seconds=5), t_switch + timedelta(seconds=50), 15, emote_only=True)
    fill(signals, t_switch + timedelta(seconds=60), t_switch + timedelta(seconds=480), 2, emote_only=True)

    for offset in (60, MODE_SETTLE_S + 5, 180, 300, REBUILD_S - 1):
        assert signals.emote_signal(t_switch + timedelta(seconds=offset)).score == 0.0, offset

    # Nach dem Basisaufbau ist Emote-only der Normalzustand – kein Burst.
    mature = signals.emote_signal(t_switch + timedelta(seconds=REBUILD_S))
    assert mature.samples >= 12
    assert mature.score == 0.0

    # Ein echter Emote-Burst wird danach wieder erkannt.
    for index in range(20):
        signals.on_chat(message(t_switch + timedelta(seconds=REBUILD_S + 5, milliseconds=index), emote_only=True))
    assert signals.emote_signal(t_switch + timedelta(seconds=REBUILD_S + 10)).score > 0.6


def test_first_mode_does_not_reset_session_basis() -> None:
    started = NOW - timedelta(minutes=10)
    signals = SessionSignals(started_at=started)
    fill(signals, started, NOW - timedelta(seconds=65), 2)
    before = signals.chat_rate_signal(NOW)

    signals.note_mode(OPEN, NOW)
    assert signals.mode_changed_at is None
    after = signals.chat_rate_signal(NOW)
    assert after.samples == before.samples
    assert after.score == before.score == 0.0


# -- Detector-Integration ---------------------------------------------------
def test_mode_transition_does_not_trigger_detector() -> None:
    signals = SessionSignals(started_at=NOW - timedelta(minutes=30))
    build_review_transition(signals, NOW)

    detector = Detector(
        threshold=0.60,
        cooldown_s=180,
        pre_roll_s=240,
        weights={
            "chat_rate": 1.0,
            "emote_burst": 0.0,
            "clip_created": 0.0,
            "viewers": 0.0,
            "clip_burst": 0.0,
        },
    )

    # Review-Ticks: 125/130/180/300 sind vor der reifen Basis, 420/480 danach.
    for offset in (125, 130, 180, 300, 419, 420, 480):
        components, _detail = signals.snapshot(NOW + timedelta(seconds=offset))
        assert components["chat_rate"] == 0.0, offset
        decision = detector.update(components, now=NOW + timedelta(seconds=offset), buffer_s=300)
        assert not decision.trigger, offset

    assert detector.triggers == 0


# -- Web-Anzeige ------------------------------------------------------------
class _FakeSupervisor:
    def __init__(self) -> None:
        self.states = {}
        self.enabled_logins = ["alpha"]
        self.total_streamers = 1
        self.gql_requests_last_cycle = 0
        self.clip_requests_last_cycle = 0
        self._snapshot = {
            "state": "live",
            "chat_connected": True,
            "chat_mode": "Sub-only · Slow 30 s",
            "signals_suppressed_s": float(REBUILD_S),
            "viewers": 10,
            "buffer_s": 120.0,
            "last_score": 0.1,
            "last_components": {},
            "last_detail": {},
            "cooldown_remaining_s": 0.0,
        }

    def snapshot(self, streamer_id: int):
        return dict(self._snapshot)


class _FakeSettingsService:
    def __init__(self, values: dict) -> None:
        self.values = values

    async def get(self, force: bool = False) -> dict:
        return dict(self.values)


def test_chat_mode_visible_on_dashboard_and_streamers(tmp_path, monkeypatch) -> None:
    engine = build_engine(tmp_path / "web.db")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(Streamer(login="alpha", display_name="Alpha", enabled=True))
            await session.commit()

    asyncio.run(setup())
    monkeypatch.setattr("app.db._engine", engine)
    monkeypatch.setattr("app.db._sessionmaker", factory)

    values = default_values(AppConfig(storage_root=tmp_path, data_dir=tmp_path))
    app = FastAPI()
    app.include_router(web_routes.router)
    app.state.supervisor = _FakeSupervisor()
    app.state.settings_service = _FakeSettingsService(values)
    app.state.twitch = SimpleNamespace(mode="anonymous-gql")
    app.state.db_path = tmp_path / "web.db"

    try:
        with TestClient(app) as client:
            dashboard = client.get("/")
            assert dashboard.status_code == 200
            assert "Sub-only · Slow 30 s" in dashboard.text
            assert "Signale setzen aus (Übergang)" in dashboard.text

            streamers = client.get("/streamers")
            assert streamers.status_code == 200
            assert "Sub-only · Slow 30 s" in streamers.text
    finally:
        asyncio.run(engine.dispose())
