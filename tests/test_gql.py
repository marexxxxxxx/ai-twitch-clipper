from __future__ import annotations

import json

import httpx
import pytest

from app.twitch.gql import GQLClient
from app.twitch.types import TwitchError, UserNotFound


def make_client(handler) -> tuple[GQLClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5)
    return GQLClient(http_client=http), http


def test_parse_channel_with_stream_and_clips() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["variables"]["login"] == "shroud"
        return httpx.Response(
            200,
            json={
                "data": {
                    "user": {
                        "id": "12345",
                        "login": "shroud",
                        "displayName": "shroud",
                        "stream": {
                            "id": "stream-1",
                            "viewersCount": 4321,
                            "createdAt": "2026-11-04T10:00:00Z",
                            "game": {"name": "VALORANT"},
                        },
                        "clips": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "clip-1",
                                        "title": "nice",
                                        "createdAt": "2026-11-04T11:59:00Z",
                                        "durationSeconds": 30,
                                        "viewCount": 12,
                                    }
                                }
                            ]
                        },
                    }
                }
            },
        )

    async def run() -> None:
        client, http = make_client(handler)
        try:
            channel = await client.fetch_channel("Shroud")
        finally:
            await http.aclose()
        assert channel.id == "12345"
        assert channel.login == "shroud"
        assert channel.is_live is True
        assert channel.viewers == 4321
        assert channel.game_name == "VALORANT"
        assert channel.stream_id == "stream-1"
        assert channel.started_at is not None
        assert len(channel.clips) == 1
        assert channel.clips[0].duration_s == 30

    import asyncio

    asyncio.run(run())


def test_unknown_user_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"user": None}})

    async def run() -> None:
        client, http = make_client(handler)
        try:
            with pytest.raises(UserNotFound):
                await client.fetch_channel("gibt-es-nicht")
        finally:
            await http.aclose()

    import asyncio

    asyncio.run(run())


def test_graphql_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "rate limited"}]})

    async def run() -> None:
        client, http = make_client(handler)
        try:
            with pytest.raises(TwitchError, match="rate limited"):
                await client.fetch_channel("shroud")
        finally:
            await http.aclose()

    import asyncio

    asyncio.run(run())


def test_offline_channel_has_no_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "user": {
                        "id": "1",
                        "login": "offline",
                        "displayName": "Offline",
                        "stream": None,
                        "clips": {"edges": []},
                    }
                }
            },
        )

    async def run() -> None:
        client, http = make_client(handler)
        try:
            channel = await client.fetch_channel("offline")
        finally:
            await http.aclose()
        assert channel.is_live is False
        assert channel.viewers == 0

    import asyncio

    asyncio.run(run())


def test_top_live_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "streams": {
                        "edges": [
                            {
                                "node": {
                                    "id": "s1",
                                    "viewersCount": 9000,
                                    "createdAt": "2026-11-04T09:00:00Z",
                                    "game": {"name": "Just Chatting"},
                                    "broadcaster": {"id": "42", "login": "someone", "displayName": "SomeOne"},
                                }
                            }
                        ]
                    }
                }
            },
        )

    async def run() -> None:
        client, http = make_client(handler)
        try:
            channels = await client.fetch_top_live(5)
        finally:
            await http.aclose()
        assert len(channels) == 1
        assert channels[0].login == "someone"
        assert channels[0].viewers == 9000

    import asyncio

    asyncio.run(run())
