"""Gemeinsame Twitch-Datentypen."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Clip:
    id: str
    title: str
    created_at: datetime
    duration_s: float
    view_count: int = 0


@dataclass(slots=True)
class Channel:
    id: str
    login: str
    display_name: str
    is_live: bool = False
    viewers: int = 0
    started_at: datetime | None = None
    game_name: str | None = None
    stream_id: str | None = None
    clips: list[Clip] = field(default_factory=list)


class TwitchError(Exception):
    """Basisklasse für Twitch-Fehler."""


class UserNotFound(TwitchError):
    """Der Login existiert nicht."""


class TwitchUnavailable(TwitchError):
    """Twitch nicht erreichbar oder unerwartete Antwort."""
