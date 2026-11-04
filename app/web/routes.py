"""Server-gerenderte Seiten (deutsch)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.serializers import recording_dict, streamer_dict
from ..capacity import capacity_snapshot
from ..dashboard_data import build_dashboard
from ..db import get_db
from ..detect.weights import CHAT_PROFILE_WEIGHTS
from ..models import LogEntry, Recording, Streamer
from ..timeutil import duration_hms, human_bytes, iso, localtime

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["localtime"] = localtime
templates.env.filters["duration"] = duration_hms
templates.env.filters["bytesize"] = human_bytes
templates.env.filters["score100"] = lambda value: f"{float(value or 0) * 100:.0f}"

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    settings = await request.app.state.settings_service.get()
    data = await build_dashboard(
        db, request.app.state.supervisor, settings, request.app.state.twitch.mode
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"page": "dashboard", "data": data, "settings": settings, "sqlite_path": str(request.app.state.db_path)},
    )


@router.get("/streamers", response_class=HTMLResponse)
async def streamers_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (await db.execute(select(Streamer).order_by(Streamer.login))).scalars().all()
    supervisor = request.app.state.supervisor
    items = []
    for row in rows:
        runtime = supervisor.snapshot(row.id)
        item = streamer_dict(row, runtime)
        item["queue_position"] = (runtime or {}).get("queue_position")
        items.append(item)
    settings = await request.app.state.settings_service.get()
    capacity = await capacity_snapshot(supervisor, settings)
    return templates.TemplateResponse(
        request,
        "streamers.html",
        {"page": "streamers", "streamers": items, "capacity": capacity},
    )


@router.get("/recordings", response_class=HTMLResponse)
async def recordings_page(
    request: Request,
    streamer: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    query = select(Recording)
    if streamer:
        query = query.where(Recording.streamer_login == streamer.lower())
    if status:
        query = query.where(Recording.status == status)
    rows = (await db.execute(query.order_by(Recording.created_at.desc()).limit(300))).scalars().all()
    streamers = (await db.execute(select(Streamer.login).order_by(Streamer.login))).scalars().all()
    items = [recording_dict(row) for row in rows]
    return templates.TemplateResponse(
        request,
        "recordings.html",
        {
            "page": "recordings",
            "recordings": items,
            "streamers": list(streamers),
            "filter_streamer": streamer or "",
            "filter_status": status or "",
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    service = request.app.state.settings_service
    values = await service.get(force=True)
    try:
        storage = await service.check(str(values.get("storage_root") or "/data"))
    except Exception:  # noqa: BLE001
        storage = None
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "page": "settings",
            "values": values,
            "storage": storage,
            "chat_profile": CHAT_PROFILE_WEIGHTS,
        },
    )


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    rows = (
        await db.execute(select(LogEntry).order_by(LogEntry.id.desc()).limit(500))
    ).scalars().all()
    items = [
        {
            "ts": iso(row.ts),
            "level": row.level,
            "streamer_login": row.streamer_login,
            "message": row.message,
        }
        for row in reversed(rows)
    ]
    return templates.TemplateResponse(request, "logs.html", {"page": "logs", "items": items})
