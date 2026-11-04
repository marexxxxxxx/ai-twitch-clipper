from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.detect.signals import (
    BUCKET_S,
    SessionSignals,
    bucket_key,
    burst_score,
    viewers_score,
)
from app.twitch.chat import ChatMessage, parse_privmsg
from app.twitch.types import Clip

NOW = datetime(2026, 11, 4, 12, 0, 0)


def message(ts: datetime, text: str = "hi", emote_only: bool = False, hype: int = 0) -> ChatMessage:
    return ChatMessage(ts=ts, user="tester", text=text, emotes=[], emote_only=emote_only, hype_count=hype)


def at(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def epoch(value: datetime) -> float:
    return value.replace(tzinfo=timezone.utc).timestamp()


def test_burst_and_viewer_scores() -> None:
    assert burst_score(10, 2) == 0.5
    assert burst_score(2, 2) == 0.0
    assert burst_score(100, 1) == 1.0
    assert viewers_score(0.10) == 1.0
    assert viewers_score(0.05) == 0.5
    assert viewers_score(-0.2) == 0.0


def test_chat_rate_detects_burst_against_median() -> None:
    started = NOW - timedelta(minutes=10)
    signals = SessionSignals(started_at=started)
    # Gleichmäßiger Basis-Chat: 2 Nachrichten pro 5-s-Bucket.
    current = bucket_key(started)
    while current <= int(epoch(NOW - timedelta(seconds=65))):
        for offset in (1, 2):
            signals.on_chat(message(at(current + offset)))
        current += BUCKET_S
    # Burst im letzten abgeschlossenen Bucket.
    burst_key = bucket_key(NOW) - BUCKET_S
    for index in range(30):
        signals.on_chat(message(at(burst_key + 1)))
    detail = signals.chat_rate_signal(NOW)
    assert detail.samples > 60
    assert detail.score == 1.0
    assert detail.median == 2


def test_chat_score_zero_without_minimum_history() -> None:
    signals = SessionSignals(started_at=NOW - timedelta(seconds=120))
    for index in range(20):
        signals.on_chat(message(NOW - timedelta(seconds=index)))
    assert signals.chat_rate_signal(NOW).score == 0.0


def test_viewer_delta_over_two_minutes() -> None:
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    signals.add_viewers(1000, NOW - timedelta(seconds=130))
    signals.add_viewers(1100, NOW)
    detail = signals.viewer_signal(NOW)
    assert detail.score == 1.0
    assert detail.metric == 0.1


def test_clip_signal_and_burst() -> None:
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    clip = Clip(id="c1", title="wow", created_at=NOW - timedelta(seconds=30), duration_s=20, view_count=10)
    fresh = signals.add_clips([clip], NOW)
    assert len(fresh) == 1
    detail = signals.clip_signal(NOW)
    assert detail.score == 1.0
    assert detail.extra["clip_burst"] == 1 / 3
    assert detail.extra["anchor"] < epoch(NOW)

    more = [
        Clip(id=f"c{i}", title="x", created_at=NOW - timedelta(seconds=10 * i), duration_s=10)
        for i in range(2, 5)
    ]
    signals.add_clips(more, NOW)
    assert signals.clip_signal(NOW).extra["clip_burst"] == 1.0


def test_old_clips_are_ignored() -> None:
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    old = Clip(id="old", title="", created_at=NOW - timedelta(minutes=10), duration_s=20)
    assert signals.add_clips([old], NOW) == []
    assert signals.clip_signal(NOW).score == 0.0


def test_parse_privmsg_emote_only() -> None:
    line = (
        "@badge-info=;display-name=Tester;emotes=25:0-4/1902:6-10;mod=0 "
        ":tester!tester@tester.tmi.twitch.tv PRIVMSG #chan :Kappa Keepo"
    )
    parsed = parse_privmsg(line)
    assert parsed is not None
    assert parsed.emote_only is True
    assert parsed.user == "Tester"
    assert len(parsed.emotes) == 2


def test_parse_privmsg_hype_and_text() -> None:
    hype = parse_privmsg(
        "@display-name=A;emotes=;mod=0 :a!a@a.tmi.twitch.tv PRIVMSG #chan :KEKW"
    )
    assert hype is not None and hype.emote_only and hype.hype_count == 1

    plain = parse_privmsg(
        "@display-name=B;emotes=;mod=0 :b!b@b.tmi.twitch.tv PRIVMSG #chan :gutes Spiel"
    )
    assert plain is not None and not plain.emote_only and plain.hype_count == 0


def test_parse_privmsg_ignores_non_messages() -> None:
    assert parse_privmsg("PING :tmi.twitch.tv") is None
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan :Welcome") is None
