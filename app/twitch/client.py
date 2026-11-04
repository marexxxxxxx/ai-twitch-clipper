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

    async def fetch_clips(self, login: str) -> list[Clip]:
        return await self._impl.fetch_clips(login)

    async def fetch_top_live(self, limit: int = 20) -> list[Channel]:
        return await self._impl.fetch_top_live(limit)
