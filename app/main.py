"""FastAPI-App, Lifespan (Supervisor, Retention), Mounting."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import dashboard as dashboard_api
from .api import health, recordings, settings as settings_api, streamers
from .clip.assembler import ClipAssembler
from .config import get_config
from .db import create_schema, dispose_engine, ensure_signal_detail_column, get_sessionmaker
from .logging_setup import configure_logging, log_hub
from .retention import RetentionLoop, reconcile_interrupted_recordings
from .runtime_settings import SettingsService, seed_defaults
from .supervisor import Supervisor
from .twitch.client import TwitchClient
from .web import routes as web_routes

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    config = get_config()
    configure_logging(config.log_level)
    logger.info("AI Twitch Clipper startet (DB: %s)", config.db_path)

    await create_schema()
    await ensure_signal_detail_column()
    async with get_sessionmaker()() as session:
        await seed_defaults(session, config)

    log_hub.start()
    settings_service = SettingsService()
    try:
        await reconcile_interrupted_recordings(await settings_service.get(force=True))
    except Exception:  # noqa: BLE001
        logger.exception("Startup-Reconcile fehlgeschlagen")
    twitch = TwitchClient(config)
    supervisor = Supervisor(twitch, settings_service, ClipAssembler(), config)
    retention = RetentionLoop(settings_service)
    retention.active_buffer_dirs = supervisor.active_buffer_dirs

    app.state.config = config
    app.state.db_path = config.db_path
    app.state.settings_service = settings_service
    app.state.twitch = twitch
    app.state.supervisor = supervisor
    app.state.retention = retention

    await supervisor.start()
    retention.start()
    try:
        yield
    finally:
        logger.info("AI Twitch Clipper wird beendet")
        await retention.stop()
        await supervisor.stop()
        await twitch.close()
        await log_hub.stop()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Twitch Clipper",
        version=__version__,
        description=(
            "Selbst gehosteter Dienst, der Twitch-Streamer überwacht, virale Momente "
            "heuristisch erkennt und 5-Minuten-Sequenzen aus einem Ringpuffer schreibt."
        ),
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    app.include_router(health.router)
    app.include_router(streamers.router)
    app.include_router(recordings.router)
    app.include_router(settings_api.router)
    app.include_router(dashboard_api.router)
    app.include_router(web_routes.router)
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    cfg = get_config()
    uvicorn.run("app.main:app", host="0.0.0.0", port=cfg.port, log_config=None, access_log=False)
