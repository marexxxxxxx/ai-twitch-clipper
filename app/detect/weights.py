"""Gemeinsame Signal-Gewichte für Detector und Laufzeit-Einstellungen."""

from __future__ import annotations

#: Bestehende fünf Signale unverändert; neue Signale starten in der Shadow-Phase bei 0.
DEFAULT_WEIGHTS: dict[str, float] = {
    "chat_rate": 0.35,
    "emote_burst": 0.20,
    "clip_created": 0.30,
    "viewers": 0.15,
    "clip_burst": 0.10,
    "unique_chatters": 0.0,
    "hype_keywords": 0.0,
    "copypasta": 0.0,
    "clip_commands": 0.0,
    "streamer_command": 0.0,
}

#: Startprofil nach der Shadow-Auswertung (Summe = 1,0).
CHAT_PROFILE_WEIGHTS: dict[str, float] = {
    "chat_rate": 0.20,
    "unique_chatters": 0.15,
    "emote_burst": 0.15,
    "hype_keywords": 0.10,
    "copypasta": 0.10,
    "clip_commands": 0.05,
    "streamer_command": 0.05,
    "clip_created": 0.10,
    "viewers": 0.05,
    "clip_burst": 0.05,
}

NEW_SIGNAL_KEYS: tuple[str, ...] = (
    "unique_chatters",
    "hype_keywords",
    "copypasta",
    "clip_commands",
    "streamer_command",
)


def sanitize_weights(weights: dict[str, float] | None) -> dict[str, float]:
    """Füllt fehlende Schlüssel auf und begrenzt Werte auf ``0.0..1.0``."""
    if not weights:
        return dict(DEFAULT_WEIGHTS)
    result: dict[str, float] = {}
    for key, default in DEFAULT_WEIGHTS.items():
        try:
            value = float(weights.get(key, default))
        except (TypeError, ValueError):
            value = default
        result[key] = max(0.0, min(1.0, value))
    if sum(result.values()) <= 0:
        return dict(DEFAULT_WEIGHTS)
    return result


def active_weights(weights: dict[str, float] | None, shadow_mode: bool) -> dict[str, float]:
    """Liefert die wirksamen Gewichte; im Shadow-Mode bleiben neue Signale aus."""
    result = sanitize_weights(weights)
    if shadow_mode:
        for key in NEW_SIGNAL_KEYS:
            result[key] = 0.0
    return result
