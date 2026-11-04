"""Anonymer Twitch-GraphQL-Client.

Nutzt die öffentliche Web-Client-ID und benötigt keine Zugangsdaten.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..timeutil import parse_iso
from .types import Channel, Clip, TwitchError, TwitchUnavailable, UserNotFound

logger = logging.getLogger(__name__)

GQL_URL = "https://gql.twitch.tv/gql"
PUBLIC_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"

#: Twitch GQL akzeptiert maximal 15 Root-Field-Aliase pro Query (16 ⇒ HTTP 400).
BATCH_CHUNK_SIZE = 15
MAX_ROOT_ALIASES = 15

#: Größte stabile Batch-Größe für Clip-Abfragen.
#: Messung 2026-11-04 gegen den echten GQL-Endpunkt (Live-Logins,
#: ``clips(first:10, criteria:{period:LAST_DAY})``):
#:   n=5: 5/5 erfolgreich, n=3: 5/5, n=1: 2/2.
#: Ein 15er-Clip-Batch war zuvor instabil (nur 2/5), daher bleibt der separate
#: Clip-Poll bewusst bei 5 Aliasen. Bei Änderungen erneut messen.
CLIP_BATCH_SIZE = 5

CHANNEL_QUERY = """
query Channel($login: String!) {
  user(login: $login) {
    id
    login
    displayName
    stream {
      id
      viewersCount
      createdAt
      game { name }
    }
    clips(first: 10, criteria: {period: LAST_DAY}) {
      edges {
        node { id title createdAt durationSeconds viewCount }
      }
    }
  }
}
""".strip()

CLIPS_QUERY = """
query Clips($login: String!) {
  user(login: $login) {
    id
    clips(first: 10, criteria: {period: LAST_DAY}) {
      edges {
        node { id title createdAt durationSeconds viewCount }
      }
    }
  }
}
""".strip()

TOP_STREAMS_QUERY = """
query TopStreams($first: Int!) {
  streams(first: $first) {
    edges {
      node {
        id
        viewersCount
        createdAt
        game { name }
        broadcaster { id login displayName }
      }
    }
  }
}
""".strip()

#: Live-Status-Felder je Alias. Bewusst **ohne** ``clips``: Ein Batch aus 15
#: Aliasen mit Clip-Feldern war messbar instabil (Twitch lieferte HTTP 400/
#: Teilfehler), während derselbe Batch ohne ``clips`` zuverlässig antwortet.
_CHANNEL_FIELDS = """
    id
    login
    displayName
    stream {
      id
      viewersCount
      createdAt
      game { name }
    }
""".strip()

#: Clip-Felder für den separaten Clip-Poll.
_CLIP_FIELDS = """
    id
    login
    clips(first: 10, criteria: {period: LAST_DAY}) {
      edges {
        node { id title createdAt durationSeconds viewCount }
      }
    }
