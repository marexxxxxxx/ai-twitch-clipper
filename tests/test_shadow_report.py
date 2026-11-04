"""Tests für den Shadow-Report über gespeicherte Chat-Signale."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.timeutil import utcnow
from app.tools.shadow_report import build_report, main

ALL_COMPONENTS = {
    "chat_rate": 1.0,
    "unique_chatters": 1.0,
    "emote_burst": 1.0,
    "hype_keywords": 1.0,
    "copypasta": 1.0,
    "clip_commands": 1.0,
    "streamer_command": 1.0,
    "clip_created": 1.0,
    "viewers": 1.0,
    "clip_burst": 1.0,
}


def _init_db(db: Path, *, with_streamer: bool = True, with_detail: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE signal_sample ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "streamer_id INTEGER, "
        "ts DATETIME NOT NULL, "
        "score FLOAT NOT NULL, "
        "components JSON)"
    )
    if with_detail:
        conn.execute("ALTER TABLE signal_sample ADD COLUMN detail JSON")
    if with_streamer:
        conn.execute("CREATE TABLE streamer (id INTEGER PRIMARY KEY, login TEXT NOT NULL)")
    conn.commit()
    return conn


def _insert(
    conn: sqlite3.Connection,
    login: str,
    ts: datetime,
    components: dict[str, float],
    *,
    streamer_id: int = 1,
    score: float = 0.0,
    detail: dict | None = None,
    with_streamer: bool = True,
) -> None:
    if with_streamer:
        conn.execute("CREATE TABLE IF NOT EXISTS streamer (id INTEGER PRIMARY KEY, login TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO streamer (id, login) VALUES (?, ?)", (streamer_id, login))
    has_detail = "detail" in {
        row[1] for row in conn.execute("PRAGMA table_info(signal_sample)")
    }
    if has_detail:
        conn.execute(
            "INSERT INTO signal_sample (streamer_id, ts, score, components, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                streamer_id,
                ts.strftime("%Y-%m-%d %H:%M:%S.%f"),
                score,
                json.dumps(components),
                json.dumps(detail) if detail is not None else None,
            ),
        )
    else:
        conn.execute(
            "INSERT INTO signal_sample (streamer_id, ts, score, components) VALUES (?, ?, ?, ?)",
            (
                streamer_id,
                ts.strftime("%Y-%m-%d %H:%M:%S.%f"),
                score,
                json.dumps(components),
            ),
        )
    conn.commit()


def test_shadow_report_contains_signals_and_simulation(tmp_path):
    db = tmp_path / "clipper.db"
    conn = _init_db(db)
    _insert(
        conn,
        "alice",
        utcnow() - timedelta(hours=1),
        dict(ALL_COMPONENTS),
        score=0.31,
        detail={"unique_chatters": {"value": 5, "median": 2, "samples": 11}},
    )
    conn.close()

    report = build_report(db, hours=48, streamer=None, simulate_profile=True)

    assert "unique_chatters" in report
    assert "would-trigger" in report.lower()
    assert "Would-trigger: 1" in report


def test_report_contains_percentiles_threshold_count_top_moments_and_detail(tmp_path):
    db = tmp_path / "clipper.db"
    conn = _init_db(db)
    base = utcnow() - timedelta(hours=5)
    values = [0.05, 0.20, 0.45, 0.80, 1.00]
    for index, value in enumerate(values):
        _insert(
            conn,
            "alice",
            base + timedelta(minutes=index),
            {"unique_chatters": value},
            score=round(value / 2, 3),
            detail={"unique_chatters": {"value": index, "median": 1, "samples": 12}},
        )
    conn.close()

    report = build_report(db, hours=48)

    assert "p50" in report
    assert "p95" in report
    assert "max" in report
    # 5 Werte: p50=0.450, p95=1.000, max=1.000, zwei Ticks >= 0.70.
    assert re.search(r"unique_chatters\s+5\s+0\.450\s+1\.000\s+1\.000\s+2", report)
    top_timestamps = [
        (base + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S") for index in (4, 3, 2)
    ]
    for timestamp in top_timestamps:
        assert f"{timestamp} UTC" in report
    assert f"{base:%Y-%m-%d %H:%M:%S} UTC" not in report
    assert '"median": 1' in report
    assert '"samples": 12' in report


def test_simulation_respects_cooldown_and_hourly_limit(tmp_path):
    db = tmp_path / "clipper.db"
    conn = _init_db(db)
    base = utcnow() - timedelta(minutes=40)
    for index in range(25):
        _insert(
            conn,
            "alice",
            base + timedelta(minutes=index),
            dict(ALL_COMPONENTS),
            score=1.0,
        )
    conn.close()

    report = build_report(db, hours=1, streamer=None, simulate_profile=True)

    assert "Kandidaten (Score >= 0.40): 25" in report
    # Cooldown 180 s erlaubt 6 Trigger in 24 Minuten; das Stundenlimit stoppt danach.
    assert "Would-trigger: 6" in report
    assert "Erwartete Clip-Rate: 6.00 Clips/Stunde" in report


def test_streamer_filter_is_case_insensitive_and_excludes_others(tmp_path):
    db = tmp_path / "clipper.db"
    conn = _init_db(db)
    now = utcnow() - timedelta(hours=1)
    _insert(conn, "alice", now, dict(ALL_COMPONENTS), streamer_id=1)
    _insert(conn, "bob", now, dict(ALL_COMPONENTS), streamer_id=2)
    conn.close()

    report = build_report(db, hours=48, streamer="ALICE")

    assert "Streamer: alice" in report
    assert "Streamer: bob" not in report


def test_report_handles_old_schema_without_detail_or_streamer_table(tmp_path):
    db = tmp_path / "clipper.db"
    conn = _init_db(db, with_streamer=False, with_detail=False)
    _insert(
        conn,
        "alice",
        utcnow() - timedelta(hours=1),
        {"unique_chatters": 1.0},
        with_streamer=False,
    )
    conn.close()

    report = build_report(db, hours=48)

    assert "unique_chatters" in report
    assert "Streamer #1" in report
    assert "Details (letzter gespeicherter Aggregatwert je Signal)" in report


def test_report_accepts_minimal_signal_sample_schema(tmp_path):
    db = tmp_path / "clipper.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE signal_sample (ts TEXT, score REAL, components JSON)")
    conn.execute(
        "INSERT INTO signal_sample VALUES (?, ?, ?)",
        (
            (utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            0.2,
            json.dumps({"unique_chatters": 1.0}),
        ),
    )
    conn.commit()
    conn.close()

    report = build_report(db, hours=48, simulate_profile=True)

    assert "unique_chatters" in report
    assert "Would-trigger: 0" in report


def test_build_report_missing_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_report(tmp_path / "missing.db")


def test_cli_prints_report(tmp_path, capsys):
    db = tmp_path / "clipper.db"
    conn = _init_db(db)
    _insert(
        conn,
        "alice",
        utcnow() - timedelta(hours=1),
        dict(ALL_COMPONENTS),
        detail={"unique_chatters": {"value": 5, "median": 2, "samples": 11}},
    )
    conn.close()

    assert main(["--db", str(db), "--hours", "48", "--simulate-profile"]) == 0

    output = capsys.readouterr().out
    assert "unique_chatters" in output
    assert "would-trigger" in output.lower()


def test_cli_reports_missing_database(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "missing.db")]) == 1
    assert "Fehler:" in capsys.readouterr().err
