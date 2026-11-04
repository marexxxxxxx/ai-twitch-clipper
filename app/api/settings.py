"""Einstellungen und Pfadprüfung."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel

from ..runtime_settings import SettingsValidationError
from ..storage import StorageError, check_path

router = APIRouter(tags=["settings"])
logger = logging.getLogger(__name__)


def _check_dict(check) -> dict:
    return {
        "path": str(check.path),
        "exists": check.exists,
        "writable": check.writable,
        "free_bytes": check.free_bytes,
        "free_gb": check.free_gb,
        "total_gb": check.total_gb,
        "message": check.message,
    }


@router.get("/api/settings")
async def get_settings(request: Request) -> dict:
    service = request.app.state.settings_service
    values = await service.get(force=True)
    try:
        check = await service.check(str(values.get("storage_root") or "/data"))
        storage = _check_dict(check)
    except (StorageError, OSError) as exc:
        storage = {"path": str(values.get("storage_root")), "writable": False, "message": str(exc)}
    return {"values": values, "storage": storage}


@router.put("/api/settings")
async def update_settings(request: Request, payload: dict = Body(...)) -> dict:
    service = request.app.state.settings_service
    try:
        values = await service.update(payload)
    except SettingsValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Einstellungen geändert: %s", ", ".join(sorted(payload)))
    return {"values": values}


class PathRequest(BaseModel):
    path: str


@router.post("/api/settings/check-path")
async def check_storage_path(payload: PathRequest) -> dict:
    try:
        check = await check_path(payload.path)
    except StorageError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _check_dict(check)
