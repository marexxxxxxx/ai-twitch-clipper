from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.runtime_settings import SettingsValidationError, validate_updates
from app.storage import StorageError, check_path


@pytest.mark.asyncio
async def test_check_path_creates_and_accepts_writable_dir(tmp_path: Path) -> None:
    target = tmp_path / "neu" / "clips"
    result = await check_path(str(target))
    assert result.writable is True
    assert target.is_dir()
    assert result.free_bytes > 0
    assert result.free_gb > 0


@pytest.mark.asyncio
async def test_check_path_rejects_file(tmp_path: Path) -> None:
    file = tmp_path / "datei.txt"
    file.write_text("x")
    with pytest.raises(StorageError, match="kein Verzeichnis"):
        await check_path(str(file))


@pytest.mark.asyncio
async def test_check_path_rejects_relative_path() -> None:
    with pytest.raises(StorageError, match="absoluten Pfad"):
        await check_path("relative/clips")


@pytest.mark.asyncio
async def test_check_path_reports_unwritable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("Als root sind Rechteprüfungen nicht aussagekräftig.")
    target = tmp_path / "readonly"
    target.mkdir()
    target.chmod(0o500)
    try:
        result = await check_path(str(target))
        assert result.writable is False
        assert "Nicht beschreibbar" in result.message
    finally:
        target.chmod(0o700)


@pytest.mark.asyncio
async def test_validate_updates_accepts_valid_storage_root(tmp_path: Path) -> None:
    clean = await validate_updates({"storage_root": str(tmp_path / "clips")})
    assert clean["storage_root"].endswith("clips")


@pytest.mark.asyncio
async def test_validate_updates_rejects_bad_threshold() -> None:
    with pytest.raises(SettingsValidationError, match="Schwelle"):
        await validate_updates({"threshold": 1.5})
    with pytest.raises(SettingsValidationError, match="Cooldown"):
        await validate_updates({"cooldown_s": -1})


@pytest.mark.asyncio
async def test_validate_updates_rejects_unknown_key() -> None:
    with pytest.raises(SettingsValidationError, match="Unbekannte"):
        await validate_updates({"gibt_es_nicht": 1})


@pytest.mark.asyncio
async def test_validate_updates_rejects_zero_weights() -> None:
    with pytest.raises(SettingsValidationError, match="Gewicht"):
        await validate_updates(
            {
                "weights": {
                    "chat_rate": 0,
                    "emote_burst": 0,
                    "clip_created": 0,
                    "viewers": 0,
                    "clip_burst": 0,
                }
            }
        )


@pytest.mark.asyncio
async def test_validate_updates_normalizes_weights() -> None:
    clean = await validate_updates({"weights": {"chat_rate": 0.5}})
    assert clean["weights"]["chat_rate"] == 0.5
    assert clean["weights"]["clip_created"] == 0.30


@pytest.mark.asyncio
async def test_check_path_rejects_nul_byte() -> None:
    with pytest.raises(StorageError, match="NUL"):
        await check_path("/tmp/\x00boese")
    with pytest.raises(SettingsValidationError):
        await validate_updates({"storage_root": "/tmp/\x00boese"})


def test_settings_api_returns_400_for_nul_path() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import settings as settings_api

    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app) as client:
        response = client.post("/api/settings/check-path", json={"path": "/tmp/\x00boese"})
    assert response.status_code == 400
    assert "NUL" in response.json()["detail"]
