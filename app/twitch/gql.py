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
                response = await self._http.post(GQL_URL, json=payload, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise TwitchUnavailable(f"Twitch GQL HTTP {response.status_code}")
                if response.status_code >= 400:
                    raise TwitchError(f"Twitch GQL HTTP {response.status_code}: {response.text[:200]}")
                data = response.json()
                if data.get("errors"):
                    messages = "; ".join(str(err.get("message", err)) for err in data["errors"])
                    raise TwitchError(f"Twitch GQL: {messages}")
                return data.get("data") or {}
            except (httpx.HTTPError, TwitchUnavailable) as exc:
                last_error = exc
                if attempt == 3:
                    break
                delay = min(8.0, 0.5 * (2**attempt))
                logger.warning("GQL-Anfrage fehlgeschlagen (%s), neuer Versuch in %.1fs", exc, delay)
                await asyncio.sleep(delay)
        raise TwitchUnavailable(f"Twitch nicht erreichbar: {last_error}") from last_error

    async def fetch_channel(self, login: str) -> Channel:
        login = login.strip().lower()
        data = await self._post(CHANNEL_QUERY, {"login": login})
        user = data.get("user")
        if not user:
            raise UserNotFound(f"Twitch-Kanal '{login}' wurde nicht gefunden.")
        stream = user.get("stream") or {}
        clips = self._parse_clips((user.get("clips") or {}).get("edges") or [])
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
