"""Fassade: wählt Helix (mit Credentials) oder anonymes GraphQL."""

from __future__ import annotations

import logging

from ..config import AppConfig, get_config
from .gql import GQLClient
from .helix import HelixClient
from .types import Channel, Clip, TwitchError, TwitchUnavailable, UserNotFound

logger = logging.getLogger(__name__)

__all__ = [
    "TwitchClient",
    "Channel",
    "Clip",
    "TwitchError",
    "TwitchUnavailable",
    "UserNotFound",
]


class TwitchClient:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        if self.config.helix_enabled:
            assert self.config.twitch_client_id and self.config.twitch_client_secret
            self._impl: HelixClient | GQLClient = HelixClient(
                self.config.twitch_client_id, self.config.twitch_client_secret
            )
            self.mode = "helix"
        else:
            self._impl = GQLClient()
            self.mode = "anonymous-gql"
        logger.info("Twitch-Client-Modus: %s", self.mode)

    async def close(self) -> None:
        await self._impl.close()

    async def fetch_channel(self, login: str) -> Channel:
        return await self._impl.fetch_channel(login)

    async def fetch_channels_batch(self, logins: list[str]) -> dict[str, Channel]:
        """Zentrale Batch-Abfrage für alle überwachten Streamer."""
        return await self._impl.fetch_channels_batch(logins)

    async def fetch_clips(self, login: str) -> list[Clip]:
        return await self._impl.fetch_clips(login)

    async def fetch_clips_batch(self, logins: list[str]) -> dict[str, list[Clip]]:
        """Separater Clip-Poll (kleinere Chunks als der Live-Status-Batch)."""
        fetch = getattr(self._impl, "fetch_clips_batch", None)
        if callable(fetch):
            return await fetch(logins)
        result: dict[str, list[Clip]] = {}
        for login in logins:
            result[login.strip().lower()] = await self._impl.fetch_clips(login)
        return result

    def reset_request_counter(self) -> None:
        """Setzt den Request-Zähler für den beginnenden Poll-Zyklus zurück."""
        begin = getattr(self._impl, "begin_cycle", None)
        if callable(begin):
            begin()

    @property
    def gql_requests_last_cycle(self) -> int:
        """Requests des letzten Poll-Zyklus (bei Helix: HTTP-GETs)."""
        return int(getattr(self._impl, "requests_last_cycle", 0) or 0)

    async def fetch_top_live(self, limit: int = 20) -> list[Channel]:
        return await self._impl.fetch_top_live(limit)
