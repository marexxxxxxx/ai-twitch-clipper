from __future__ import annotations

import json
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


def test_unique_chatters_counts_distinct_users():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    message_ts = NOW - timedelta(seconds=5)
    signals.on_chat(ChatMessage(ts=message_ts, user="Alice", user_id="1", text="a", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    signals.on_chat(ChatMessage(ts=message_ts, user="Alice", user_id="1", text="b", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    signals.on_chat(ChatMessage(ts=message_ts, user="Bob", user_id="2", text="c", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    assert signals._bucket_metric(NOW, "unique_chatters") == 2

def test_hype_keywords_and_clip_commands_are_counted():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), hype_keywords="geil,lol")
    message_ts = NOW - timedelta(seconds=5)
    signals.on_chat(ChatMessage(ts=message_ts, user="A", user_id="1", text="das war geil", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    signals.on_chat(ChatMessage(ts=message_ts, user="B", user_id="2", text="!clip", emotes=[], emote_only=False, hype_count=0), now=message_ts)
    assert signals._bucket_metric(NOW, "hype_keywords") == 1
    assert signals._bucket_metric(NOW, "clip_commands") == 1

def test_streamer_command_hits_and_expires():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    message = ChatMessage(ts=NOW, user="Alpha", user_id="1", text="!clip", emotes=[], emote_only=False, hype_count=0, badges="broadcaster/1")
    signals.on_chat(message, now=NOW)
    assert signals.streamer_command_signal(NOW).score == 1.0
    assert signals.streamer_command_signal(NOW + timedelta(seconds=31)).score == 0.0

def test_regular_user_clip_command_is_not_streamer_command():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    message = ChatMessage(ts=NOW, user="Viewer", user_id="2", text="!clip", emotes=[], emote_only=False, hype_count=0, badges="subscriber/12")
    signals.on_chat(message, now=NOW)
    assert signals.streamer_command_signal(NOW).score == 0.0

def test_copypasta_requires_three_distinct_users():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for user_id, user in (("1", "A"), ("2", "B"), ("3", "C")):
        signals.on_chat(ChatMessage(ts=NOW, user=user, user_id=user_id, text="das ist krass", emotes=[], emote_only=False, hype_count=0), now=NOW)
    assert signals.copypasta_signal(NOW).score == 1.0


def test_copypasta_ignores_same_user_repeats():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for _ in range(3):
        signals.on_chat(ChatMessage(ts=NOW, user="A", user_id="1", text="das ist krass", emotes=[], emote_only=False, hype_count=0), now=NOW)
    assert signals.copypasta_signal(NOW).score == 0.0


def test_copypasta_falls_back_to_display_name():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for user in ("A", "B", "C"):
        signals.on_chat(ChatMessage(ts=NOW, user=user, user_id="", text="das ist krass", emotes=[], emote_only=False, hype_count=0), now=NOW)
    assert signals.copypasta_signal(NOW).score == 1.0


def test_copypasta_expires_after_hold():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for user_id, user in (("1", "A"), ("2", "B"), ("3", "C")):
        signals.on_chat(ChatMessage(ts=NOW, user=user, user_id=user_id, text="das ist krass", emotes=[], emote_only=False, hype_count=0), now=NOW)
    assert signals.copypasta_signal(NOW + timedelta(seconds=14)).score == 1.0
    assert signals.copypasta_signal(NOW + timedelta(seconds=16)).score == 0.0


def test_copypasta_detail_and_normalization():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    texts = ("Das ist KRASS!!!", "das   ist krass", "das ist krass https://example.com")
    for user_id, (user, text) in enumerate(zip(("A", "B", "C"), texts)):
        signals.on_chat(ChatMessage(ts=NOW, user=user, user_id=str(user_id), text=text, emotes=[], emote_only=False, hype_count=0), now=NOW)
    detail = signals.copypasta_signal(NOW)
    assert detail.score == 1.0
    assert detail.extra == {"active": 1.0, "users": 3, "window_s": 10}

    emote_only = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for user_id, user in (("1", "A"), ("2", "B"), ("3", "C")):
        emote_only.on_chat(ChatMessage(ts=NOW, user=user, user_id=user_id, text="das ist krass", emotes=[], emote_only=True, hype_count=0), now=NOW)
    assert emote_only.copypasta_signal(NOW).score == 0.0


def test_copypasta_respects_window():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), copypasta_min_users=3)
    for user_id, user, age in (("1", "A", 11), ("2", "B", 9), ("3", "C", 0)):
        signals.on_chat(ChatMessage(ts=NOW - timedelta(seconds=age), user=user, user_id=user_id, text="das ist krass", emotes=[], emote_only=False, hype_count=0), now=NOW - timedelta(seconds=age))
    assert signals.copypasta_signal(NOW).score == 0.0


def test_snapshot_contains_all_signals_and_safe_detail():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10))
    components, detail = signals.snapshot(NOW)
    assert set(components) == {
        "chat_rate", "emote_burst", "clip_created", "viewers", "clip_burst",
        "unique_chatters", "hype_keywords", "copypasta", "clip_commands", "streamer_command",
    }
    assert "unique_chatters" in detail
    assert "hype_keywords" in detail
    assert "copypasta" in detail
    assert "clip_commands" in detail
    assert "streamer_command" in detail
    assert "text" not in detail["hype_keywords"]
    assert "user" not in detail["copypasta"]


def test_snapshot_detail_shapes_and_aggregate_only():
    signals = SessionSignals(started_at=NOW - timedelta(minutes=10), hype_keywords="geil,lol")
    message_ts = NOW - timedelta(seconds=5)
    chats = (
        ("1", "Alice", "das war geil", "broadcaster/1"),
        ("2", "Bob", "lol geil", "subscriber/12"),
        ("3", "Carol", "einfach geil", ""),
    )
    for user_id, user, text, badges in chats:
        signals.on_chat(
            ChatMessage(
                ts=message_ts,
                user=user,
                user_id=user_id,
                text=text,
                emotes=[],
                emote_only=False,
                hype_count=0,
                badges=badges,
            ),
            now=message_ts,
        )

    _, detail = signals.snapshot(NOW)
    assert set(detail["unique_chatters"]) == {"value", "median", "samples"}
    assert set(detail["hype_keywords"]) == {"value", "median", "samples", "matched"}
    assert set(detail["copypasta"]) == {"active", "users", "window_s"}
    assert set(detail["clip_commands"]) == {"value", "median", "samples"}
    assert set(detail["streamer_command"]) == {"hit", "until"}
    assert detail["hype_keywords"]["matched"] == "geil,lol"

    serialized = json.dumps(detail)
    for raw in ("das war geil", "lol geil", "einfach geil", "Alice", "Bob", "Carol"):
        assert raw not in serialized


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


def test_parse_privmsg_reads_user_id_and_badges():
    line = (
        "@badges=broadcaster/1;display-name=Alpha;emotes=;user-id=123 "
        ":alpha!alpha@alpha.tmi.twitch.tv PRIVMSG #alpha :!clip"
    )
    message = parse_privmsg(line)
    assert message is not None
    assert message.user_id == "123"
    assert message.badges == "broadcaster/1"

def test_parse_privmsg_defaults_user_id_and_badges():
    line = "@display-name=Beta :beta!beta@beta.tmi.twitch.tv PRIVMSG #beta :haha"
    message = parse_privmsg(line)
    assert message is not None
    assert message.user_id == ""
    assert message.badges == ""
