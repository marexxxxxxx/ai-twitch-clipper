"""Tests für die SQLite-Migration von ``SignalSample.detail``."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db import ensure_signal_detail_column


async def test_existing_sqlite_gets_detail_column(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE signal_sample "
                "(id INTEGER PRIMARY KEY, ts DATETIME, score FLOAT, components JSON)"
            )
        )
    await ensure_signal_detail_column(engine)
    async with engine.connect() as conn:
        columns = (await conn.execute(text("PRAGMA table_info(signal_sample)"))).all()
    assert "detail" in {row[1] for row in columns}
    await engine.dispose()


async def test_migration_is_idempotent(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE signal_sample "
                "(id INTEGER PRIMARY KEY, ts DATETIME, score FLOAT, components JSON)"
            )
        )
    await ensure_signal_detail_column(engine)
    await ensure_signal_detail_column(engine)
    async with engine.connect() as conn:
        columns = (await conn.execute(text("PRAGMA table_info(signal_sample)"))).all()
    assert [row[1] for row in columns].count("detail") == 1
    await engine.dispose()
