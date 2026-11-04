from __future__ import annotations

import json

import httpx
import pytest

from app.config import AppConfig
from app.models import Streamer
from app.runtime_settings import SettingsService
from app.supervisor import Supervisor
from app.twitch.gql import (
    BATCH_CHUNK_SIZE,
    CLIP_BATCH_SIZE,
    MAX_ROOT_ALIASES,
    GQLClient,
    build_batch_query,
    build_clips_batch_query,
)
from app.twitch.helix import HelixClient
from app.twitch.types import Channel


def test_batch_query_declares_all_aliases_and_variables() -> None:
    query = build_batch_query(15)
    for index in range(15):
        assert f"$l{index}: String!" in query
        assert f"a{index}: user(login: $l{index})" in query
    assert "viewersCount" in query
    # Clips gehören in den separaten Clip-Poll, nicht in den Live-Batch.
    assert "clips(" not in query


def test_alias_limit_is_enforced() -> None:
    assert BATCH_CHUNK_SIZE == 15
    assert MAX_ROOT_ALIASES == 15
    assert CLIP_BATCH_SIZE <= 15
    with pytest.raises(ValueError):
        build_batch_query(16)
    with pytest.raises(ValueError):
        build_clips_batch_query(16)


def test_clip_batch_query_contains_clips_but_no_stream() -> None:
    query = build_clips_batch_query(CLIP_BATCH_SIZE)
    assert "clips(first: 10, criteria: {period: LAST_DAY})" in query
    assert "viewersCount" not in query


def make_gql(handler) -> tuple[GQLClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5)
    return GQLClient(http_client=http), http


def channel_payload(login: str, index: int) -> dict:
    return {
        "id": f"id-{login}",
        "login": login,
        "displayName": f"Display {login}",
        "stream": {
            "id": f"stream-{login}",
            "viewersCount": index,
            "createdAt": "2026-11-04T10:00:00Z",
            "game": {"name": "VALORANT"},
        },
    }


