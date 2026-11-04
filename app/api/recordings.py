"""Aufnahmen: Liste, Dateien, Sidecar, Trigger, Löschen."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import Recording
from ..retention import delete_recording_files
from ..supervisor import TriggerError
from .serializers import recording_dict

router = APIRouter(tags=["recordings"])
logger = logging.getLogger(__name__)


class TriggerRequest(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=255)


@router.get("/api/recordings")
async def list_recordings(
    streamer: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    query = select(Recording)
    count_query = select(func.count(Recording.id))
    if streamer:
        query = query.where(Recording.streamer_login == streamer.lower())
        count_query = count_query.where(Recording.streamer_login == streamer.lower())
    if status:
        query = query.where(Recording.status == status)
        count_query = count_query.where(Recording.status == status)
    total = int((await db.execute(count_query)).scalar() or 0)
    rows = (
        await db.execute(
            query.order_by(Recording.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()
    return {"items": [recording_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/api/recordings/{recording_id}")
async def get_recording(recording_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    recording = await db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Aufnahme nicht gefunden.")
    return recording_dict(recording)


@router.get("/api/recordings/{recording_id}/file")
async def download_recording(recording_id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    recording = await db.get(Recording, recording_id)
    if recording is None or not recording.path:
        raise HTTPException(status_code=404, detail="Aufnahme nicht gefunden.")
    path = Path(recording.path)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Datei fehlt auf der Festplatte.")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=recording.filename or path.name,
        content_disposition_type="inline",
    )


@router.get("/api/recordings/{recording_id}/thumb")
async def recording_thumb(recording_id: int, db: AsyncSession = Depends(get_db)) -> FileResponse:
    recording = await db.get(Recording, recording_id)
    if recording is None or not recording.thumbnail_path:
        raise HTTPException(status_code=404, detail="Kein Thumbnail vorhanden.")
    path = Path(recording.thumbnail_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail fehlt auf der Festplatte.")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/api/recordings/{recording_id}/sidecar")
async def recording_sidecar(recording_id: int, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    recording = await db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Aufnahme nicht gefunden.")
    sidecar = Path(recording.path).with_suffix(".json") if recording.path else None
    if sidecar is not None and sidecar.exists():
        try:
            return JSONResponse(json.loads(sidecar.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning("Sidecar %s nicht lesbar: %s", sidecar, exc)
    return JSONResponse(
        {
            "recording_id": recording.id,
            "streamer": recording.streamer_login,
            "score": recording.score,
            "trigger": recording.trigger,
            "status": recording.status,
            "error": recording.error,
        }
    )


@router.delete("/api/recordings/{recording_id}")
async def delete_recording(
    recording_id: int,
    delete_files: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
) -> dict:
    recording = await db.get(Recording, recording_id)
    if recording is None:
        raise HTTPException(status_code=404, detail="Aufnahme nicht gefunden.")
    if recording.status in ("pending", "assembling"):
        raise HTTPException(status_code=409, detail="Aufnahme wird gerade erzeugt.")
    if delete_files:
        delete_recording_files(recording)
    await db.delete(recording)
    await db.commit()
    return {"deleted": recording_id, "files_deleted": delete_files}


@router.post("/api/trigger", status_code=202)
async def trigger_recording(payload: TriggerRequest, request: Request) -> dict:
    try:
        recording_id = await request.app.state.supervisor.trigger(
            payload.login.strip().lower(), payload.reason
        )
    except TriggerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"recording_id": recording_id, "status": "pending"}
