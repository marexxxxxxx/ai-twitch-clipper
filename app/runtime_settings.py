"""Laufzeit-Einstellungen (Tabelle ``setting``) inklusive Validierung."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AppConfig, get_config
from .db import get_sessionmaker
from .models import Setting
from .storage import PathCheck, StorageError, check_path
from .timeutil import utcnow

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS: dict[str, float] = {
    "chat_rate": 0.35,
    "emote_burst": 0.20,
    "clip_created": 0.30,
    "viewers": 0.15,
    "clip_burst": 0.10,
}

#: Streamlink-Qualitätsnamen: ``720p60,best``, ``audio_only``, ``worst``.
QUALITY_PATTERN = re.compile(r"^[a-z0-9_]+(,[a-z0-9_]+)*$")

PRE_ROLL_MIN = 30
PRE_ROLL_MAX = 600
POST_ROLL_MIN = 10
POST_ROLL_MAX = 300
#: Das Trigger-Fenster muss vollständig in den Ringpuffer passen.
WINDOW_MAX_S = 360

#: Schlüssel, die im UI/API geändert werden dürfen.
EDITABLE_KEYS = {
    "storage_root",
    "pre_roll_s",
    "post_roll_s",
    "threshold",
    "cooldown_s",
    "quality",
    "retention_days",
    "max_gb_per_streamer",
    "weights",
    "audio_enabled",
}


class SettingsValidationError(ValueError):
    """Ungültige Eingabe; die Meldung ist für die UI gedacht."""


def default_values(config: AppConfig | None = None) -> dict[str, Any]:
    config = config or get_config()
    return {
        "storage_root": str(config.storage_root),
        "pre_roll_s": 240,
        "post_roll_s": 60,
        "threshold": 0.60,
        "cooldown_s": 180,
        "quality": config.quality or "720p60,best",
        "retention_days": 14,
        "max_gb_per_streamer": 50,
        "weights": dict(DEFAULT_WEIGHTS),
        "audio_enabled": False,
    }


async def seed_defaults(session: AsyncSession, config: AppConfig | None = None) -> None:
    existing = {row.key for row in (await session.execute(select(Setting))).scalars()}
    for key, value in default_values(config).items():
        if key not in existing:
            session.add(Setting(key=key, value=value, updated_at=utcnow()))
    await session.commit()


async def read_all(session: AsyncSession, config: AppConfig | None = None) -> dict[str, Any]:
    values = default_values(config)
    rows = (await session.execute(select(Setting))).scalars().all()
    for row in rows:
        values[row.key] = row.value
    return values


def _as_int(value: Any, name: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(f"{name} muss eine ganze Zahl sein.") from exc
    if not low <= number <= high:
        raise SettingsValidationError(f"{name} muss zwischen {low} und {high} liegen.")
    return number


def _as_float(value: Any, name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(f"{name} muss eine Zahl sein.") from exc
    if not low <= number <= high:
        raise SettingsValidationError(f"{name} muss zwischen {low} und {high} liegen.")
    return number


async def validate_updates(
    payload: dict[str, Any], current: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Prüft und normalisiert ein Teil-Update. Wirft ``SettingsValidationError``.

    ``current`` liefert die bereits gespeicherten Werte, damit die gemeinsame
    Puffer-Grenze auch bei Teil-Updates geprüft werden kann. Der Speicherort
    wird nicht nur syntaktisch geprüft, sondern real beschrieben (Temp-Datei).
    Das entspricht Abnahmekriterium 7.
    """
    if not isinstance(payload, dict):
        raise SettingsValidationError("Ungültige Einstellungen.")
    unknown = set(payload) - EDITABLE_KEYS
    if unknown:
        raise SettingsValidationError(f"Unbekannte Einstellung: {', '.join(sorted(unknown))}")

    clean: dict[str, Any] = {}
    if "storage_root" in payload:
        raw = str(payload["storage_root"] or "").strip()
        if not raw:
            raise SettingsValidationError("Der Speicherort darf nicht leer sein.")
        try:
            check = await check_path(raw)
        except StorageError as exc:
            raise SettingsValidationError(str(exc)) from exc
        if not check.writable:
            raise SettingsValidationError(f"Speicherort ist nicht beschreibbar: {check.message}")
        clean["storage_root"] = str(check.path)
    if "pre_roll_s" in payload or "post_roll_s" in payload:
        known = current or {}
        pre = _as_int(
            payload["pre_roll_s"] if "pre_roll_s" in payload else known.get("pre_roll_s", 240),
            "Vorlauf",
            PRE_ROLL_MIN,
            PRE_ROLL_MAX,
        )
        post = _as_int(
            payload["post_roll_s"] if "post_roll_s" in payload else known.get("post_roll_s", 60),
            "Nachlauf",
            POST_ROLL_MIN,
            POST_ROLL_MAX,
        )
        if pre + post > WINDOW_MAX_S:
            raise SettingsValidationError(
                "Vorlauf und Nachlauf dürfen zusammen höchstens "
                f"{WINDOW_MAX_S} s (Ringpuffer) betragen."
            )
        if "pre_roll_s" in payload:
            clean["pre_roll_s"] = pre
        if "post_roll_s" in payload:
            clean["post_roll_s"] = post
    if "threshold" in payload:
        clean["threshold"] = _as_float(payload["threshold"], "Schwelle", 0.05, 1.0)
    if "cooldown_s" in payload:
        clean["cooldown_s"] = _as_int(payload["cooldown_s"], "Cooldown", 0, 7200)
    if "retention_days" in payload:
        clean["retention_days"] = _as_int(payload["retention_days"], "Aufbewahrung", 1, 3650)
    if "max_gb_per_streamer" in payload:
        clean["max_gb_per_streamer"] = _as_int(
            payload["max_gb_per_streamer"], "Maximalgröße je Streamer", 1, 100000
        )
    if "quality" in payload:
        quality = str(payload["quality"] or "").strip()
        if not quality or len(quality) > 120 or not QUALITY_PATTERN.fullmatch(quality):
            raise SettingsValidationError(
                "Ungültige Qualitätsangabe. Erlaubt sind nur kommaseparierte "
                "Streamlink-Qualitätsnamen wie 720p60,best, audio_only oder worst."
            )
        clean["quality"] = quality
    if "audio_enabled" in payload:
        clean["audio_enabled"] = bool(payload["audio_enabled"])
    if "weights" in payload:
        raw_weights = payload["weights"]
        if not isinstance(raw_weights, dict):
            raise SettingsValidationError("Gewichte müssen als Objekt gesendet werden.")
        weights: dict[str, float] = {}
        for key in DEFAULT_WEIGHTS:
            raw = raw_weights.get(key, DEFAULT_WEIGHTS[key])
            weights[key] = _as_float(raw, f"Gewicht {key}", 0.0, 1.0)
        if sum(weights.values()) <= 0:
            raise SettingsValidationError("Mindestens ein Gewicht muss größer als 0 sein.")
        clean["weights"] = weights
    return clean