def batch_handler(calls: list[list[str]], unknown: set[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        variables = payload["variables"]
        assert variables, "Batch ohne Variablen"
        calls.append(list(variables.values()))
        data: dict[str, object] = {}
        for key, login in variables.items():
            alias = "a" + key[1:]
            if login in unknown:
                data[alias] = None
                continue
            data[alias] = channel_payload(login, int(key[1:]))
        return httpx.Response(200, json={"data": data})

    return handler


@pytest.mark.asyncio
async def test_40_logins_use_exactly_three_gql_posts_of_max_15() -> None:
    calls: list[list[str]] = []
    client, http = make_gql(batch_handler(calls, unknown=set()))
    logins = [f"streamer{i}" for i in range(40)]
    try:
        client.begin_cycle()
        channels = await client.fetch_channels_batch(logins)
    finally:
        await http.aclose()

    assert len(calls) == 3
    assert [len(chunk) for chunk in calls] == [15, 15, 10]
    assert all(len(chunk) <= MAX_ROOT_ALIASES for chunk in calls)
    assert client.requests_last_cycle == 3
    assert len(channels) == 40
    assert set(channels) == set(logins)


@pytest.mark.asyncio
async def test_unknown_login_in_chunk_does_not_drop_the_rest() -> None:
    calls: list[list[str]] = []
    logins = [f"streamer{i}" for i in range(40)]
    unknown = {logins[5], logins[27]}
    client, http = make_gql(batch_handler(calls, unknown))
    try:
        channels = await client.fetch_channels_batch(logins)
    finally:
        await http.aclose()

    assert len(calls) == 3
    assert len(channels) == 38
    assert logins[5] not in channels
    assert logins[27] not in channels
    # Beide Chunks wurden trotz unbekannter Logins vollständig verarbeitet.
    assert logins[4] in channels
    assert logins[6] in channels
    assert logins[26] in channels
    assert logins[28] in channels


@pytest.mark.asyncio
async def test_batch_maps_login_to_channel_fields() -> None:
    calls: list[list[str]] = []
    client, http = make_gql(batch_handler(calls, unknown=set()))
    try:
        channels = await client.fetch_channels_batch(["Alpha", "beta", "alpha"])
    finally:
        await http.aclose()

    assert calls == [["alpha", "beta"]]
    assert channels["alpha"].login == "alpha"
    assert channels["alpha"].id == "id-alpha"
    assert channels["alpha"].display_name == "Display alpha"
    assert channels["alpha"].is_live is True
    assert channels["alpha"].viewers == 0
    assert channels["beta"].viewers == 1
    assert channels["beta"].stream_id == "stream-beta"
    # Der Live-Batch liefert keine Clips mehr.
    assert channels["alpha"].clips == []


@pytest.mark.asyncio
async def test_empty_batch_sends_no_request() -> None:
    calls: list[list[str]] = []
    client, http = make_gql(batch_handler(calls, unknown=set()))
    try:
        client.begin_cycle()
        channels = await client.fetch_channels_batch(["", "   "])
    finally:
        await http.aclose()

    assert channels == {}
    assert calls == []
    assert client.requests_last_cycle == 0


@pytest.mark.asyncio
async def test_failing_chunk_keeps_other_chunk_results() -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        variables = payload["variables"]
        calls.append(list(variables.values()))
        if len(calls) == 1:
            return httpx.Response(200, json={"errors": [{"message": "rate limited"}]})
        data = {
            "a" + key[1:]: channel_payload(login, int(key[1:]))
            for key, login in variables.items()
        }
        return httpx.Response(200, json={"data": data})

    client, http = make_gql(handler)
    try:
        channels = await client.fetch_channels_batch([f"s{i}" for i in range(30)])
    finally:
        await http.aclose()

    assert len(calls) == 2
    assert len(channels) == 15
    assert all(login in channels for login in [f"s{i}" for i in range(15, 30)])


@pytest.mark.asyncio
async def test_supervisor_poll_cycle_for_40_streamers_uses_two_posts(
    session_factory, tmp_path
) -> None:
    async with session_factory() as session:
        for index in range(40):
            session.add(Streamer(login=f"streamer{index}", display_name=f"s{index}"))
        await session.commit()

    calls: list[list[str]] = []
    client, http = make_gql(batch_handler(calls, unknown=set()))
    config = AppConfig(storage_root=tmp_path, data_dir=tmp_path)
    supervisor = Supervisor(client, SettingsService(ttl=0.0), config=config)
    try:
        await supervisor._poll_once()  # noqa: SLF001
    finally:
        await http.aclose()

    assert len(calls) == 3
    assert [len(chunk) for chunk in calls] == [15, 15, 10]
    assert supervisor.gql_requests_last_cycle == 3
    assert len(supervisor.channel_cache) == 40
    # Der Live-Batch füllt keinen Clip-Cache; das macht der separate Poll.
    assert supervisor.clips_cache == {}
    assert supervisor.cache_version == 1


@pytest.mark.asyncio
async def test_clip_poll_chunks_live_logins_and_keeps_last_cache_on_chunk_error(
    tmp_path,
) -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        variables = payload["variables"]
        logins = list(variables.values())
        calls.append(logins)
        # Erster Chunk fällt aus; alle anderen liefern Clips für jeden Alias.
        if len(calls) == 1:
            return httpx.Response(200, json={"errors": [{"message": "alias limit"}]})
        data = {
            "a" + key[1:]: {
                "id": f"id-{login}",
                "login": login,
                "clips": {
                    "edges": [
                        {
                            "node": {
                                "id": f"clip-{login}",
                                "title": "nice",
                                "createdAt": "2026-11-04T11:00:00Z",
                                "durationSeconds": 30,
                                "viewCount": 5,
                            }
                        }
                    ]
                },
            }
            for key, login in variables.items()
        }
        return httpx.Response(200, json={"data": data})

    client, http = make_gql(handler)
    config = AppConfig(storage_root=tmp_path, data_dir=tmp_path)
    supervisor = Supervisor(client, SettingsService(ttl=0.0), config=config)
    logins = [f"live{i}" for i in range(12)]
    supervisor.channel_cache = {
        login: client_channel(login) for login in logins
    }
    # Ein Kanal ist offline und darf nicht abgefragt werden.
    supervisor.channel_cache["offline"] = client_channel("offline", is_live=False)
    failed_chunk = sorted(logins)[:CLIP_BATCH_SIZE]
    supervisor.clips_cache = {login: [] for login in failed_chunk}
    try:
        await supervisor._clip_poll_once()  # noqa: SLF001
    finally:
        await http.aclose()

    # 12 Live-Logins ⇒ Chunks à CLIP_BATCH_SIZE.
    expected_chunks = [
        min(CLIP_BATCH_SIZE, 12 - start)
        for start in range(0, 12, CLIP_BATCH_SIZE)
    ]
    assert [len(chunk) for chunk in calls] == expected_chunks
    assert all("offline" not in chunk for chunk in calls)
    # Der ausgefallene erste Chunk behält seinen letzten Cache,
    # die übrigen Chunks werden übernommen.
    assert all(supervisor.clips_cache[login] == [] for login in failed_chunk)
    assert supervisor.clips_cache["live3"][0].id == "clip-live3"
    assert supervisor.clips_cache["live9"][0].id == "clip-live9"
    assert "offline" not in supervisor.clips_cache


def client_channel(login: str, is_live: bool = True) -> Channel:
    return Channel(
        id=f"id-{login}",
        login=login,
        display_name=login,
        is_live=is_live,
        stream_id=f"stream-{login}" if is_live else None,
    )


@pytest.mark.asyncio
async def test_partial_gql_response_keeps_present_channels() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        variables = payload["variables"]
        # Nur der erste Alias ist in ``data``; der Rest wird als Fehler gemeldet.
        first_key = next(iter(variables))
        first_login = variables[first_key]
        return httpx.Response(
            200,
            json={
                "data": {"a" + first_key[1:]: channel_payload(first_login, 0)},
                "errors": [{"message": "Alias a1 nicht auflösbar"}],
            },
        )

    client, http = make_gql(handler)
    try:
        channels = await client.fetch_channels_batch(["alpha", "beta"])
    finally:
        await http.aclose()

    assert set(channels) == {"alpha"}
    assert channels["alpha"].login == "alpha"


@pytest.mark.asyncio
async def test_helix_batch_uses_parallel_single_queries() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 3600})
        if request.url.path.endswith("/users"):
            login = request.url.params["login"]
            seen.append(login)
            if login == "unknown":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(
                200,
                json={"data": [{"id": f"id-{login}", "login": login, "display_name": login}]},
            )
        if request.url.path.endswith("/streams"):
            login = request.url.params["user_login"]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": f"stream-{login}",
                            "user_id": f"id-{login}",
                            "user_login": login,
                            "user_name": login,
                            "viewer_count": 7,
                            "started_at": "2026-11-04T10:00:00Z",
                            "game_name": "VALORANT",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/clips"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"message": "unexpected"})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5)
    client = HelixClient("client-id", "secret", http_client=http)
    try:
        client.begin_cycle()
        channels = await client.fetch_channels_batch(["shroud", "unknown", "shroud"])
    finally:
        await http.aclose()

    assert set(channels) == {"shroud"}
    assert channels["shroud"].id == "id-shroud"
    assert channels["shroud"].is_live is True
    assert channels["shroud"].viewers == 7
    assert seen == ["shroud", "unknown"]
    assert client.requests_last_cycle > 0
