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

    Trigger, wenn ``score >= threshold`` in zwei aufeinanderfolgenden Ticks,
    der Cooldown abgelaufen ist und der Puffer den Vorlauf abdeckt.
    """

    def __init__(
        self,
        threshold: float = 0.60,
        cooldown_s: int = 180,
        pre_roll_s: int = 240,
        weights: dict[str, float] | None = None,
        required_ticks: int = 2,
    ) -> None:
        self.threshold = float(threshold)
        self.cooldown_s = int(cooldown_s)
        self.pre_roll_s = int(pre_roll_s)
        self.weights = sanitize_weights(weights)
        self.required_ticks = max(1, int(required_ticks))
        self.consecutive = 0
        self.last_trigger_at: datetime | None = None
        self.last_score = 0.0
        self.last_components: dict[str, float] = {}
        self.triggers = 0

    def configure(
        self,
        threshold: float | None = None,
        cooldown_s: int | None = None,
        pre_roll_s: int | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        if threshold is not None:
            self.threshold = float(threshold)
        if cooldown_s is not None:
            self.cooldown_s = int(cooldown_s)
        if pre_roll_s is not None:
            self.pre_roll_s = int(pre_roll_s)
        if weights:
            self.weights = sanitize_weights(weights)

    def update(
        self,
        components: dict[str, float],
        now: datetime | None = None,
        buffer_s: float = 0.0,
        anchor_ts: float | None = None,
        weights: dict[str, float] | None = None,
    ) -> TriggerDecision:
        now = now or utcnow()
        effective_weights = sanitize_weights(weights or self.weights)
        score = fuse(components, effective_weights)
        self.last_score = score
        self.last_components = dict(components)

        if score >= self.threshold:
            self.consecutive += 1
        else:
            self.consecutive = 0

        cooldown_remaining = 0.0
        if self.last_trigger_at is not None:
            elapsed = (now - self.last_trigger_at).total_seconds()
            cooldown_remaining = max(0.0, self.cooldown_s - elapsed)

        notes: list[str] = []
        if score < self.threshold:
            notes.append("Score unter Schwelle")
        if self.consecutive < self.required_ticks and score >= self.threshold:
            notes.append(f"Hysterese {self.consecutive}/{self.required_ticks}")
        if cooldown_remaining > 0:
            notes.append(f"Cooldown noch {cooldown_remaining:.0f} s")
        if buffer_s < self.pre_roll_s:
            notes.append(f"Puffer {buffer_s:.0f} s < Vorlauf {self.pre_roll_s} s")

        trigger = (
            score >= self.threshold
            and self.consecutive >= self.required_ticks
            and cooldown_remaining <= 0
            and buffer_s >= self.pre_roll_s
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
            "consecutive": self.consecutive,
            "last_score": round(self.last_score, 4),
            "last_components": dict(self.last_components),
            "last_trigger_at": self.last_trigger_at.isoformat() if self.last_trigger_at else None,
            "triggers": self.triggers,
        }
