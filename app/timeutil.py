"""Zeit-Helfer.

Intern wird durchgängig mit naiven UTC-Datetimes gearbeitet. SQLite speichert
keine Zeitzone; naive UTC vermeidet die üblichen Fallen beim Vergleich
aware/naive. Erst für die Anzeige wird in die lokale Zeitzone umgerechnet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Aktuelle Zeit als naive UTC-Datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_iso(value: str | None) -> datetime | None:
    """ISO-8601 (auch mit ``Z``) nach naiver UTC-Datetime."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso(dt: datetime | None) -> str | None:
    """Naive UTC-Datetime als ISO-8601 mit ``Z``."""
    if dt is None:
        return None
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return aware.isoformat().replace("+00:00", "Z")


def localtime(dt: datetime | str | None, fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
    """UTC-Datetime (oder ISO-String) für die UI in lokale Zeit umrechnen."""
    if dt is None or dt == "":
        return "–"
    if isinstance(dt, str):
        dt = parse_iso(dt)
        if dt is None:
            return "–"
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return aware.astimezone().strftime(fmt)


def unix(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    return aware.timestamp()


def duration_hms(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def human_bytes(value: float | int | None) -> str:
    if value is None:
        return "–"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def since(ts: datetime | None, now: datetime | None = None) -> timedelta:
    return (now or utcnow()) - ts if ts else timedelta(0)
