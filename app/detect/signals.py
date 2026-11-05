"""Signalgewinnung pro Stream-Session (Chat, Emotes, Clips, Viewer)."""

from __future__ import annotations

import re
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..timeutil import iso, utcnow
from ..twitch.chat import ChatMessage
from ..twitch.types import Clip
from .keywords import keyword_pattern, parse_keywords
from .weights import DEFAULT_WEIGHTS, sanitize_weights  # re-export for existing imports

BUCKET_S = 5
HISTORY_S = 900
MIN_HISTORY_S = 300
RECENT_EXCLUDE_S = 60
CLIP_WINDOW_S = 180
VIEWER_DELTA_S = 120
STREAMER_COMMAND_HOLD_S = 30
COPYPASTA_HOLD_S = 15
COPYPASTA_RETENTION_S = 30
COPYPASTA_MAX_RECENT = 2000

CLIP_COMMAND_RE = re.compile(r"(?i)(?:^|\s)(?:!clip|clip\s+(?:it|that|das)|clippen|!highlight)(?:\s|$)")
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
PUNCTUATION_RE = re.compile(r"[^\w\s]")
WHITESPACE_RE = re.compile(r"\s+")

HYPE_EMOTES = (
    "KEKW",
    "LUL",
    "OMEGALUL",
    "Pog",
    "PogChamp",
    "monkaS",
    "PepeHands",
    "catJAM",
    "Clap",
    "ICANT",
    "HUH",
)

def bucket_key(now: datetime) -> int:
    aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    return int(aware.timestamp() // BUCKET_S) * BUCKET_S


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def burst_score(metric: float, median: float) -> float:
    """Score-Formel aus der Spec: (x - 2*median) / (4*median + 4)."""
    return clamp((metric - 2.0 * median) / (4.0 * median + 4.0))


def identity(message: ChatMessage) -> str:
    """Stabile Nutzerkennung: Twitch-ID, sonst Anzeigename."""
    return message.user_id or message.user


def _normalize_text(text: str) -> str:
    """Vergleichsform für Copypasta: klein, ohne URLs und Satzzeichen."""
    lowered = URL_RE.sub(" ", text.lower())
    without_punctuation = PUNCTUATION_RE.sub("", lowered)
    return WHITESPACE_RE.sub(" ", without_punctuation).strip()


def _has_streamer_badge(badges: str) -> bool:
    """True, wenn die Badge-Liste Streamer- oder Moderator-Rechte enthält."""
    tokens = {badge.strip() for badge in badges.split(",")}
    return bool(tokens & {"broadcaster/1", "moderator/1"})


def viewers_score(delta: float) -> float:
    """Relative Zuschaueränderung; nur positive Werte zählen."""
    return clamp(delta / 0.10)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * q))
    return ordered[max(0, min(len(ordered) - 1, index))]


@dataclass(slots=True)
class Bucket:
    messages: int = 0
    emote_only: int = 0
    hype: int = 0
    users: set[str] = field(default_factory=set)
    hype_keywords: int = 0
    clip_commands: int = 0
    hype_matches: set[str] = field(default_factory=set)


@dataclass(slots=True)
class SignalDetail:
    score: float = 0.0
    metric: float = 0.0
    median: float = 0.0
    samples: int = 0
    extra: dict[str, float | str | None] = field(default_factory=dict)


