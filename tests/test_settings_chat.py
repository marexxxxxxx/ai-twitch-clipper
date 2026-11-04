from __future__ import annotations

from app.config import AppConfig
from app.runtime_settings import default_values


def test_fresh_defaults_enable_chat_tracking(tmp_path):
    values = default_values(AppConfig(storage_root=tmp_path, data_dir=tmp_path))
    assert values["threshold"] == 0.40
    assert values["required_ticks"] == 1
    assert values["max_clips_per_hour"] == 6
    assert values["shadow_mode"] is True
    assert values["streamer_command_triggers"] is True
    assert values["copypasta_min_users"] == 3
    assert values["copypasta_window_s"] == 10


async def test_existing_install_keeps_threshold_and_weights(tmp_path):
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.config import AppConfig
    from app.db import build_engine
    from app.models import Base, Setting
    from app.runtime_settings import read_all, seed_defaults
    from app.timeutil import utcnow

    config = AppConfig(storage_root=tmp_path, data_dir=tmp_path)
    engine = build_engine(tmp_path / "settings.db")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add_all(
            [
                Setting(key="threshold", value=0.53, updated_at=utcnow()),
                Setting(
                    key="weights",
                    value={"chat_rate": 0.53, "emote_burst": 0.51},
                    updated_at=utcnow(),
                ),
            ]
        )
        await session.commit()
        await seed_defaults(session, config)
        values = await read_all(session, config)
    assert values["threshold"] == 0.53
    assert values["weights"]["chat_rate"] == 0.53
    assert values["required_ticks"] == 2
    assert values["max_clips_per_hour"] == 60
    assert values["shadow_mode"] is True
    await engine.dispose()


async def test_new_settings_are_validated():
    import pytest

    from app.runtime_settings import SettingsValidationError, validate_updates

    clean = await validate_updates(
        {
            "required_ticks": 2,
            "max_clips_per_hour": 12,
            "shadow_mode": False,
            "streamer_command_triggers": False,
            "hype_keywords": "geil, lol",
            "copypasta_min_users": 4,
            "copypasta_window_s": 20,
        }
    )
    assert clean["required_ticks"] == 2
    assert clean["max_clips_per_hour"] == 12
    assert clean["shadow_mode"] is False
    assert clean["streamer_command_triggers"] is False
    assert clean["hype_keywords"] == "geil, lol"
    assert clean["copypasta_min_users"] == 4
    assert clean["copypasta_window_s"] == 20
    for payload in (
        {"required_ticks": 0},
        {"max_clips_per_hour": 0},
        {"copypasta_min_users": 1},
        {"copypasta_window_s": 4},
        {"hype_keywords": "x" * 501},
        {"shadow_mode": "yes"},
    ):
        with pytest.raises(SettingsValidationError):
            await validate_updates(payload)


async def test_weights_update_fills_new_keys():
    from app.runtime_settings import validate_updates

    clean = await validate_updates({"weights": {"chat_rate": 1.0}})
    assert clean["weights"]["unique_chatters"] == 0.0
    assert clean["weights"]["clip_created"] == 0.30


async def test_existing_install_without_threshold_gets_old_default(tmp_path):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db import build_engine
    from app.models import Base, Setting
    from app.runtime_settings import read_all, seed_defaults
    from app.timeutil import utcnow

    config = AppConfig(storage_root=tmp_path, data_dir=tmp_path)
    engine = build_engine(tmp_path / "legacy.db")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Setting(key="cooldown_s", value=180, updated_at=utcnow()))
        await session.commit()
        await seed_defaults(session, config)
        values = await read_all(session, config)
    assert values["threshold"] == 0.60
    assert values["required_ticks"] == 2
    assert values["max_clips_per_hour"] == 60
    await engine.dispose()


async def test_reseed_preserves_existing_chat_settings(tmp_path):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.db import build_engine
    from app.models import Base, Setting
    from app.runtime_settings import read_all, seed_defaults

    config = AppConfig(storage_root=tmp_path, data_dir=tmp_path)
    engine = build_engine(tmp_path / "reseed.db")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        await seed_defaults(session, config)
        rows = {
            row.key: row for row in (await session.execute(select(Setting))).scalars()
        }
        rows["required_ticks"].value = 4
        rows["shadow_mode"].value = False
        await session.commit()
        await seed_defaults(session, config)
        values = await read_all(session, config)
    assert values["required_ticks"] == 4
    assert values["shadow_mode"] is False
    await engine.dispose()
