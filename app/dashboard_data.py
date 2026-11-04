"""Gemeinsame Dashboard-Daten für API und Server-Rendering."""

from __future__ import annotations

import shutil
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .capacity import capacity_snapshot
from .models import Recording, SignalSample, Streamer
from .runtime_settings import storage_path
from .timeutil import iso, utcnow


async def build_dashboard(
    db: AsyncSession,
    supervisor: Any,
    settings: dict[str, Any],
    twitch_mode: str,
) -> dict[str, Any]:
    streamers = (await db.execute(select(Streamer).order_by(Streamer.login))).scalars().all()

    since = utcnow() - timedelta(minutes=15)
    samples_by_streamer: dict[int, list[dict]] = {}
    if streamers:
        rows = (
            await db.execute(
                select(SignalSample)
                .where(SignalSample.ts >= since)
                .where(SignalSample.streamer_id.in_([row.id for row in streamers]))
                .order_by(SignalSample.ts)
            )
        ).scalars().all()
        for sample in rows:
            samples_by_streamer.setdefault(sample.streamer_id, []).append(
                {"ts": iso(sample.ts), "score": round(sample.score, 3)}
            )

    recent = (
        await db.execute(select(Recording).order_by(Recording.created_at.desc()).limit(10))
    ).scalars().all()

    counts: dict[str, int] = {}
    for status in ("ready", "truncated", "failed", "pending", "assembling"):
        counts[status] = int(
            (
                await db.execute(select(func.count(Recording.id)).where(Recording.status == status))
            ).scalar()
            or 0
        )

    root = storage_path(settings)
    try:
        usage = shutil.disk_usage(str(root))
        disk = {
            "path": str(root),
            "free_gb": round(usage.free / (1024**3), 2),
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
        }
    except OSError as exc:
        disk = {"path": str(root), "error": str(exc)}

    from .api.serializers import recording_dict, streamer_dict

    items = []
    for streamer in streamers:
        runtime = supervisor.snapshot(streamer.id)
        payload = streamer_dict(streamer, runtime)
        payload["sparkline"] = samples_by_streamer.get(streamer.id, [])
        items.append(payload)

    return {
        "streamers": items,
        "recordings": [recording_dict(row) for row in recent],
        "disk": disk,
        "counts": counts,
        "twitch_mode": twitch_mode,
        "capacity": await capacity_snapshot(supervisor, settings),
        "now": iso(utcnow()),
    }
