"""Logging: Konsole + In-Memory-Puffer + asynchrone Persistenz in SQLite."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .db import get_sessionmaker
from .models import LogEntry
from .timeutil import utcnow

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_BUFFER = 2000


class LogHub:
    """Sammelt Logzeilen für die UI und schreibt sie gebündelt in die DB."""

    def __init__(self) -> None:
        self.buffer: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFER)
        self.queue: asyncio.Queue[dict[str, Any]] | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task[None] | None = None
        self._dropped = 0

    def start(self) -> None:
        if self.task is not None:
            return
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=5000)
        self.task = asyncio.create_task(self._writer(), name="log-writer")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            self.task = None
        await self._flush()

    def add(
        self,
        level: str,
        message: str,
        streamer_login: str | None = None,
        created: datetime | None = None,
    ) -> None:
        item = {
            "ts": created or utcnow(),
            "level": level,
            "streamer_login": streamer_login,
            "message": message,
        }
        self.buffer.append(item)
        if self.queue is None:
            return
        try:
            if self.loop is not None and self.loop.is_running():
                self.loop.call_soon_threadsafe(self._enqueue, item)
        except RuntimeError:
            pass

    def _enqueue(self, item: dict[str, Any]) -> None:
        if self.queue is None:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                logging.getLogger(__name__).warning("Log-Queue voll, %d Einträge verworfen", self._dropped)

    async def _writer(self) -> None:
        while True:
            batch: list[dict[str, Any]] = []
            try:
                first = await self.queue.get()  # type: ignore[union-attr]
                batch.append(first)
                await asyncio.sleep(0.8)
                while len(batch) < 200:
                    batch.append(self.queue.get_nowait())  # type: ignore[union-attr]
            except asyncio.QueueEmpty:
                pass
            if not batch:
                continue
            try:
                async with get_sessionmaker()() as session:
                    session.add_all(
                        [
                            LogEntry(
                                ts=item["ts"],
                                level=item["level"],
                                streamer_login=item.get("streamer_login"),
                                message=item["message"][:4000],
                            )
                            for item in batch
                        ]
                    )
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - Logging darf die App nie töten
                logging.getLogger(__name__).debug("Log-Persistenz fehlgeschlagen", exc_info=True)

    async def _flush(self) -> None:
        if self.queue is None:
            return
        items: list[dict[str, Any]] = []
        while not self.queue.empty():
            items.append(self.queue.get_nowait())
        if not items:
            return
        try:
            async with get_sessionmaker()() as session:
                session.add_all(
                    [
                        LogEntry(
                            ts=item["ts"],
                            level=item["level"],
                            streamer_login=item.get("streamer_login"),
                            message=item["message"][:4000],
                        )
                        for item in items
                    ]
                )
                await session.commit()
        except Exception:  # noqa: BLE001
            pass

    def recent(self, limit: int = 500) -> list[dict[str, Any]]:
        items = list(self.buffer)[-limit:]
        return [
            {
                "ts": item["ts"].isoformat() + "Z",
                "level": item["level"],
                "streamer_login": item.get("streamer_login"),
                "message": item["message"],
            }
            for item in items
        ]


log_hub = LogHub()


class HubHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            log_hub.add(
                level=record.levelname,
                message=message,
                streamer_login=getattr(record, "streamer_login", None),
                created=datetime.fromtimestamp(record.created, tz=timezone.utc).replace(tzinfo=None),
            )
        except Exception:  # noqa: BLE001
            self.handleError(record)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    root.addHandler(stream)

    hub = HubHandler()
    hub.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(hub)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
