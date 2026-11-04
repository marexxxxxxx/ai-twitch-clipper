from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import AppConfig
from app.twitch.client import TwitchClient
from app.twitch.gql import GQLClient
from app.twitch.helix import HelixClient


@pytest.mark.asyncio
async def test_anonymous_mode_without_credentials() -> None:
    config = AppConfig(
        twitch_client_id=None,
        twitch_client_secret=None,
        storage_root=Path("/tmp/clipper-test"),
    )
    client = TwitchClient(config)
    try:
        assert client.mode == "anonymous-gql"
        assert isinstance(client._impl, GQLClient)  # noqa: SLF001
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_helix_mode_with_credentials() -> None:
    config = AppConfig(
        twitch_client_id="client-id",
        twitch_client_secret="secret",
        storage_root=Path("/tmp/clipper-test"),
    )
    client = TwitchClient(config)
    try:
        assert client.mode == "helix"
        assert isinstance(client._impl, HelixClient)  # noqa: SLF001
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_helix_channel_parsing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.path.endswith("/users"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "42", "login": "shroud", "display_name": "shroud"}
                    ]
                },
            )
        if request.url.path.endswith("/streams"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "stream-1",
                            "user_id": "42",
                            "user_login": "shroud",
                            "user_name": "shroud",
                            "viewer_count": 1234,
                            "started_at": "2026-11-04T10:00:00Z",
                            "game_name": "VALORANT",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/clips"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "clip-1",
                            "title": "nice",
                            "created_at": "2026-11-04T11:00:00Z",
                            "duration": 30.0,
                            "view_count": 5,
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"message": "unexpected"})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5)
    client = HelixClient("client-id", "secret", http_client=http)
    try:
        channel = await client.fetch_channel("shroud")
    finally:
        await http.aclose()

    assert channel.id == "42"
    assert channel.is_live is True
    assert channel.viewers == 1234
    assert channel.game_name == "VALORANT"
    assert len(channel.clips) == 1
    assert channel.clips[0].duration_s == 30.0


@pytest.mark.asyncio
async def test_helix_refreshes_token_after_401_in_same_call() -> None:
    calls = {"token": 0, "users": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            calls["token"] += 1
            token = "token1" if calls["token"] == 1 else "token2"
            return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
        authorization = request.headers.get("Authorization")
        if request.url.path.endswith("/users"):
            calls["users"] += 1
            if authorization == "Bearer token1":
                return httpx.Response(401, json={"message": "invalid token"})
            assert authorization == "Bearer token2"
            return httpx.Response(
                200, json={"data": [{"id": "42", "login": "shroud", "display_name": "shroud"}]}
            )
        if request.url.path.endswith("/streams"):
            assert authorization == "Bearer token2"
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/clips"):
            assert authorization == "Bearer token2"
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"message": "unexpected"})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5)
    client = HelixClient("client-id", "secret", http_client=http)
    try:
        channel = await client.fetch_channel("shroud")
    finally:
        await http.aclose()

    assert channel.id == "42"
    assert channel.is_live is False
    assert calls["token"] == 2
    assert calls["users"] == 2
