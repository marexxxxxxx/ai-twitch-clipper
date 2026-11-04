"""Datenbank-Setup (SQLAlchemy 2 async, SQLite)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import AppConfig, get_config
from .models import Base

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def build_engine(db_path: Path) -> AsyncEngine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        pool_pre_ping=True,
        connect_args={"timeout": 30},
    )
    event.listen(engine.sync_engine, "connect", _configure_sqlite)
    return engine


def init_engine(config: AppConfig | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        config = config or get_config()
        _engine = build_engine(config.db_path)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def create_schema() -> None:
    engine = init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ensure_signal_detail_column(engine: AsyncEngine | None = None) -> None:
    """Ergänzt die ``detail``-Spalte in bestehenden SQLite-Datenbanken."""
    resolved = engine if engine is not None else init_engine()
    async with resolved.begin() as conn:
        columns = (await conn.execute(text("PRAGMA table_info(signal_sample)"))).all()
        if columns and "detail" not in {row[1] for row in columns}:
            await conn.execute(text("ALTER TABLE signal_sample ADD COLUMN detail JSON"))


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async context manager für Hintergrundtasks."""
    async with get_sessionmaker()() as session:
        yield session


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI-Dependency."""
    async with get_sessionmaker()() as session:
        yield session
