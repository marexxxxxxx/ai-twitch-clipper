from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import build_engine
from app.models import Base


@pytest.fixture
async def session_factory(tmp_path, monkeypatch):
    """Frische SQLite-DB je Test; app.db-Globals zeigen darauf."""
    engine = build_engine(tmp_path / "test.db")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr("app.db._engine", engine)
    monkeypatch.setattr("app.db._sessionmaker", factory)
    yield factory
    await engine.dispose()
