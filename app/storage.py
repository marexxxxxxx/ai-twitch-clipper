"""Dateisystem-Helfer: Pfadprüfung, freier Platz, atomare Writes."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


class StorageError(Exception):
    """Pfad kann nicht verwendet werden."""


@dataclass(slots=True)
class PathCheck:
    path: Path
    exists: bool
    writable: bool
    free_bytes: int
    total_bytes: int
    message: str = ""

    @property
    def free_gb(self) -> float:
        return round(self.free_bytes / (1024**3), 2)

    @property
    def total_gb(self) -> float:
        return round(self.total_bytes / (1024**3), 2)


def _check_path_sync(raw: str) -> PathCheck:
    if not raw or not str(raw).strip():
        raise StorageError("Der Speicherort darf nicht leer sein.")
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise StorageError("Bitte einen absoluten Pfad angeben.")
    path = path.resolve(strict=False)

    exists = path.exists()
    if exists and not path.is_dir():
        raise StorageError(f"{path} existiert, ist aber kein Verzeichnis.")

    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise StorageError(f"Verzeichnis {path} kann nicht angelegt werden (keine Rechte).") from exc
    except OSError as exc:
        raise StorageError(f"Verzeichnis {path} kann nicht angelegt werden: {exc}") from exc

    probe: Path | None = None
    try:
        fd, probe_name = tempfile.mkstemp(prefix=".clipper-write-test-", dir=str(path))
        probe = Path(probe_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("ok")
    except OSError as exc:
        usage = shutil.disk_usage(str(path))
        return PathCheck(
            path=path,
            exists=exists,
            writable=False,
            free_bytes=usage.free,
            total_bytes=usage.total,
            message=f"Nicht beschreibbar: {exc}",
        )
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass

    usage = shutil.disk_usage(str(path))
    return PathCheck(
        path=path,
        exists=exists,
        writable=True,
        free_bytes=usage.free,
        total_bytes=usage.total,
        message="Verzeichnis ist beschreibbar.",
    )


async def check_path(raw: str | Path) -> PathCheck:
    return await asyncio.to_thread(_check_path_sync, str(raw))


def free_bytes(path: Path | str) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def ensure_dir(path: Path | str) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def atomic_write_text(path: Path | str, content: str, encoding: str = "utf-8") -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def atomic_write_bytes(path: Path | str, content: bytes) -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def directory_size(path: Path | str) -> int:
    total = 0
    root = Path(path)
    if not root.exists():
        return 0
    for entry in root.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total
