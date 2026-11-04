"""Health- und Kapazitäts-Endpunkte."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..capacity import capacity_snapshot

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/capacity")
async def capacity(request: Request) -> dict:
    settings = await request.app.state.settings_service.get()
    return await capacity_snapshot(request.app.state.supervisor, settings)
