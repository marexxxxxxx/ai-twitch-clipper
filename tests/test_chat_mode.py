from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.detect.detector import Detector
from app.detect.signals import (
    BUCKET_S,
    MODE_SETTLE_S,
    SessionSignals,
    bucket_key,
)
from app.twitch.chat import ChatMessage, ChatMode, ChatReader, parse_privmsg, parse_roomstate

NOW = datetime(2026, 11, 4, 12, 0, 0)

OPEN = ChatMode()
SUBS_ONLY = ChatMode(subs_only=True)
EMOTE_ONLY = ChatMode(emote_only=True)


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


def line(tags: str) -> str:
    return f"@{tags} :tmi.twitch.tv ROOMSTATE #chan"


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

    slow = parse_roomstate(line("followers-only=-1;slow=30;subs-only=0;emote-only=0"))
    assert slow is not None and slow.slow == 30
    assert slow.label == "Slow 30 s"


def test_parse_roomstate_open_chat_and_defaults() -> None:
    mode = parse_roomstate(line("emote-only=0;followers-only=-1;slow=0;subs-only=0"))
    assert mode == OPEN
    assert mode is not None and not mode.restricted and mode.label == ""

    # Fehlende Tags fallen auf Defaults (offener Chat) zurück.
    assert parse_roomstate(line("room-id=123")) == OPEN


def test_parse_roomstate_combined_label() -> None:
    mode = parse_roomstate(line("emote-only=1;followers-only=10;slow=30;subs-only=1"))
    assert mode is not None
    assert mode.label == "Sub-only · Emote-only · Followers-only 10 min · Slow 30 s"


def test_parse_roomstate_ignores_non_roomstate() -> None:
    assert parse_roomstate("PING :tmi.twitch.tv") is None
    assert parse_roomstate("@display-name=A :a!a@a.tmi.twitch.tv PRIVMSG #chan :hi") is None
    assert parse_privmsg(line("subs-only=1")) is None


# -- ChatReader-Callback ----------------------------------------------------
def test_chat_reader_mode_callback_only_on_change() -> None:
    calls: list[ChatMode] = []
    reader = ChatReader("chan", lambda _message: None, on_mode=calls.append)
    first = line("emote-only=0;followers-only=-1;slow=0;subs-only=1")
    same = line("emote-only=0;followers-only=-1;slow=0;subs-only=1")
    changed = line("emote-only=0;followers-only=-1;slow=0;subs-only=0")

    reader._apply_mode(parse_roomstate(first))  # type: ignore[arg-type]
    reader._apply_mode(parse_roomstate(same))  # type: ignore[arg-type]
    assert len(calls) == 1
    assert calls[0].label == "Sub-only"
    assert reader.mode == SUBS_ONLY

    reader._apply_mode(parse_roomstate(changed))  # type: ignore[arg-type]
    assert len(calls) == 2
    assert calls[1] == OPEN


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

    # Zurückschalten auf offen: normaler Chat, im ersten Bucket ein Ausreißer.
    signals.note_mode(OPEN, NOW)
    assert signals.buckets == {}, "Buckets der Sub-only-Phase müssen verworfen sein"
    for index in range(30):
        signals.on_chat(message(NOW + timedelta(seconds=5, milliseconds=index)))
    fill(signals, NOW + timedelta(seconds=10), NOW + timedelta(seconds=65), 4)

    # Innerhalb der Settle-Zeit bleibt der Chat-Score trotz Ausreißer bei 0.
    assert signals.chat_rate_signal(NOW + timedelta(seconds=10)).score == 0.0
    assert signals.chat_rate_signal(NOW + timedelta(seconds=MODE_SETTLE_S - 1)).score == 0.0

    # Nach dem Settle wird gegen die neue offene Phase verglichen, nicht gegen
    # die (leere) Sub-only-Historie – kein False Positive.
    settled = signals.chat_rate_signal(NOW + timedelta(seconds=MODE_SETTLE_S + 5))
    assert settled.samples >= 12
    assert settled.score == 0.0

    # Fehlende Buckets der neuen Phase zählen erst, wenn genug vergangen sind.
    early = signals.chat_rate_signal(NOW + timedelta(seconds=MODE_SETTLE_S + 1))
    assert early.samples >= 12


def test_emote_only_transition_does_not_burst() -> None:
    started = NOW - timedelta(minutes=30)
    signals = SessionSignals(started_at=started)
    signals.note_mode(OPEN, started)
    t_switch = NOW - timedelta(minutes=10)
    fill(signals, started, t_switch, 2, emote_only=True)

    signals.note_mode(EMOTE_ONLY, t_switch)
    fill(signals, t_switch, t_switch + timedelta(seconds=MODE_SETTLE_S - 10), 10, emote_only=True)

    detail = signals.emote_signal(t_switch + timedelta(seconds=MODE_SETTLE_S - 10))
    assert detail.score == 0.0
    assert detail.metric > 0.6

    # Auch nach dem Settle ist die Emote-Phase die neue Basis – kein Burst.
    fill(signals, t_switch + timedelta(seconds=MODE_SETTLE_S), NOW + timedelta(seconds=30), 10, emote_only=True)
    after = signals.emote_signal(NOW + timedelta(seconds=30))
    assert after.score == 0.0


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
    started = NOW - timedelta(minutes=30)
    signals = SessionSignals(started_at=started)
    signals.note_mode(OPEN, started)
    fill(signals, started, NOW - timedelta(minutes=10), 4)

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

    # Sub-only an, danach zurück auf offen mit einem echten Burst.
    signals.note_mode(SUBS_ONLY, NOW - timedelta(minutes=10))
    signals.note_mode(OPEN, NOW)
    for index in range(40):
        signals.on_chat(message(NOW + timedelta(seconds=5, milliseconds=index)))

    decisions = []
    for offset in (10, 15, 20, 25, MODE_SETTLE_S - 1):
        components, _detail = signals.snapshot(NOW + timedelta(seconds=offset))
        assert components["chat_rate"] == 0.0
        decisions.append(detector.update(components, now=NOW + timedelta(seconds=offset), buffer_s=300))

    assert not any(decision.trigger for decision in decisions)
    assert detector.triggers == 0
