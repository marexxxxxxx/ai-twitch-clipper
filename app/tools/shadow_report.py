"""Shadow-Report für die gespeicherten Chat-Signale.

Liest die SQLite-Datenbank ausschließlich im Read-only-Modus und fasst die
Ticks je Streamer zusammen. Mit ``--simulate-profile`` wird zusätzlich das
Chat-Profil (``CHAT_PROFILE_WEIGHTS``) samt Trigger-Pipeline simuliert.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..detect.signals import fuse, percentile
from ..detect.weights import CHAT_PROFILE_WEIGHTS, DEFAULT_WEIGHTS
from ..timeutil import parse_iso, utcnow

PROFILE_THRESHOLD = 0.40
PROFILE_REQUIRED_TICKS = 1
PROFILE_COOLDOWN_S = 180
PROFILE_MAX_CLIPS_PER_HOUR = 6
TOP_MOMENTS = 3


@dataclass(slots=True)
class Tick:
    sample_id: int
    ts: datetime
    score: float
    components: dict[str, float]
    detail: dict[str, Any]
    login: str


@dataclass(slots=True)
class Simulation:
    candidates: list[tuple[Tick, float]]
    triggers: list[tuple[Tick, float]]


def _connect_read_only(path: Path) -> sqlite3.Connection:
    """Öffnet die DB über einen SQLite-URI-Handler ohne Schreibzugriff."""
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _parse_ts(value: Any) -> datetime | None:
    """SQLite-Zeitstempel als naive UTC-Datetime."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
    else:
        try:
            parsed = parse_iso(str(value))
        except ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode("utf-8", errors="replace")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _components(value: Any) -> dict[str, float]:
    components: dict[str, float] = {}
    for key, raw in _json_dict(value).items():
        try:
            components[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return components


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _load_ticks(connection: sqlite3.Connection, cutoff: datetime) -> list[Tick]:
    if not _table_exists(connection, "signal_sample"):
        return []

    columns = _columns(connection, "signal_sample")
    if "ts" not in columns:
        return []
    id_expression = "s.id" if "id" in columns else "s.rowid"
    streamer_expression = "s.streamer_id" if "streamer_id" in columns else "NULL"
    score_expression = "s.score" if "score" in columns else "0.0"
    components_expression = "s.components" if "components" in columns else "NULL"
    detail_expression = "s.detail" if "detail" in columns else "NULL"
    has_streamer = _table_exists(connection, "streamer") and "streamer_id" in columns
    join = "LEFT JOIN streamer AS st ON st.id = s.streamer_id" if has_streamer else ""
    login_expression = "st.login" if has_streamer else "NULL"

    query = (
        f"SELECT {id_expression} AS id, {streamer_expression} AS streamer_id, "
        f"s.ts AS ts, {score_expression} AS score, "
        f"{components_expression} AS components, {detail_expression} AS detail, "
        f"{login_expression} AS login "
        "FROM signal_sample AS s "
        f"{join} "
        "WHERE s.ts >= ? "
        "ORDER BY s.ts ASC, id ASC"
    )
    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")

    ticks: list[Tick] = []
    for row in connection.execute(query, (cutoff_text,)):
        ts = _parse_ts(row["ts"])
        if ts is None or ts < cutoff:
            continue
        streamer_id = row["streamer_id"]
        login = str(row["login"] or "").strip()
        if not login:
            login = f"Streamer #{streamer_id}" if streamer_id is not None else "unbekannt"
        ticks.append(
            Tick(
                sample_id=int(row["id"]),
                ts=ts,
                score=_as_float(row["score"]),
                components=_components(row["components"]),
                detail=_json_dict(row["detail"]),
                login=login,
            )
        )
    return ticks


def _signal_keys(ticks: list[Tick]) -> list[str]:
    keys = list(DEFAULT_WEIGHTS)
    seen = set(keys)
    for tick in ticks:
        for key in tick.components:
            if key not in seen:
                keys.append(key)
                seen.add(key)
    return keys


def _format_value(value: float) -> str:
    return f"{value:.3f}"


def _format_components(components: dict[str, float]) -> str:
    parts = [
        f"{key}={_format_value(value)}"
        for key, value in sorted(components.items())
        if abs(value) > 0.0005
    ]
    return ", ".join(parts) if parts else "–"


def _format_ts(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _simulate_profile(ticks: list[Tick]) -> Simulation:
    """Simuliert Chat-Profil-Fusion und Trigger-Pipeline je Streamer."""
    scored = [(tick, fuse(tick.components, CHAT_PROFILE_WEIGHTS)) for tick in ticks]
    candidates = [(tick, score) for tick, score in scored if score >= PROFILE_THRESHOLD]
    triggers: list[tuple[Tick, float]] = []
    last_trigger: datetime | None = None
    consecutive = 0

    for tick, score in scored:
        if score < PROFILE_THRESHOLD:
            consecutive = 0
            continue
        consecutive += 1
        if consecutive < PROFILE_REQUIRED_TICKS:
            continue
        if last_trigger is not None:
            elapsed = (tick.ts - last_trigger).total_seconds()
            if elapsed < PROFILE_COOLDOWN_S:
                continue
        recent = sum(
            1 for triggered, _ in triggers if (tick.ts - triggered.ts).total_seconds() < 3600
        )
        if recent >= PROFILE_MAX_CLIPS_PER_HOUR:
            continue
        triggers.append((tick, score))
        last_trigger = tick.ts
        consecutive = 0

    return Simulation(candidates=candidates, triggers=triggers)


def _streamer_section(
    login: str,
    ticks: list[Tick],
    hours: int,
    simulate_profile: bool,
) -> list[str]:
    lines = [f"=== Streamer: {login} ===", f"Ticks: {len(ticks)}"]
    legacy_scores = [tick.score for tick in ticks]
    if legacy_scores:
        lines.append(
            "Legacy-Score: "
            f"p50={_format_value(percentile(legacy_scores, 0.5))} "
            f"p95={_format_value(percentile(legacy_scores, 0.95))} "
            f"max={_format_value(max(legacy_scores))}"
        )

    keys = _signal_keys(ticks)
    lines.append("")
    lines.append("Signal-Statistik:")
    lines.append(
        f"  {'Signal':<22} {'Ticks':>5} {'p50':>7} {'p95':>7} {'max':>7} {'>=0.70':>7}"
    )
    for key in keys:
        values = [tick.components[key] for tick in ticks if key in tick.components]
        count = len(values)
        lines.append(
            f"  {key:<22} {count:>5} "
            f"{percentile(values, 0.5):>7.3f} "
            f"{percentile(values, 0.95):>7.3f} "
            f"{percentile(values, 1.0):>7.3f} "
            f"{sum(1 for value in values if value >= 0.7):>7d}"
        )

    lines.append("")
    lines.append(f"Top-Momente (max. {TOP_MOMENTS} je Signal):")
    for key in keys:
        moments = sorted(
            (
                tick
                for tick in ticks
                if key in tick.components and tick.components[key] > 0
            ),
            key=lambda tick: (-tick.components[key], tick.ts, tick.sample_id),
        )[:TOP_MOMENTS]
        lines.append(f"  {key}:")
        if not moments:
            lines.append("    keine Treffer")
            continue
        for tick in moments:
            lines.append(
                f"    - {_format_ts(tick.ts)} UTC · Wert {_format_value(tick.components[key])} "
                f"· Legacy-Score {_format_value(tick.score)} "
                f"· Komponenten: {_format_components(tick.components)}"
            )

    lines.append("")
    lines.append("Details (letzter gespeicherter Aggregatwert je Signal):")
    for key in keys:
        latest: Any = None
        for tick in reversed(ticks):
            if key in tick.detail and tick.detail[key] is not None:
                latest = tick.detail[key]
                break
        rendered = "–" if latest is None else json.dumps(latest, ensure_ascii=False, sort_keys=True)
        lines.append(f"  {key}: {rendered}")

    lines.append("")
    if not simulate_profile:
        lines.append("Chat-Profil-Simulation: nicht aktiv (mit --simulate-profile aktivieren).")
        return lines

    simulation = _simulate_profile(ticks)
    lines.append("Would-trigger-Simulation (Chat-Profil):")
    lines.append(
        f"  Schwelle {PROFILE_THRESHOLD:.2f} · required_ticks {PROFILE_REQUIRED_TICKS} "
        f"· Cooldown {PROFILE_COOLDOWN_S} s · max. {PROFILE_MAX_CLIPS_PER_HOUR}/Stunde"
    )
    weights = ", ".join(f"{key}={value:.2f}" for key, value in CHAT_PROFILE_WEIGHTS.items())
    lines.append(f"  Gewichte: {weights}")
    lines.append(
        f"  Kandidaten (Score >= {PROFILE_THRESHOLD:.2f}): {len(simulation.candidates)}"
    )
    lines.append(f"  Would-trigger: {len(simulation.triggers)} Moment(e)")
    rate = len(simulation.triggers) / hours if hours else 0.0
    lines.append(f"  Erwartete Clip-Rate: {rate:.2f} Clips/Stunde")
    if simulation.triggers:
        lines.append("  Momente:")
        for tick, score in simulation.triggers:
            lines.append(
                f"    - {_format_ts(tick.ts)} UTC · Score {_format_value(score)} "
                f"· Komponenten: {_format_components(tick.components)}"
            )
    return lines


def build_report(
    db_path: str | Path,
    hours: int = 48,
    streamer: str | None = None,
    simulate_profile: bool = False,
) -> str:
    """Erstellt den Shadow-Report als deutschen Text.

    Die Datenbank wird ausschließlich lesend geöffnet. ``streamer`` filtert
    case-insensitiv auf den Twitch-Login; ``simulate_profile`` ergänzt die
    Would-trigger-Simulation mit dem Chat-Profil.
    """
    path = Path(db_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Datenbank nicht gefunden: {path}")

    hours = int(hours)
    if hours <= 0:
        raise ValueError("hours muss größer als 0 sein.")

    now = utcnow()
    cutoff = now - timedelta(hours=hours)
    connection = _connect_read_only(path)
    try:
        ticks = _load_ticks(connection, cutoff)
    finally:
        connection.close()

    needle = streamer.strip().lower() if streamer and streamer.strip() else None
    if needle is not None:
        ticks = [tick for tick in ticks if tick.login.lower() == needle]

    lines = [
        f"Shadow-Report · Zeitraum: letzte {hours} h "
        f"(seit {cutoff:%Y-%m-%d %H:%M:%S} UTC)",
        f"Datenbank: {path}",
    ]
    if needle is not None:
        lines.append(f"Streamer-Filter: {needle}")
    lines.append("")

    if not ticks:
        lines.append("Keine Signal-Samples im Zeitraum gefunden.")
        if simulate_profile:
            lines.append("")
            lines.append("Would-trigger: 0 · Erwartete Clip-Rate: 0.00 Clips/Stunde")
        return "\n".join(lines)

    by_streamer: dict[str, list[Tick]] = {}
    for tick in ticks:
        by_streamer.setdefault(tick.login, []).append(tick)

    lines.append(f"Streamer: {len(by_streamer)} · Ticks: {len(ticks)}")
    for login in sorted(by_streamer):
        lines.append("")
        lines.extend(_streamer_section(login, by_streamer[login], hours, simulate_profile))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Erstellt einen Shadow-Report über gespeicherte Chat-Signale."
    )
    parser.add_argument("--db", required=True, type=Path, help="Pfad zur SQLite-Datenbank")
    parser.add_argument("--hours", type=int, default=48, help="Zeitraum in Stunden (Standard: 48)")
    parser.add_argument("--streamer", default=None, help="Optionaler Twitch-Login")
    parser.add_argument(
        "--simulate-profile",
        action="store_true",
        help="Chat-Profil und Trigger-Pipeline simulieren",
    )
    args = parser.parse_args(argv)

    try:
        report = build_report(
            args.db,
            hours=args.hours,
            streamer=args.streamer,
            simulate_profile=args.simulate_profile,
        )
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
