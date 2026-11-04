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
from ..twitch.gql import BATCH_CHUNK_SIZE
from ..twitch.types import TwitchError, TwitchUnavailable, UserNotFound
from .serializers import streamer_dict

router = APIRouter(tags=["streamers"])
logger = logging.getLogger(__name__)

LOGIN_RE = re.compile(r"^[a-z0-9_]{1,25}$")
BULK_MAX_ENTRIES = 200
BULK_SPLIT_RE = re.compile(r"[\s,;]+")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


class StreamerCreate(BaseModel):
    login: str = Field(min_length=1, max_length=64)


class BulkStreamerCreate(BaseModel):
    input: str = Field(default="", max_length=65536)


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


def extract_bulk_login(raw: str) -> str | None:
    """Zieht den Twitch-Login aus URL, ``#login``, ``@login`` oder Klartext."""
    text = (raw or "").strip()
    if not text:
        return None

    if text.startswith("#") or text.startswith("@"):
        candidate = text[1:]
    else:
        text = SCHEME_RE.sub("", text)
        text = re.split(r"[?#]", text, maxsplit=1)[0]
        lowered = text.lower()
        if lowered.startswith("www."):
            lowered = lowered[4:]
        if lowered.startswith("twitch.tv"):
            rest = lowered[len("twitch.tv") :]
            if not rest.startswith("/"):
                return None
            candidate = rest.lstrip("/").split("/", maxsplit=1)[0]
        else:
            candidate = text

    candidate = candidate.strip().lower()
    if not LOGIN_RE.match(candidate):
        return None
    return candidate


def parse_bulk_input(raw: str) -> list[tuple[str, str | None]]:
    """Zerlegt Freitext in ``(Originaleintrag, Login|None)``, dedupliziert."""
    entries: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for token in BULK_SPLIT_RE.split(raw or ""):
        original = token.strip()
        if not original:
            continue
        login = extract_bulk_login(original)
        key = login if login is not None else f"!{original.lower()}"
        if key in seen:
            continue
        seen.add(key)
        entries.append((original, login))
    return entries


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


@router.post("/api/streamers/bulk")
async def add_streamers_bulk(
    payload: BulkStreamerCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Importiert mehrere Kanäle aus Freitext (URL, ``#login``, ``@login``, Login)."""
    entries = parse_bulk_input(payload.input)
    if len(entries) > BULK_MAX_ENTRIES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Zu viele Einträge ({len(entries)}). Pro Import sind maximal "
                f"{BULK_MAX_ENTRIES} Kanäle erlaubt."
            ),
        )

    counts = {"added": 0, "duplicate": 0, "not_found": 0, "invalid": 0}
    valid_logins = [login for _original, login in entries if login is not None]

    existing: set[str] = set()
    if valid_logins:
        rows = (
            await db.execute(select(Streamer.login).where(Streamer.login.in_(valid_logins)))
        ).scalars().all()
        existing = {str(row).lower() for row in rows}

    lookup = [login for login in valid_logins if login not in existing]
    channels = {}
    if lookup:
        twitch = request.app.state.twitch
        try:
            for start in range(0, len(lookup), BATCH_CHUNK_SIZE):
                chunk = lookup[start : start + BATCH_CHUNK_SIZE]
                channels.update(await twitch.fetch_channels_batch(chunk))
        except TwitchError as exc:
            raise HTTPException(
                status_code=502, detail=f"Twitch nicht erreichbar: {exc}"
            ) from exc

    items: list[dict] = []
    to_add: list[Streamer] = []
    for original, login in entries:
        if login is None:
            status = "invalid"
            detail = "Kein gültiger Twitch-Login."
        elif login in existing:
            status = "duplicate"
            detail = "Bereits vorhanden."
        else:
            channel = channels.get(login)
            if channel is None:
                status = "not_found"
                detail = "Kanal nicht gefunden."
            else:
                status = "added"
                detail = "Hinzugefügt."
                to_add.append(
                    Streamer(
                        login=channel.login or login,
                        display_name=channel.display_name or login,
                        twitch_user_id=channel.id,
                        enabled=True,
                    )
                )
        counts[status] += 1
        items.append(
            {"input": original, "login": login, "status": status, "detail": detail}
        )

    if to_add:
        db.add_all(to_add)
        await db.commit()
        await request.app.state.supervisor.kick()
        logger.info(
            "Massen-Import: %d hinzugefügt, %d bereits vorhanden, %d unbekannt, %d ungültig",
            counts["added"],
            counts["duplicate"],
            counts["not_found"],
            counts["invalid"],
        )

    return {"counts": counts, "items": items}


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
