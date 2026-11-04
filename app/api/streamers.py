"""Streamer-Verwaltung und Twitch-Auflösung."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Streamer
from ..twitch.types import TwitchError, TwitchUnavailable, UserNotFound
from .serializers import streamer_dict

router = APIRouter(tags=["streamers"])
logger = logging.getLogger(__name__)

LOGIN_RE = re.compile(r"^[a-z0-9_]{1,25}$")


class StreamerCreate(BaseModel):
    login: str = Field(min_length=1, max_length=64)


class StreamerUpdate(BaseModel):
    enabled: bool | None = None
    display_name: str | None = Field(default=None, max_length=128)


def normalize_login(raw: str) -> str:
    login = (raw or "").strip().lower()
    if login.startswith("#"):
        login = login[1:]
    if not LOGIN_RE.match(login):
        raise HTTPException(
            status_code=400,
            detail="Ungültiger Twitch-Login. Erlaubt sind Buchstaben, Ziffern und Unterstriche (max. 25).",
        )
    return login


@router.get("/api/streamers")
async def list_streamers(request: Request, db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Streamer).order_by(Streamer.login))).scalars().all()
    supervisor = request.app.state.supervisor
    return [streamer_dict(row, supervisor.snapshot(row.id)) for row in rows]


@router.post("/api/streamers", status_code=201)
async def add_streamer(
    payload: StreamerCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    login = normalize_login(payload.login)
    existing = (
        await db.execute(select(Streamer).where(Streamer.login == login))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Streamer '{login}' ist bereits angelegt.")

    twitch = request.app.state.twitch
    try:
        channel = await twitch.fetch_channel(login)
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TwitchUnavailable as exc:
        raise HTTPException(status_code=502, detail=f"Twitch nicht erreichbar: {exc}") from exc
    except TwitchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    streamer = Streamer(
        login=channel.login or login,
        display_name=channel.display_name or login,
        twitch_user_id=channel.id,
        enabled=True,
    )
    db.add(streamer)
    await db.commit()
    await db.refresh(streamer)
    await request.app.state.supervisor.kick()
    logger.info("Streamer angelegt: %s", streamer.login)
    return streamer_dict(streamer, request.app.state.supervisor.snapshot(streamer.id))


@router.patch("/api/streamers/{streamer_id}")
async def update_streamer(
    streamer_id: int,
    payload: StreamerUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    streamer = await db.get(Streamer, streamer_id)
    if streamer is None:
        raise HTTPException(status_code=404, detail="Streamer nicht gefunden.")
    if payload.enabled is not None:
        streamer.enabled = payload.enabled
    if payload.display_name is not None:
        streamer.display_name = payload.display_name
    streamer.last_error = None if payload.enabled else streamer.last_error
    await db.commit()
    await request.app.state.supervisor.kick()
    return streamer_dict(streamer, request.app.state.supervisor.snapshot(streamer.id))


@router.delete("/api/streamers/{streamer_id}")
async def delete_streamer(
    streamer_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    streamer = await db.get(Streamer, streamer_id)
    if streamer is None:
        raise HTTPException(status_code=404, detail="Streamer nicht gefunden.")
    login = streamer.login
    await request.app.state.supervisor.remove_streamer(streamer_id)
    await db.delete(streamer)
    await db.commit()
    logger.info("Streamer entfernt: %s (Aufnahmen bleiben)", login)
    return {"deleted": streamer_id, "login": login}


@router.get("/api/streamers/{login}/status")
async def streamer_status(
    login: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    login = login.strip().lower()
    streamer = (
        await db.execute(select(Streamer).where(Streamer.login == login))
    ).scalar_one_or_none()
    if streamer is None:
        raise HTTPException(status_code=404, detail="Streamer nicht gefunden.")
    return streamer_dict(streamer, request.app.state.supervisor.snapshot(streamer.id))


@router.get("/api/twitch/live")
async def twitch_live(request: Request, limit: int = Query(default=10, ge=1, le=50)) -> dict:
    try:
        channels = await request.app.state.twitch.fetch_top_live(limit)
    except TwitchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "mode": request.app.state.twitch.mode,
        "items": [
            {
                "login": channel.login,
                "display_name": channel.display_name,
                "viewers": channel.viewers,
                "game_name": channel.game_name,
                "started_at": channel.started_at.isoformat() + "Z" if channel.started_at else None,
            }
            for channel in channels
        ],
    }
