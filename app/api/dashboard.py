"""Dashboard, Logs, Statistik."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..dashboard_data import build_dashboard
from ..db import get_db
from ..logging_setup import log_hub
from ..models import LogEntry
from ..timeutil import iso

router = APIRouter(tags=["dashboard"])


@router.get("/api/dashboard")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    settings = await request.app.state.settings_service.get()
    return await build_dashboard(db, request.app.state.supervisor, settings, request.app.state.twitch.mode)


@router.get("/api/logs")
async def logs(
    limit: int = Query(default=500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = (
        await db.execute(select(LogEntry).order_by(LogEntry.id.desc()).limit(limit))
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
    if not items:
        items = log_hub.recent(limit)
    return {"items": items}
