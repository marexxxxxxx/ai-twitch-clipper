"""Statische Konfiguration aus Umgebungsvariablen.

Laufzeit-Einstellungen (Schwellen, Speicherort, ...) liegen in der Tabelle
``setting`` und werden über :class:`app.runtime_settings.SettingsService`
gelesen. Diese Datei enthält nur Boot-Parameter, die sich nicht im UI ändern
lassen sollen.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    storage_root: Path = Field(default=Path("/data"))
    data_dir: Path = Field(default=Path("/data"))
    database_path: Path | None = None

    port: int = 8000
    log_level: str = "INFO"
    quality: str = "720p60,best"

    twitch_client_id: str | None = None
    twitch_client_secret: str | None = None

    # How often an offline channel is re-checked and how often clips are polled.
    live_poll_s: int = 30
    clip_poll_s: int = 30
    viewer_poll_s: int = 20
    tick_s: int = 5

    # Wird nur von Tools/CLI genutzt, um die lokale API zu erreichen.
    api_base_url: str = "http://127.0.0.1:8000"

    @property
    def db_path(self) -> Path:
        if self.database_path:
            return self.database_path
        return self.data_dir / "clipper.db"

    @property
    def helix_enabled(self) -> bool:
        return bool(self.twitch_client_id and self.twitch_client_secret)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    # TZ wird von libc/ffmpeg für die Anzeige genutzt; intern rechnen wir UTC.
    tz = os.environ.get("TZ")
    if tz:
        try:
            import time

            time.tzset()
        except (AttributeError, OSError):  # pragma: no cover - nicht auf Linux
            pass
    return AppConfig()
