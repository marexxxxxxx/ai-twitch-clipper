"""Erkennung: Fusion, Hysterese, Cooldown, Zustandsautomat."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..timeutil import utcnow
from .signals import DEFAULT_WEIGHTS, clamp, fuse, sanitize_weights

COMPONENT_LABELS = {
    "chat_rate": "Chat-Rate",
    "emote_burst": "Emote-Burst",
    "clip_created": "Neuer Clip",
    "viewers": "Zuschauer-Anstieg",
    "clip_burst": "Clip-Häufung",
    "unique_chatters": "Unique Chatter",
    "hype_keywords": "Hype-Keywords",
    "copypasta": "Copypasta",
    "clip_commands": "Clip-Kommandos",
    "streamer_command": "Streamer/Mod-Kommando",
}


@dataclass(slots=True)
class TriggerDecision:
    trigger: bool
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    consecutive: int = 0
    cooldown_remaining_s: float = 0.0
    buffer_s: float = 0.0
    anchor_ts: float | None = None
    notes: list[str] = field(default_factory=list)


def describe_trigger(components: dict[str, float], weights: dict[str, float]) -> str:
    """Erklärt in einem Satz, welche Signale den Trigger getragen haben."""
    total = sum(max(0.0, float(w)) for w in weights.values()) or 1.0
    contributions = {
        key: max(0.0, float(weights.get(key, 0.0))) * clamp(float(components.get(key, 0.0))) / total
        for key in weights
    }
    ranked = sorted(contributions.items(), key=lambda item: item[1], reverse=True)
    parts = [
        f"{COMPONENT_LABELS.get(key, key)} {value:.2f}"
        for key, value in ranked
        if value >= 0.01
    ][:3]
    if not parts:
        return "Schwelle überschritten, keine Einzelkomponente dominant."
    return "Beitrag: " + ", ".join(parts)


class Detector:
    """Zustandsautomat je Stream-Session.

    Trigger, wenn ``score >= threshold`` in ``required_ticks`` aufeinanderfolgenden
    Ticks, der Cooldown abgelaufen ist, der Puffer den Vorlauf abdeckt und das
    Stundenlimit noch nicht erreicht wurde.
    """

    def __init__(
        self,
        threshold: float = 0.60,
        cooldown_s: int = 180,
        pre_roll_s: int = 240,
        weights: dict[str, float] | None = None,
        required_ticks: int = 2,
        max_clips_per_hour: int = 60,
    ) -> None:
        self.threshold = float(threshold)
        self.cooldown_s = int(cooldown_s)
        self.pre_roll_s = int(pre_roll_s)
        self.weights = sanitize_weights(weights)
        self.required_ticks = max(1, int(required_ticks))
        self.max_clips_per_hour = max(1, int(max_clips_per_hour))
        self.consecutive = 0
        self.last_trigger_at: datetime | None = None
        self.last_score = 0.0
        self.last_components: dict[str, float] = {}
        self.last_recent_triggers = 0
        self.triggers = 0

    def configure(
        self,
        threshold: float | None = None,
        cooldown_s: int | None = None,
        pre_roll_s: int | None = None,
        weights: dict[str, float] | None = None,
        required_ticks: int | None = None,
        max_clips_per_hour: int | None = None,
    ) -> None:
        if threshold is not None:
            self.threshold = float(threshold)
        if cooldown_s is not None:
            self.cooldown_s = int(cooldown_s)
        if pre_roll_s is not None:
            self.pre_roll_s = int(pre_roll_s)
        if weights:
            self.weights = sanitize_weights(weights)
        if required_ticks is not None:
            self.required_ticks = max(1, int(required_ticks))
        if max_clips_per_hour is not None:
            self.max_clips_per_hour = max(1, int(max_clips_per_hour))

    def can_trigger(
        self,
        now: datetime | None = None,
        buffer_s: float = 0.0,
        recent_triggers: int = 0,
    ) -> tuple[bool, list[str]]:
        """Prüft Cooldown, Vorlauf und Stundenlimit ohne Datenbankzugriff.

        ``recent_triggers`` ist die von außen (aus der DB) gelieferte Anzahl
        der Clips der letzten Stunde; der Detector zählt sie nicht selbst.
        """
        now = now or utcnow()
        remaining = self.cooldown_remaining(now)
        recent = max(0, int(recent_triggers))
        reasons: list[str] = []
        if remaining > 0:
            reasons.append(f"Cooldown noch {remaining:.0f} s")
        if buffer_s < self.pre_roll_s:
            reasons.append(f"Puffer {buffer_s:.0f} s < Vorlauf {self.pre_roll_s} s")
        if recent >= self.max_clips_per_hour:
            reasons.append(f"Stundenlimit erreicht ({recent}/{self.max_clips_per_hour} Clips)")
        return (not reasons, reasons)

    def update(
        self,
        components: dict[str, float],
        now: datetime | None = None,
        buffer_s: float = 0.0,
        anchor_ts: float | None = None,
        weights: dict[str, float] | None = None,
        recent_triggers: int = 0,
    ) -> TriggerDecision:
        now = now or utcnow()
        effective_weights = sanitize_weights(weights or self.weights)
        score = fuse(components, effective_weights)
        self.last_score = score
        self.last_components = dict(components)
        self.last_recent_triggers = max(0, int(recent_triggers))

        if score >= self.threshold:
            self.consecutive += 1
        else:
            self.consecutive = 0

        cooldown_remaining = self.cooldown_remaining(now)
        allowed, blockers = self.can_trigger(now, buffer_s, recent_triggers)

        notes: list[str] = []
        if score < self.threshold:
            notes.append("Score unter Schwelle")
        if self.consecutive < self.required_ticks and score >= self.threshold:
            notes.append(f"Hysterese {self.consecutive}/{self.required_ticks}")
        notes.extend(blockers)

        trigger = (
            score >= self.threshold
            and self.consecutive >= self.required_ticks
            and allowed
        )
        decision = TriggerDecision(
            trigger=trigger,
            score=score,
            components=dict(components),
            consecutive=self.consecutive,
            cooldown_remaining_s=cooldown_remaining,
            buffer_s=buffer_s,
            anchor_ts=anchor_ts,
            notes=notes,
        )
        if trigger:
            decision.reason = describe_trigger(components, effective_weights)
            self.last_trigger_at = now
            self.consecutive = 0
            self.triggers += 1
        return decision

    def mark_triggered(self, now: datetime | None = None) -> None:
        """Setzt den Cooldown auch für manuell ausgelöste Aufnahmen."""
        self.last_trigger_at = now or utcnow()
        self.consecutive = 0
        self.triggers += 1

    def cooldown_remaining(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        if self.last_trigger_at is None:
            return 0.0
        return max(0.0, self.cooldown_s - (now - self.last_trigger_at).total_seconds())

    def snapshot(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "cooldown_s": self.cooldown_s,
            "pre_roll_s": self.pre_roll_s,
            "weights": dict(self.weights),
            "required_ticks": self.required_ticks,
            "max_clips_per_hour": self.max_clips_per_hour,
            "consecutive": self.consecutive,
            "last_score": round(self.last_score, 4),
            "last_components": dict(self.last_components),
            "last_trigger_at": self.last_trigger_at.isoformat() if self.last_trigger_at else None,
            "triggers": self.triggers,
            "triggers_last_hour": self.last_recent_triggers,
        }
