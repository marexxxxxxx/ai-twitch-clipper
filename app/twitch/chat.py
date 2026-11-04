"""Anonymer Twitch-Chat über IRC-WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import string
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosed

from ..timeutil import utcnow

logger = logging.getLogger(__name__)

CHAT_URL = "wss://irc-ws.chat.twitch.tv:443"

HYPE_EMOTES = (
    "KEKW",
    "LUL",
    "OMEGALUL",
    "Pog",
    "PogChamp",
    "monkaS",
    "PepeHands",
    "catJAM",
    "Clap",
    "ICANT",
    "HUH",
)
_HYPE_RE = re.compile(r"(?<!\w)(" + "|".join(re.escape(e) for e in HYPE_EMOTES) + r")(?!\w)")


@dataclass(slots=True)
class ChatMessage:
    ts: datetime
    user: str
    text: str
    emotes: list[str]
    emote_only: bool
    hype_count: int


def random_nick() -> str:
    suffix = "".join(random.choices(string.digits, k=8))
    return f"justinfan{suffix}"


def _parse_tags(raw: str) -> dict[str, str]:
    tags: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
        else:
            key, value = part, ""
        tags[key.replace("\\s", " ")] = (
            value.replace("\\s", " ").replace("\\:", ";").replace("\\\\", "\\").replace("\\r", "\r").replace("\\n", "\n")
        )
    return tags


def parse_privmsg(line: str) -> ChatMessage | None:
    """Parst eine IRC-Zeile. Gibt ``None`` zurück, wenn es keine Nachricht ist."""
    if not line.startswith("@"):
        return None
    try:
        tag_part, rest = line.split(" ", 1)
    except ValueError:
        return None
    tags = _parse_tags(tag_part[1:])
    if " PRIVMSG " not in rest:
        return None
    try:
        _prefix, rest2 = rest.split(" ", 1)
        _channel, text = rest2.split(" :", 1)
    except ValueError:
        return None

    emotes: list[str] = []
    ranges: list[tuple[int, int]] = []
    emotes_tag = tags.get("emotes", "")
    if emotes_tag:
        for chunk in emotes_tag.split("/"):
            if ":" not in chunk:
                continue
            emote_id, positions = chunk.split(":", 1)
            emotes.append(emote_id)
            for position in positions.split(","):
                if "-" not in position:
                    continue
                try:
                    start, end = position.split("-", 1)
                    ranges.append((int(start), int(end)))
                except ValueError:
                    continue

    emote_only = False
    if ranges:
        covered = [False] * len(text)
        for start, end in ranges:
            for index in range(max(0, start), min(len(text), end + 1)):
                covered[index] = True
        leftover = "".join(ch for ch, is_emote in zip(text, covered) if not is_emote and not ch.isspace())
        emote_only = leftover == ""
    elif text.strip() and _HYPE_RE.fullmatch(text.strip()):
        emote_only = True

    hype_count = len(_HYPE_RE.findall(text))
    return ChatMessage(
        ts=utcnow(),
        user=tags.get("display-name") or tags.get("login") or "",
        text=text,
        emotes=emotes,
        emote_only=emote_only,
        hype_count=hype_count,
    )


class ChatReader:
    """Verbindet sich anonym mit dem Chat eines Kanals und liefert Nachrichten.

    Reconnectet mit Backoff, bis :meth:`stop` gerufen wird. Alle Fehler werden
    geloggt, aber nicht nach außen geworfen.
    """

    def __init__(self, login: str, on_message: Callable[[ChatMessage], None]) -> None:
        self.login = login.strip().lower()
        self.on_message = on_message
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.connected = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"chat:{self.login}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown
                pass
            self._task = None

    async def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    CHAT_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=2**20,
                ) as ws:
                    await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                    await ws.send("PASS SCHMOOPIIE")
                    await ws.send(f"NICK {random_nick()}")
                    await ws.send(f"JOIN #{self.login}")
                    self.connected = True
                    backoff = 2.0
                    logger.info("Chat verbunden: #%s", self.login)
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        for line in str(raw).split("\r\n"):
                            if not line:
                                continue
                            if line.startswith("PING"):
                                await ws.send("PONG :tmi.twitch.tv")
                                continue
                            try:
                                message = parse_privmsg(line)
                            except Exception:  # noqa: BLE001 - eine kaputte Zeile darf den Chat nicht töten
                                logger.debug("Chat-Zeile nicht parsebar: %s", line[:200])
                                continue
                            if message is not None:
                                try:
                                    self.on_message(message)
                                except Exception:  # noqa: BLE001
                                    logger.exception("Chat-Callback fehlgeschlagen")
            except asyncio.CancelledError:
                raise
            except (ConnectionClosed, OSError, websockets.WebSocketException) as exc:
                self.connected = False
                if self._stop.is_set():
                    break
                logger.warning("Chat #%s getrennt (%s), Reconnect in %.0fs", self.login, exc, backoff)
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                if self._stop.is_set():
                    break
                logger.warning("Chat #%s Fehler (%s), Reconnect in %.0fs", self.login, exc, backoff)
            finally:
                self.connected = False
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(60.0, backoff * 2)