class SessionSignals:
    """Hält die 5-s-Buckets einer Aufnahme-Session.

    Reine In-Memory-Struktur ohne I/O, damit sie gut testbar bleibt. Die
    Zeitpunkte werden explizit übergeben (kein verstecktes ``now()``).
    """

    def __init__(
        self,
        started_at: datetime | None = None,
        hype_keywords: str | None = None,
        copypasta_min_users: int = 3,
        copypasta_window_s: int = 10,
    ) -> None:
        self.started_at = started_at or utcnow()
        self.buckets: dict[int, Bucket] = defaultdict(Bucket)
        self.viewer_samples: deque[tuple[datetime, int]] = deque(maxlen=400)
        self.recent_clips: dict[str, Clip] = {}
        self.seen_clip_ids: set[str] = set()
        self.last_clip_poll: datetime | None = None
        self._hype_pattern = keyword_pattern(parse_keywords(hype_keywords))
        self.copypasta_min_users = int(copypasta_min_users)
        self.copypasta_window_s = int(copypasta_window_s)
        self._streamer_command_until: datetime | None = None
        self._recent_texts: deque[tuple[datetime, str, str]] = deque(maxlen=COPYPASTA_MAX_RECENT)
        self._copypasta_until: datetime | None = None
        self._copypasta_users: set[str] = set()

    # -- Chat ---------------------------------------------------------------
    def on_chat(self, message: ChatMessage, now: datetime | None = None) -> None:
        now = now or message.ts
        key = bucket_key(now)
        bucket = self.buckets[key]
        bucket.messages += 1
        if message.emote_only:
            bucket.emote_only += 1
        bucket.hype += max(0, message.hype_count)
        bucket.users.add(identity(message))
        matches = self._hype_pattern.findall(message.text)
        if matches:
            bucket.hype_keywords += 1
            bucket.hype_matches.update(match.lower() for match in matches)
        if CLIP_COMMAND_RE.search(message.text):
            bucket.clip_commands += 1
            if _has_streamer_badge(message.badges):
                self._streamer_command_until = now + timedelta(seconds=STREAMER_COMMAND_HOLD_S)
        cutoff = int((now - timedelta(seconds=HISTORY_S)).timestamp())
        for old_key in [k for k in self.buckets if k < cutoff]:
            self.buckets.pop(old_key, None)
        self._track_copypasta(message, now)

    def _track_copypasta(self, message: ChatMessage, now: datetime) -> None:
        """Sammelt wiederholte Texte und löst bei genug Nutzern den Hold aus."""
        normalized = _normalize_text(message.text)
        if message.emote_only or len(normalized) < 4:
            return
        self._recent_texts.append((now, normalized, identity(message)))
        retention_cutoff = now - timedelta(seconds=COPYPASTA_RETENTION_S)
        while self._recent_texts and self._recent_texts[0][0] < retention_cutoff:
            self._recent_texts.popleft()
        window_start = now - timedelta(seconds=self.copypasta_window_s)
        users_by_text: dict[str, set[str]] = defaultdict(set)
        for ts, text, user in self._recent_texts:
            if ts >= window_start:
                users_by_text[text].add(user)
        for users in users_by_text.values():
            if len(users) >= self.copypasta_min_users:
                self._copypasta_until = now + timedelta(seconds=COPYPASTA_HOLD_S)
                self._copypasta_users = set(users)
                break

    def copypasta_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        if self._copypasta_until is not None and now >= self._copypasta_until:
            self._copypasta_until = None
            self._copypasta_users.clear()
        active = self._copypasta_until is not None
        return SignalDetail(
            score=1.0 if active else 0.0,
            metric=1.0 if active else 0.0,
            extra={
                "active": 1.0 if active else 0.0,
                "users": len(self._copypasta_users) if active else 0,
                "window_s": self.copypasta_window_s,
            },
        )

    # -- Viewer -------------------------------------------------------------
    def add_viewers(self, viewers: int, now: datetime | None = None) -> None:
        now = now or utcnow()
        self.viewer_samples.append((now, max(0, int(viewers))))
        cutoff = now - timedelta(seconds=HISTORY_S)
        while self.viewer_samples and self.viewer_samples[0][0] < cutoff:
            self.viewer_samples.popleft()

    def viewer_delta(self, now: datetime | None = None) -> float | None:
        now = now or utcnow()
        target = now - timedelta(seconds=VIEWER_DELTA_S)
        if len(self.viewer_samples) < 2:
            return None
        base: tuple[datetime, int] | None = None
        for sample in self.viewer_samples:
            if sample[0] <= target + timedelta(seconds=15):
                base = sample
            else:
                break
        if base is None or base[1] <= 0:
            return None
        latest = self.viewer_samples[-1]
        if latest[0] <= base[0]:
            return None
        return (latest[1] - base[1]) / base[1]

    # -- Clips --------------------------------------------------------------
    def add_clips(self, clips: list[Clip], now: datetime | None = None) -> list[Clip]:
        now = now or utcnow()
        self.last_clip_poll = now
        fresh: list[Clip] = []
        for clip in clips:
            if clip.id in self.seen_clip_ids:
                continue
            self.seen_clip_ids.add(clip.id)
            # Nur Clips, deren Moment in dieser Session liegt (Puffer deckt ihn ab).
            if clip.created_at < self.started_at:
                continue
            if clip.created_at < now - timedelta(seconds=CLIP_WINDOW_S):
                continue
            if clip.created_at > now + timedelta(minutes=5):
                continue
            self.recent_clips[clip.id] = clip
            fresh.append(clip)
        cutoff = now - timedelta(seconds=CLIP_WINDOW_S)
        for clip_id in [cid for cid, clip in self.recent_clips.items() if clip.created_at < cutoff]:
            self.recent_clips.pop(clip_id, None)
        return fresh

    def clip_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        cutoff = now - timedelta(seconds=CLIP_WINDOW_S)
        active = [clip for clip in self.recent_clips.values() if clip.created_at >= cutoff]
        if not active:
            return SignalDetail(score=0.0, metric=0.0, extra={"clip_burst": 0.0})
        active.sort(key=lambda clip: clip.created_at)
        latest = active[-1]
        anchor = latest.created_at - timedelta(seconds=max(0.0, latest.duration_s) / 2.0)
        anchor = min(now, max(now - timedelta(seconds=300), anchor))
        burst = min(1.0, len(active) / 3.0)
        return SignalDetail(
            score=1.0,
            metric=float(len(active)),
            extra={
                "clip_burst": burst,
                "anchor": anchor.replace(tzinfo=timezone.utc).timestamp(),
                "clips": float(len(active)),
                "clip_id": latest.id,
            },
        )

    # -- Bucket-Auswertung --------------------------------------------------
    @staticmethod
    def _bucket_value(bucket: Bucket, metric: str) -> float:
        if metric == "messages":
            return float(bucket.messages)
        if metric == "unique_chatters":
            return float(len(bucket.users))
        if metric == "hype_keywords":
            return float(bucket.hype_keywords)
        if metric == "clip_commands":
            return float(bucket.clip_commands)
        return float(bucket.emote_only + bucket.hype)

    def _window_values(self, now: datetime, metric: str, upto_offset_s: int = RECENT_EXCLUDE_S) -> list[float]:
        """Werte aller 5-s-Buckets der letzten 15 min, ohne die letzten 60 s.

        Fehlende Buckets zählen als 0, sonst würde ein stiller Chat den Median
        künstlich nach oben ziehen. Rückgabe ist auf das 99. Perzentil gekappt.
        """
        end = now - timedelta(seconds=upto_offset_s)
        start = max(self.started_at, now - timedelta(seconds=HISTORY_S))
        values: list[float] = []
        current = bucket_key(start)
        end_key = bucket_key(end)
        while current <= end_key:
            bucket = self.buckets.get(current)
            values.append(0.0 if bucket is None else self._bucket_value(bucket, metric))
            current += BUCKET_S
        if not values:
            return []
        p99 = percentile(values, 0.99)
        return [min(value, p99) for value in values]

    def _bucket_metric(self, now: datetime, metric: str) -> float:
        # Letzter abgeschlossener 5-s-Bucket (Ticks können mitten im Bucket liegen).
        key = bucket_key(now) - BUCKET_S
        bucket = self.buckets.get(key)
        if bucket is None:
            return 0.0
        return self._bucket_value(bucket, metric)

    def _burst_signal(self, now: datetime, metric: str, extra_key: str) -> SignalDetail:
        value = self._bucket_metric(now, metric)
        if now - self.started_at < timedelta(seconds=MIN_HISTORY_S):
            return SignalDetail(score=0.0, metric=value, samples=0)
        values = self._window_values(now, metric)
        if len(values) < 12:
            return SignalDetail(score=0.0, metric=value, samples=len(values))
        median = statistics.median(values)
        return SignalDetail(
            score=burst_score(value, median),
            metric=value,
            median=median,
            samples=len(values),
            extra={extra_key: value, "median": median},
        )

    def unique_chatters_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        return self._burst_signal(now, "unique_chatters", "unique_chatters")

    def hype_keywords_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        detail = self._burst_signal(now, "hype_keywords", "hype_keywords")
        bucket = self.buckets.get(bucket_key(now) - BUCKET_S)
        matched = ",".join(sorted(bucket.hype_matches)) if bucket is not None else ""
        detail.extra["matched"] = matched
        return detail

    def clip_commands_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        return self._burst_signal(now, "clip_commands", "clip_commands")

    def streamer_command_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        if self._streamer_command_until is not None and now >= self._streamer_command_until:
            self._streamer_command_until = None
        hit = self._streamer_command_until is not None
        return SignalDetail(
            score=1.0 if hit else 0.0,
            metric=1.0 if hit else 0.0,
            extra={"hit": 1.0 if hit else 0.0, "until": iso(self._streamer_command_until)},
        )

    def chat_rate_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        metric = self._bucket_metric(now, "messages")
        if now - self.started_at < timedelta(seconds=MIN_HISTORY_S):
            return SignalDetail(score=0.0, metric=metric, samples=0)
        values = self._window_values(now, "messages")
        if len(values) < 12:
            return SignalDetail(score=0.0, metric=metric, samples=len(values))
        median = statistics.median(values)
        return SignalDetail(
            score=burst_score(metric, median),
            metric=metric,
            median=median,
            samples=len(values),
            extra={"rate": metric, "median": median},
        )

    def emote_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        metric = self._bucket_metric(now, "emotes")
        if now - self.started_at < timedelta(seconds=MIN_HISTORY_S):
            return SignalDetail(score=0.0, metric=metric, samples=0)
        values = self._window_values(now, "emotes")
        if len(values) < 12:
            return SignalDetail(score=0.0, metric=metric, samples=len(values))
        median = statistics.median(values)
        return SignalDetail(
            score=burst_score(metric, median),
            metric=metric,
            median=median,
            samples=len(values),
            extra={"emote_burst": metric, "median": median},
        )

    def viewer_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        delta = self.viewer_delta(now)
        if delta is None:
            return SignalDetail(score=0.0, extra={"viewers_delta": 0.0})
        return SignalDetail(
            score=viewers_score(delta),
            metric=delta,
            extra={"viewers_delta": delta},
        )

    def snapshot(self, now: datetime | None = None) -> tuple[dict[str, float], dict[str, object]]:
        """Berechnet alle Komponenten für einen Tick."""
        now = now or utcnow()
        chat = self.chat_rate_signal(now)
        emote = self.emote_signal(now)
        clip_detail = self.clip_signal(now)
        viewer = self.viewer_signal(now)
        unique = self.unique_chatters_signal(now)
        hype = self.hype_keywords_signal(now)
        copypasta = self.copypasta_signal(now)
        clip_commands = self.clip_commands_signal(now)
        streamer_command = self.streamer_command_signal(now)

        components: dict[str, float] = {
            "chat_rate": round(chat.score, 4),
            "emote_burst": round(emote.score, 4),
            "clip_created": round(clip_detail.score, 4),
            "viewers": round(viewer.score, 4),
            "clip_burst": round(float(clip_detail.extra.get("clip_burst", 0.0)), 4),
            "unique_chatters": round(unique.score, 4),
            "hype_keywords": round(hype.score, 4),
            "copypasta": round(copypasta.score, 4),
            "clip_commands": round(clip_commands.score, 4),
            "streamer_command": round(streamer_command.score, 4),
        }
        detail: dict[str, object] = {
            "chat_rate": {
                "value": chat.metric,
                "median": round(chat.median, 3),
                "samples": chat.samples,
            },
            "emote_burst": {
                "value": emote.metric,
                "median": round(emote.median, 3),
                "samples": emote.samples,
            },
            "clip_created": {
                "clips": int(clip_detail.extra.get("clips", 0)),
                "clip_id": clip_detail.extra.get("clip_id"),
                "anchor": clip_detail.extra.get("anchor"),
            },
            "viewers": {"delta": round(float(viewer.extra.get("viewers_delta", 0.0)), 4)},
            "unique_chatters": {
                "value": unique.metric,
                "median": round(unique.median, 3),
                "samples": unique.samples,
            },
            "hype_keywords": {
                "value": hype.metric,
                "median": round(hype.median, 3),
                "samples": hype.samples,
                "matched": hype.extra.get("matched") or "",
            },
            "copypasta": {
                "active": copypasta.extra.get("active", 0.0),
                "users": copypasta.extra.get("users", 0),
                "window_s": copypasta.extra.get("window_s", self.copypasta_window_s),
            },
            "clip_commands": {
                "value": clip_commands.metric,
                "median": round(clip_commands.median, 3),
                "samples": clip_commands.samples,
            },
            "streamer_command": {
                "hit": streamer_command.extra.get("hit", 0.0),
                "until": streamer_command.extra.get("until"),
            },
        }
        return components, detail


def fuse(components: dict[str, float], weights: dict[str, float]) -> float:
    """Gewichtete Fusion; die Summe der Gewichte wird auf 1 normalisiert."""
    total_weight = sum(max(0.0, float(value)) for value in weights.values())
    if total_weight <= 0:
        return 0.0
    score = 0.0
    for key, weight in weights.items():
        score += max(0.0, float(weight)) * clamp(float(components.get(key, 0.0)))
    return clamp(score / total_weight)
