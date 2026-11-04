from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import AppConfig
from app.db import build_engine
from app.models import Base, Streamer
from app.runtime_settings import default_values
from app.web import routes as web_routes


class FakeSupervisor:
    def __init__(self) -> None:
        self.states = {}
        self.enabled_logins = ["alpha", "beta"]
        self.total_streamers = 2
        self.gql_requests_last_cycle = 1
        self._snapshots = {
            2: {"state": "queued", "queue_position": 0, "last_error": None},
        }

    def snapshot(self, streamer_id: int):
        return self._snapshots.get(streamer_id)


def test_streamer_dict_exposes_queue_position() -> None:
    from app.api.serializers import streamer_dict

    streamer = SimpleNamespace(
        id=1,
        login="alpha",
        display_name="Alpha",
        twitch_user_id=None,
        enabled=True,
        created_at=None,
        last_live_at=None,
        last_error=None,
    )
    payload = streamer_dict(streamer, {"state": "queued", "queue_position": 2})

    assert payload["queue_position"] == 2


class FakeSettingsService:
    def __init__(self, values: dict) -> None:
        self.values = values

    async def get(self, force: bool = False) -> dict:
        return dict(self.values)

    async def check(self, path: str):
        return SimpleNamespace(
            path=path,
            exists=True,
            writable=True,
            free_bytes=1024**3,
            free_gb=1.0,
            total_gb=2.0,
            message="Pfad ist beschreibbar.",
        )


def prepare_db(tmp_path, monkeypatch):
    engine = build_engine(tmp_path / "web.db")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as session:
            session.add(Streamer(login="alpha", display_name="Alpha", enabled=True))
            session.add(Streamer(login="beta", display_name="Beta", enabled=True))
            await session.commit()

    asyncio.run(setup())
    monkeypatch.setattr("app.db._engine", engine)
    monkeypatch.setattr("app.db._sessionmaker", factory)
    return engine


def test_settings_page_has_chat_controls(tmp_path, monkeypatch) -> None:
    import json
    import re

    from app.detect.weights import CHAT_PROFILE_WEIGHTS

    engine = prepare_db(tmp_path, monkeypatch)
    values = default_values(AppConfig(storage_root=tmp_path, data_dir=tmp_path))
    app = FastAPI()
    app.include_router(web_routes.router)
    app.state.supervisor = FakeSupervisor()
    app.state.settings_service = FakeSettingsService(values)
    app.state.twitch = SimpleNamespace(mode="anonymous-gql")
    app.state.db_path = tmp_path / "web.db"

    try:
        with TestClient(app) as client:
            page = client.get("/settings").text
    finally:
        asyncio.run(engine.dispose())

    for needle in (
        "required_ticks",
        "max_clips_per_hour",
        "shadow_mode",
        "streamer_command_triggers",
        "hype_keywords",
        "copypasta_min_users",
        "copypasta_window_s",
        "weight_unique_chatters",
        "apply-chat-profile",
    ):
        assert needle in page
    for key in CHAT_PROFILE_WEIGHTS:
        assert f'name="weight_{key}"' in page
    match = re.search(r"data-chat-profile='([^']*)'", page)
    assert match is not None
    assert json.loads(match.group(1)) == CHAT_PROFILE_WEIGHTS


def test_pages_render_with_queue_and_bulk_panel(tmp_path, monkeypatch) -> None:
    engine = prepare_db(tmp_path, monkeypatch)
    values = default_values(AppConfig(storage_root=tmp_path, data_dir=tmp_path))
    app = FastAPI()
    app.include_router(web_routes.router)
    app.state.supervisor = FakeSupervisor()
    app.state.settings_service = FakeSettingsService(values)
    app.state.twitch = SimpleNamespace(mode="anonymous-gql")
    app.state.db_path = tmp_path / "web.db"

    try:
        with TestClient(app) as client:
            for path in ("/streamers", "/", "/settings"):
                response = client.get(path)
                assert response.status_code == 200, path

            streamers_html = client.get("/streamers").text
            assert "Mehrere Kanäle importieren" in streamers_html
            assert "WARTESCHLANGE" in streamers_html
            assert "Platz 1" in streamers_html
            assert "aktiv" in streamers_html and "in Warteschlange" in streamers_html

            dashboard_html = client.get("/").text
            assert "Kapazität" in dashboard_html
            assert "Twitch-Anfragen" in dashboard_html

            settings_html = client.get("/settings").text
            assert 'name="max_concurrent_streams"' in settings_html
    finally:
        asyncio.run(engine.dispose())


def test_streamers_page_has_manual_clip_button(tmp_path, monkeypatch) -> None:
    engine = prepare_db(tmp_path, monkeypatch)
    values = default_values(AppConfig(storage_root=tmp_path, data_dir=tmp_path))
    app = FastAPI()
    app.include_router(web_routes.router)
    app.state.supervisor = FakeSupervisor()
    app.state.settings_service = FakeSettingsService(values)
    app.state.twitch = SimpleNamespace(mode="anonymous-gql")
    app.state.db_path = tmp_path / "web.db"

    try:
        with TestClient(app) as client:
            page = client.get("/streamers").text
    finally:
        asyncio.run(engine.dispose())

    assert "js-clip" in page
    assert "Jetzt clippen" in page