""".strip()


def _build_alias_query(name: str, count: int, fields: str) -> str:
    total = max(0, count)
    if total > MAX_ROOT_ALIASES:
        raise ValueError(
            f"Twitch GQL erlaubt maximal {MAX_ROOT_ALIASES} Root-Field-Aliase, "
            f"angefragt: {total}"
        )
    declarations = ", ".join(f"$l{index}: String!" for index in range(total))
    aliases = [
        f"  a{index}: user(login: $l{index}) {{\n{fields}\n  }}"
        for index in range(total)
    ]
    return f"query {name}({declarations}) {{\n" + "\n".join(aliases) + "\n}"


def build_batch_query(count: int) -> str:
    """Baut eine Live-Status-Anfrage mit ``count`` Aliasen (``a0``..``aN-1``)."""
    return _build_alias_query("BatchChannels", count, _CHANNEL_FIELDS)


def build_clips_batch_query(count: int) -> str:
    """Baut eine Clip-Anfrage mit ``count`` Aliasen (ohne Live-Status-Felder)."""
    return _build_alias_query("BatchClips", count, _CLIP_FIELDS)


def chunk_logins(logins: list[str], size: int = BATCH_CHUNK_SIZE) -> list[list[str]]:
    size = max(1, int(size))
    return [logins[index : index + size] for index in range(0, len(logins), size)]


class GQLClient:
    def __init__(
        self,
        client_id: str = PUBLIC_CLIENT_ID,
        timeout: float = 12.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id or PUBLIC_CLIENT_ID
        self._timeout = timeout
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        #: Anzahl der in diesem Poll-Zyklus abgesetzten POSTs (für /api/capacity).
        self.requests_last_cycle = 0

    def begin_cycle(self) -> None:
        """Setzt den Request-Zähler zu Beginn eines Poll-Zyklus zurück."""
        self.requests_last_cycle = 0

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        headers = {
            "Client-ID": self._client_id,
            "Content-Type": "application/json",
            "User-Agent": "ai-twitch-clipper/1.0",
        }
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                self.requests_last_cycle += 1
                response = await self._http.post(GQL_URL, json=payload, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise TwitchUnavailable(f"Twitch GQL HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise TwitchError(f"Twitch GQL HTTP {response.status_code}: {response.text[:200]}")
                data = response.json()
                payload = data.get("data")
                if data.get("errors"):
                    messages = "; ".join(str(err.get("message", err)) for err in data["errors"])
                    # Twitch liefert bei Alias-Batches oft ``data`` und ``errors``
                    # gemeinsam (einzelne Aliase fehlen). Vorhandene Daten
                    # behalten; nur fehlende Aliase gelten als Fehler.
                    if isinstance(payload, dict) and payload:
                        logger.warning("Twitch GQL Teil-Antwort (%s); nutze vorhandene Daten", messages)
                        return payload
                    raise TwitchError(f"Twitch GQL: {messages}")
                return payload or {}
            except (httpx.HTTPError, TwitchUnavailable) as exc:
                last_error = exc
                if attempt == 3:
                    break
                delay = min(8.0, 0.5 * (2**attempt))
                logger.warning("GQL-Anfrage fehlgeschlagen (%s), neuer Versuch in %.1fs", exc, delay)
                await asyncio.sleep(delay)
        raise TwitchUnavailable(f"Twitch nicht erreichbar: {last_error}") from last_error

    @classmethod
    def _channel_from_user(cls, user: dict[str, Any], login: str) -> Channel:
        stream = user.get("stream") or {}
        clips = cls._parse_clips((user.get("clips") or {}).get("edges") or [])
        return Channel(
            id=str(user.get("id") or ""),
            login=str(user.get("login") or login).lower(),
            display_name=str(user.get("displayName") or login),
            is_live=bool(stream),
            viewers=int(stream.get("viewersCount") or 0) if stream else 0,
            started_at=parse_iso(stream.get("createdAt")) if stream else None,
            game_name=((stream.get("game") or {}).get("name") if stream else None),
            stream_id=str(stream.get("id")) if stream and stream.get("id") else None,
            clips=clips,
        )

    async def fetch_channel(self, login: str) -> Channel:
        login = login.strip().lower()
        data = await self._post(CHANNEL_QUERY, {"login": login})
        user = data.get("user")
        if not user:
            raise UserNotFound(f"Twitch-Kanal '{login}' wurde nicht gefunden.")
        return self._channel_from_user(user, login)

    async def fetch_channels_batch(self, logins: list[str]) -> dict[str, Channel]:
        """Holt bis zu 15 Live-Status-Kanäle pro POST über GQL-Aliase.

        Die Anfrage enthält bewusst keine Clip-Felder; Clips kommen über
        :meth:`fetch_clips_batch` in einem eigenen, kleineren Batch.

        Unbekannte Logins (``user: null``) fehlen im Ergebnis, ohne den
        restlichen Batch zu verwerfen. Fällt ein Chunk komplett aus, werden
        die übrigen Chunks trotzdem genutzt; erst wenn kein einziger Login
        ein Ergebnis liefert, wird der Fehler weitergereicht.
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

        channels: dict[str, Channel] = {}
        errors: list[Exception] = []
        for chunk in chunk_logins(normalized):
            variables = {f"l{index}": login for index, login in enumerate(chunk)}
            try:
                data = await self._post(build_batch_query(len(chunk)), variables)
            except TwitchError as exc:
                errors.append(exc)
                logger.warning("Batch-Poll für %d Kanal/Kanäle fehlgeschlagen: %s", len(chunk), exc)
                continue
            for index, login in enumerate(chunk):
                user = data.get(f"a{index}")
                if not user:
                    logger.debug("Twitch-Kanal '%s' im Batch nicht gefunden", login)
                    continue
                channels[login] = self._channel_from_user(user, login)
        if errors and not channels:
            raise errors[-1]
        return channels

    async def fetch_clips_batch(self, logins: list[str]) -> dict[str, list[Clip]]:
        """Holt Clips für mehrere Logins in separaten, kleinen GQL-Batches.

        ``CLIP_BATCH_SIZE`` ist der messtechnisch größte stabile Chunk. Fällt
        ein Chunk aus, bleiben die übrigen Ergebnisse nutzbar; erst wenn kein
        einziger Login Clips liefert, wird der Fehler weitergereicht. Der
        Aufrufer behält bei Fehlern seinen letzten bekannten Cache.
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

        result: dict[str, list[Clip]] = {}
        errors: list[Exception] = []
        for chunk in chunk_logins(normalized, CLIP_BATCH_SIZE):
            variables = {f"l{index}": login for index, login in enumerate(chunk)}
            try:
                data = await self._post(build_clips_batch_query(len(chunk)), variables)
            except TwitchError as exc:
                errors.append(exc)
                logger.warning("Clip-Batch für %d Kanal/Kanäle fehlgeschlagen: %s", len(chunk), exc)
                continue
            for index, login in enumerate(chunk):
                user = data.get(f"a{index}")
                if not user:
                    logger.debug("Twitch-Clip-Batch: Kanal '%s' fehlt in der Antwort", login)
                    continue
                result[login] = self._parse_clips((user.get("clips") or {}).get("edges") or [])
        if errors and not result:
            raise errors[-1]
        return result

    async def fetch_clips(self, login: str) -> list[Clip]:
        data = await self._post(CLIPS_QUERY, {"login": login.strip().lower()})
        user = data.get("user")
        if not user:
            raise UserNotFound(f"Twitch-Kanal '{login}' wurde nicht gefunden.")
        return self._parse_clips((user.get("clips") or {}).get("edges") or [])

    async def fetch_top_live(self, limit: int = 20) -> list[Channel]:
        data = await self._post(TOP_STREAMS_QUERY, {"first": max(1, min(int(limit), 100))})
        channels: list[Channel] = []
        for edge in (data.get("streams") or {}).get("edges") or []:
            node = edge.get("node") or {}
            broadcaster = node.get("broadcaster") or {}
            if not broadcaster.get("login"):
                continue
            channels.append(
                Channel(
                    id=str(broadcaster.get("id") or ""),
                    login=str(broadcaster.get("login")).lower(),
                    display_name=str(broadcaster.get("displayName") or broadcaster.get("login")),
                    is_live=True,
                    viewers=int(node.get("viewersCount") or 0),
                    started_at=parse_iso(node.get("createdAt")),
                    game_name=((node.get("game") or {}).get("name")),
                    stream_id=str(node.get("id")) if node.get("id") else None,
                )
            )
        return channels

    @staticmethod
    def _parse_clips(edges: list[dict[str, Any]]) -> list[Clip]:
        clips: list[Clip] = []
        for edge in edges:
            node = edge.get("node") or {}
            created = parse_iso(node.get("createdAt"))
            if not node.get("id") or created is None:
                continue
            try:
                duration = float(node.get("durationSeconds") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            clips.append(
                Clip(
                    id=str(node["id"]),
                    title=str(node.get("title") or ""),
                    created_at=created,
                    duration_s=duration,
                    view_count=int(node.get("viewCount") or 0),
                )
            )
        return clips
