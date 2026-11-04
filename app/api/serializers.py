"""Serialisierung für API und UI."""

from __future__ import annotations

from typing import Any

from ..detect.detector import COMPONENT_LABELS
from ..models import Recording, Streamer
from ..timeutil import iso, utcnow

DETAIL_LABELS = {
    "value": "Wert",
    "median": "Median",
    "samples": "Stichproben",
    "clips": "Clips",
    "clip_id": "Clip-ID",
    "anchor": "Anker",
    "delta": "Änderung",
}


def _detail_text(detail: Any) -> str:
    if not isinstance(detail, dict):
        return ""
    parts: list[str] = []
    for key, value in detail.items():
        if value is None:
            continue
        if isinstance(value, float):
            value = f"{value:.2f}"
        parts.append(f"{DETAIL_LABELS.get(key, key)} {value}")
    return ", ".join(parts)


def score_breakdown(trigger: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Signal, Rohwert/Detail, Gewicht und Beitrag für die UI-Tabelle."""
    trigger = trigger or {}
    components = trigger.get("components") or {}
    weights = trigger.get("weights") or {}
    details = trigger.get("components_detail") or {}
    total = sum(max(0.0, float(value or 0)) for value in weights.values()) or 1.0

    rows: list[dict[str, Any]] = []
    for key, raw in components.items():
        try:
            value = max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            value = 0.0
        try:
            weight = max(0.0, float(weights.get(key) or 0.0))
        except (TypeError, ValueError):
            weight = 0.0
        rows.append(
            {
                "key": key,
                "label": COMPONENT_LABELS.get(key, key),
                "value": round(value, 4),
                "weight": round(weight, 4),
                "contribution": round(weight * value / total, 4),
                "detail": _detail_text(details.get(key)),
            }
        )
    rows.sort(key=lambda row: row["contribution"], reverse=True)
    return rows


def streamer_dict(streamer: Streamer, runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime = runtime or {}
    uptime = None
    if runtime.get("session_started_at"):
        try:
            from ..timeutil import parse_iso

            started = parse_iso(runtime["session_started_at"])
            if started:
                uptime = max(0.0, (utcnow() - started).total_seconds())
        except Exception:  # noqa: BLE001
            uptime = None
    return {
        "id": streamer.id,
        "login": streamer.login,
        "display_name": streamer.display_name,
        "twitch_user_id": streamer.twitch_user_id,
        "enabled": streamer.enabled,
        "created_at": iso(streamer.created_at),
        "last_live_at": iso(streamer.last_live_at),
        "last_error": runtime.get("last_error") or streamer.last_error,
        "state": runtime.get("state", "offline"),
        "live": runtime.get("state") == "live",
        "viewers": runtime.get("viewers", 0),
        "uptime_s": uptime,
        "buffer_s": runtime.get("buffer_s", 0.0),
        "last_score": runtime.get("last_score", 0.0),
        "last_components": runtime.get("last_components", {}),
        "last_detail": runtime.get("last_detail", {}),
        "chat_connected": runtime.get("chat_connected", False),
        "cooldown_remaining_s": runtime.get("cooldown_remaining_s", 0.0),
        "stream_id": runtime.get("stream_id"),
        "recording_id": runtime.get("recording_id"),
    }


def recording_dict(recording: Recording, include_trigger: bool = True) -> dict[str, Any]:
    item = {
        "id": recording.id,
        "streamer_id": recording.streamer_id,
        "streamer_login": recording.streamer_login,
        "path": recording.path,
        "filename": recording.filename,
        "started_at": iso(recording.started_at),
        "ended_at": iso(recording.ended_at),
        "duration_s": recording.duration_s,
        "score": recording.score,
        "status": recording.status,
        "size_bytes": recording.size_bytes,
        "thumbnail_path": recording.thumbnail_path,
        "error": recording.error,
        "created_at": iso(recording.created_at),
        "file_url": f"/api/recordings/{recording.id}/file" if recording.path else None,
        "thumbnail_url": f"/api/recordings/{recording.id}/thumb" if recording.thumbnail_path else None,
        "sidecar_url": f"/api/recordings/{recording.id}/sidecar",
    }
    if include_trigger:
        item["trigger"] = recording.trigger
        item["score_breakdown"] = score_breakdown(recording.trigger)
    return item
