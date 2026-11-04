"""Optionaler Twitch-Helix-Client (App-Access-Token).

Wird nur genutzt, wenn ``TWITCH_CLIENT_ID`` und ``TWITCH_CLIENT_SECRET``
gesetzt sind. Schnittstelle identisch zum anonymen GQL-Client.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

import httpx

from ..timeutil import parse_iso, utcnow
from .types import Channel, Clip, TwitchError, TwitchUnavailable, UserNotFound

logger = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"

#: Helix kennt keine Batch-Alias-Anfrage; Einzelabfragen laufen begrenzt parallel.
BATCH_CONCURRENCY = 5


class HelixClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        timeout: float = 12.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("Helix benötigt Client-ID und Client-Secret.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        #: Anzahl der in diesem Poll-Zyklus abgesetzten Requests.
        self.requests_last_cycle = 0

    def begin_cycle(self) -> None:
        """Setzt den Request-Zähler zu Beginn eines Poll-Zyklus zurück."""
        self.requests_last_cycle = 0

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires - 60:
                return self._token
            try:
                response = await self._http.post(
                    TOKEN_URL,
                    params={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "client_credentials",
                    },
                )
            except httpx.HTTPError as exc:
                raise TwitchUnavailable(f"Helix-Token nicht erreichbar: {exc}") from exc
            if response.status_code != 200:
                raise TwitchUnavailable(
                    f"Helix-Token HTTP {response.status_code}: {response.text[:200]}"
                )
            data = response.json()
            self._token = str(data["access_token"])
            self._token_expires = time.monotonic() + float(data.get("expires_in") or 3600)
            return self._token

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            # Token/Header bei jedem Versuch neu bilden, damit ein 401-Refresh
            # innerhalb desselben Aufrufs tatsächlich mit dem neuen Token läuft.
            token = await self._access_token()
            headers = {"Client-ID": self._client_id, "Authorization": f"Bearer {token}"}
            try:
                self.requests_last_cycle += 1
                response = await self._http.get(HELIX + path, params=params, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise TwitchUnavailable(f"Helix HTTP {response.status_code}")
                if response.status_code == 401 and attempt == 0:
                    # Token verworfen/abgelaufen: neu holen.
                    self._token = None
                    continue
                if response.status_code >= 400:
                    raise TwitchError(f"Helix HTTP {response.status_code}: {response.text[:200]}")
                return response.json().get("data") or {}
            except (httpx.HTTPError, TwitchUnavailable) as exc:
                last_error = exc
                if attempt == 2:
                    break
                await asyncio.sleep(0.5 * (2**attempt))
        raise TwitchUnavailable(f"Helix nicht erreichbar: {last_error}") from last_error

    async def _fetch_live_channel(self, login: str) -> Channel:
        """Live-Status ohne Clips (die laufen im separaten Clip-Poll)."""
        data = await self._get("/users", {"login": login})
        users = data if isinstance(data, list) else data.get("data") or []
        if not users:
            raise UserNotFound(f"Twitch-Kanal '{login}' wurde nicht gefunden.")
        user = users[0]
        user_id = str(user["id"])

        streams = await self._get("/streams", {"user_login": login})
        stream_items = streams if isinstance(streams, list) else streams.get("data") or []
        stream = stream_items[0] if stream_items else None

        return Channel(
            id=user_id,
            login=str(user.get("login") or login).lower(),
            display_name=str(user.get("display_name") or login),
            is_live=bool(stream),
            viewers=int(stream.get("viewer_count") or 0) if stream else 0,
            started_at=parse_iso(stream.get("started_at")) if stream else None,
            game_name=stream.get("game_name") if stream else None,
            stream_id=str(stream.get("id")) if stream and stream.get("id") else None,
        )

    async def fetch_channel(self, login: str) -> Channel:
        login = login.strip().lower()
        channel = await self._fetch_live_channel(login)
        channel.clips = await self.fetch_clips(login, channel.id)
        return channel

    async def fetch_channels_batch(self, logins: list[str]) -> dict[str, Channel]:
        """Fallback ohne Helix-Batch: Einzelabfragen parallel, max. 5 gleichzeitig.

        Unbekannte Logins fehlen im Ergebnis; einzelne Fehler brechen den
        restlichen Batch nicht ab. Clips werden hier nicht mitgeladen.
        """
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in logins:
            login = str(raw or "").strip().lower()
            if not login or login in seen:
                continue
            seen.add(login)
            normalized.append(login)
        if not normalized:
            return {}

        semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def fetch_one(login: str) -> tuple[str, Channel | None]:
            async with semaphore:
                try:
                    return login, await self.fetch_channel(login)
                except UserNotFound:
                    return login, None

        results = await asyncio.gather(
            *(fetch_one(login) for login in normalized), return_exceptions=True
        )
        channels: dict[str, Channel] = {}
        for item in results:
            if isinstance(item, asyncio.CancelledError):
                raise item
            if isinstance(item, BaseException):
                logger.warning("Helix-Batch-Teilabfrage fehlgeschlagen: %s", item)
                continue
            login, channel = item
            if channel is not None:
                channels[login] = channel
        return channels

    async def fetch_clips(self, login: str, broadcaster_id: str | None = None) -> list[Clip]:
        if not broadcaster_id:
            data = await self._get("/users", {"login": login.strip().lower()})
            users = data if isinstance(data, list) else data.get("data") or []
            if not users:
                raise UserNotFound(f"Twitch-Kanal '{login}' wurde nicht gefunden.")
            broadcaster_id = str(users[0]["id"])
        # Helix akzeptiert started_at nur innerhalb der letzten 24h.
        started_at = (utcnow() - timedelta(hours=23, minutes=55)).replace(microsecond=0).isoformat() + "Z"
        payload = await self._get(
            "/clips",
            {"broadcaster_id": broadcaster_id, "first": 20, "started_at": started_at},
        )
        items = payload if isinstance(payload, list) else payload.get("data") or []
        clips: list[Clip] = []
        for item in items:
            created = parse_iso(item.get("created_at"))
            if created is None:
                continue
            clips.append(
                Clip(
                    id=str(item.get("id")),
                    title=str(item.get("title") or ""),
                    created_at=created,
                    duration_s=float(item.get("duration") or 0),
                    view_count=int(item.get("view_count") or 0),
                )
            )
        return clips

    async def fetch_clips_batch(self, logins: list[str]) -> dict[str, list[Clip]]:
        """Clip-Poll parallel, max. 5 gleichzeitig; Fehler einzelner Logins werden übersprungen."""
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in logins:
            login = str(raw or "").strip().lower()
            if not login or login in seen:
                continue
            seen.add(login)
            normalized.append(login)
        if not normalized:
            return {}

        semaphore = asyncio.Semaphore(BATCH_CONCURRENCY)
        first_error: Exception | None = None

        async def fetch_one(login: str) -> tuple[str, list[Clip] | None]:
            async with semaphore:
                try:
                    return login, await self.fetch_clips(login)
                except UserNotFound:
                    return login, None
                except Exception as exc:  # noqa: BLE001
                    nonlocal first_error
                    if first_error is None:
                        first_error = exc
                    logger.warning("Helix-Clip-Abfrage %s fehlgeschlagen: %s", login, exc)
                    return login, None

        results = await asyncio.gather(
            *(fetch_one(login) for login in normalized), return_exceptions=True
        )
        clips_by_login: dict[str, list[Clip]] = {}
        for item in results:
            if isinstance(item, asyncio.CancelledError):
                raise item
            if isinstance(item, BaseException):
                if first_error is None:
                    first_error = item
                continue
            login, clips = item
            if clips is not None:
                clips_by_login[login] = clips
        if first_error is not None and not clips_by_login:
            raise first_error
        return clips_by_login

    async def fetch_top_live(self, limit: int = 20) -> list[Channel]:
        payload = await self._get("/streams", {"first": max(1, min(int(limit), 100))})
        items = payload if isinstance(payload, list) else payload.get("data") or []
        channels: list[Channel] = []
        for item in items:
            channels.append(
                Channel(
                    id=str(item.get("user_id") or ""),
                    login=str(item.get("user_login") or "").lower(),
                    display_name=str(item.get("user_name") or item.get("user_login") or ""),
                    is_live=True,
                    viewers=int(item.get("viewer_count") or 0),
                    started_at=parse_iso(item.get("started_at")),
                    game_name=item.get("game_name"),
                    stream_id=str(item.get("id")) if item.get("id") else None,
                )
            )
        return channels