class SettingsService:
    """Kleiner Cache, damit 5-s-Ticks nicht bei jedem Tick die DB lesen."""

    def __init__(self, ttl: float = 2.0) -> None:
        self._ttl = ttl
        self._value: dict[str, Any] | None = None
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._value is not None and now - self._loaded_at < self._ttl:
            return self._value
        async with self._lock:
            if not force and self._value is not None and time.monotonic() - self._loaded_at < self._ttl:
                return self._value
            async with get_sessionmaker()() as session:
                self._value = await read_all(session)
            self._loaded_at = time.monotonic()
            return self._value

    def invalidate(self) -> None:
        self._value = None
        self._loaded_at = 0.0

    async def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = None
        if "pre_roll_s" in payload or "post_roll_s" in payload:
            current = await self.get(force=True)
        clean = await validate_updates(payload, current)
        async with get_sessionmaker()() as session:
            rows = {
                row.key: row
                for row in (await session.execute(select(Setting).where(Setting.key.in_(clean)))).scalars()
            }
            now = utcnow()
            for key, value in clean.items():
                if key in rows:
                    rows[key].value = value
                    rows[key].updated_at = now
                else:
                    session.add(Setting(key=key, value=value, updated_at=now))
            await session.commit()
        self.invalidate()
        return await self.get(force=True)

    async def check(self, path: str) -> PathCheck:
        return await check_path(path)


def storage_path(settings: dict[str, Any]) -> Path:
    return Path(str(settings.get("storage_root") or get_config().storage_root)).expanduser()
