"""Signalgewinnung pro Stream-Session (Chat, Emotes, Clips, Viewer)."""

from __future__ import annotations

import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..timeutil import utcnow
from ..twitch.chat import ChatMessage, ChatMode
from ..twitch.types import Clip

BUCKET_S = 5
HISTORY_S = 900
MIN_HISTORY_S = 300
RECENT_EXCLUDE_S = 60
CLIP_WINDOW_S = 180
VIEWER_DELTA_S = 120
#: Nach einem Moduswechsel bleiben Chat-/Emote-Signale so lange stumm, bis
#: sich die neue Basis gefüllt hat (kein Trigger direkt am Übergang).
MODE_SETTLE_S = 120
#: Mindestzahl ausgewerteter 5-s-Buckets in der aktuellen Modus-Phase.
MIN_MODE_BUCKETS = 12

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

DEFAULT_WEIGHTS: dict[str, float] = {
    "chat_rate": 0.35,
    "emote_burst": 0.20,
    "clip_created": 0.30,
    "viewers": 0.15,
    "clip_burst": 0.10,
}


def bucket_key(now: datetime) -> int:
    aware = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    return int(aware.timestamp() // BUCKET_S) * BUCKET_S


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def burst_score(metric: float, median: float) -> float:
    """Score-Formel aus der Spec: (x - 2*median) / (4*median + 4)."""
    return clamp((metric - 2.0 * median) / (4.0 * median + 4.0))


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

    def __init__(self, started_at: datetime | None = None) -> None:
        self.started_at = started_at or utcnow()
        self.buckets: dict[int, Bucket] = defaultdict(Bucket)
        self.viewer_samples: deque[tuple[datetime, int]] = deque(maxlen=400)
        self.recent_clips: dict[str, Clip] = {}
        self.seen_clip_ids: set[str] = set()
        self.last_clip_poll: datetime | None = None
        self.chat_mode: ChatMode | None = None
        self.mode_changed_at: datetime | None = None

    # -- Chat ---------------------------------------------------------------
    def note_mode(self, mode: ChatMode, now: datetime | None = None) -> None:
        """Übernimmt den Chat-Modus; bei einem echten Wechsel wird die Basis neu aufgebaut.

        Der erste Modus einer Session (``None`` → gesetzt) ist kein Übergang und
        setzt keinen Settle-Zeitpunkt – die Session startet bereits in diesem Modus.
        """
        now = now or utcnow()
        if self.chat_mode is None:
            self.chat_mode = mode
            return
        if self.chat_mode == mode:
            return
        self.chat_mode = mode
        # Historie der alten Phase ist nicht vergleichbar (Sub-only, Slow, Emote-only).
        self.buckets.clear()
        self.mode_changed_at = now

    def _settling(self, now: datetime) -> bool:
        return (
            self.mode_changed_at is not None
            and now - self.mode_changed_at < timedelta(seconds=MODE_SETTLE_S)
        )

    def _phase_start(self) -> datetime:
        """Beginn der belastbaren Basis: erst nach dem 120-s-Settle.

        Die erste Minute nach einem Wechsel ist Ramp-up (Nachrichten trudeln
        wieder ein) und darf nicht als Median-Basis dienen.
        """
        if self.mode_changed_at is not None and self.mode_changed_at > self.started_at:
            return self.mode_changed_at + timedelta(seconds=MODE_SETTLE_S)
        return self.started_at

    def _basis_ready(self, now: datetime) -> bool:
        """Mindesthistorie wie beim Sessionstart, gerechnet ab der neuen Basis."""
        return now - self._phase_start() >= timedelta(seconds=MIN_HISTORY_S)

    def on_chat(self, message: ChatMessage, now: datetime | None = None) -> None:
        now = now or message.ts
        key = bucket_key(now)
        bucket = self.buckets[key]
        bucket.messages += 1
        if message.emote_only:
            bucket.emote_only += 1
        bucket.hype += max(0, message.hype_count)
        cutoff = int((now - timedelta(seconds=HISTORY_S)).timestamp())
        for old_key in [k for k in self.buckets if k < cutoff]:
            self.buckets.pop(old_key, None)

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
    def _window_values(self, now: datetime, metric: str, upto_offset_s: int = RECENT_EXCLUDE_S) -> list[float]:
        """Werte aller 5-s-Buckets der letzten 15 min, ohne die letzten 60 s.

        Fehlende Buckets zählen als 0, sonst würde ein stiller Chat den Median
        künstlich nach oben ziehen. Rückgabe ist auf das 99. Perzentil gekappt.
        """
        end = now - timedelta(seconds=upto_offset_s)
        # Nie Buckets vor dem letzten Moduswechsel auswerten: Sie stammen aus
        # einer anderen Chat-Restriktion und würden den Median verfälschen.
        start = max(self._phase_start(), now - timedelta(seconds=HISTORY_S))
        values: list[float] = []
        current = bucket_key(start)
        end_key = bucket_key(end)
        while current <= end_key:
            bucket = self.buckets.get(current)
            if bucket is None:
                values.append(0.0)
            elif metric == "messages":
                values.append(float(bucket.messages))
            else:
                values.append(float(bucket.emote_only + bucket.hype))
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
        return float(bucket.messages if metric == "messages" else bucket.emote_only + bucket.hype)

    def chat_rate_signal(self, now: datetime | None = None) -> SignalDetail:
        now = now or utcnow()
        metric = self._bucket_metric(now, "messages")
        if self._settling(now) or not self._basis_ready(now):
            return SignalDetail(score=0.0, metric=metric, samples=0)
        values = self._window_values(now, "messages")
        if len(values) < MIN_MODE_BUCKETS:
            return SignalDetail(score=0.0, metric=metric, samples=len(values))
        median = statistics.median(values)
        # Sparse-Guard: eine noch nicht volle Basis mit Median 0 ist kein
        # belastbares Signal (sonst würde normaler Chat als Burst gelten).
        if median == 0.0 and not self._basis_ready(now):
            return SignalDetail(score=0.0, metric=metric, median=median, samples=len(values))
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
        if self._settling(now) or not self._basis_ready(now):
            return SignalDetail(score=0.0, metric=metric, samples=0)
        values = self._window_values(now, "emotes")
        if len(values) < MIN_MODE_BUCKETS:
            return SignalDetail(score=0.0, metric=metric, samples=len(values))
        median = statistics.median(values)
        if median == 0.0 and not self._basis_ready(now):
            return SignalDetail(score=0.0, metric=metric, median=median, samples=len(values))
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

        components: dict[str, float] = {
            "chat_rate": round(chat.score, 4),
            "emote_burst": round(emote.score, 4),
            "clip_created": round(clip_detail.score, 4),
            "viewers": round(viewer.score, 4),
            "clip_burst": round(float(clip_detail.extra.get("clip_burst", 0.0)), 4),
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


def sanitize_weights(weights: dict[str, float] | None) -> dict[str, float]:
    if not weights:
        return dict(DEFAULT_WEIGHTS)
    result = dict(DEFAULT_WEIGHTS)
    for key in DEFAULT_WEIGHTS:
        if key in weights:
            try:
                result[key] = max(0.0, float(weights[key]))
            except (TypeError, ValueError):
                result[key] = DEFAULT_WEIGHTS[key]
    if sum(result.values()) <= 0:
        return dict(DEFAULT_WEIGHTS)
    return result
