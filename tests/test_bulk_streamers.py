from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.streamers import parse_bulk_input
from app.api import streamers as streamers_api
from app.models import Streamer
from app.twitch.types import Channel, TwitchUnavailable


class FakeTwitch:
    def __init__(self, channels: dict[str, Channel] | None = None, error: Exception | None = None) -> None:
        self.channels = channels or {}
        self.error = error
        self.calls: list[list[str]] = []

    async def fetch_channels_batch(self, logins: list[str]) -> dict[str, Channel]:
        self.calls.append(list(logins))
        if self.error is not None:
            raise self.error
        return {login: self.channels[login] for login in logins if login in self.channels}


class FakeSupervisor:
    def __init__(self) -> None:
        self.kicks = 0

    async def kick(self) -> None:
        self.kicks += 1


def make_app(twitch: FakeTwitch, supervisor: FakeSupervisor) -> FastAPI:
    app = FastAPI()
    app.include_router(streamers_api.router)
    app.state.twitch = twitch
    app.state.supervisor = supervisor
    return app


async def post_bulk(app: FastAPI, text: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/streamers/bulk", json={"input": text})


async def count_streamers(session_factory) -> int:
    async with session_factory() as session:
        return int((await session.execute(select(func.count(Streamer.id)))).scalar() or 0)


def channel(login: str) -> Channel:
    return Channel(id=f"id-{login}", login=login, display_name=f"Display {login}")


# -- Parser ----------------------------------------------------------------
def test_parser_accepts_urls_hashes_and_plain_logins() -> None:
    entries = parse_bulk_input(
        "https://www.twitch.tv/Foo\n"
        "http://twitch.tv/bar?x=1\n"
        "twitch.tv/baz/videos\n"
        "www.twitch.tv/qux/videos\n"
        "#Some_One;@Other\n"
        "plain"
    )
    assert [login for _original, login in entries] == [
        "foo", "bar", "baz", "qux", "some_one", "other", "plain",
    ]


def test_parser_deduplicates_case_insensitive() -> None:
    entries = parse_bulk_input("Foo foo #FOO https://twitch.tv/foo, @foo")
    assert len(entries) == 1
    assert entries[0][1] == "foo"


def test_parser_marks_invalid_entries() -> None:
    entries = parse_bulk_input("bad-name !!! ab.cd https://example.com/foo")
    assert entries
    assert all(login is None for _original, login in entries)


def test_parser_splits_commas_semicolons_and_whitespace() -> None:
    entries = parse_bulk_input("alpha,beta;gamma delta")
    assert [login for _original, login in entries] == ["alpha", "beta", "gamma", "delta"]


# -- Bulk-Endpunkt ---------------------------------------------------------
@pytest.mark.asyncio
async def test_40_valid_lines_use_one_bulk_call_and_two_batches(session_factory) -> None:
    logins = [f"user{index:02d}" for index in range(40)]
    twitch = FakeTwitch({login: channel(login) for login in logins})
    supervisor = FakeSupervisor()
    app = make_app(twitch, supervisor)

    response = await post_bulk(app, "\n".join(f"https://twitch.tv/{login}" for login in logins))

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"] == {"added": 40, "duplicate": 0, "not_found": 0, "invalid": 0}
    assert len(payload["items"]) == 40
    assert all(item["status"] == "added" for item in payload["items"])
    # Ein Bulk-Aufruf, intern höchstens 15 Logins je Twitch-Batch.
    assert [len(chunk) for chunk in twitch.calls] == [15, 15, 10]
    assert await count_streamers(session_factory) == 40
    assert supervisor.kicks == 1


@pytest.mark.asyncio
async def test_duplicate_not_found_and_contract_shape(session_factory) -> None:
    async with session_factory() as session:
        session.add(Streamer(login="alpha", display_name="Alpha", enabled=True))
        await session.commit()

    twitch = FakeTwitch({"known": channel("known")})
    supervisor = FakeSupervisor()
    app = make_app(twitch, supervisor)

    response = await post_bulk(app, "alpha\nknown\nunknown\nhttps://twitch.tv/known")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"counts", "items"}
    assert set(payload["counts"]) == {"added", "duplicate", "not_found", "invalid"}
    assert payload["counts"] == {"added": 1, "duplicate": 1, "not_found": 1, "invalid": 0}
    assert set(payload["items"][0]) == {"input", "login", "status", "detail"}
    assert [item["status"] for item in payload["items"]] == [
        "duplicate", "added", "not_found",
    ]
    # Der doppelte URL-Eintrag wurde vorher dedupliziert.
    assert len(payload["items"]) == 3
    assert await count_streamers(session_factory) == 2


@pytest.mark.asyncio
async def test_invalid_entries_are_reported_without_twitch_call(session_factory) -> None:
    twitch = FakeTwitch()
    supervisor = FakeSupervisor()
    app = make_app(twitch, supervisor)

    response = await post_bulk(app, "bad-name !!!")

    assert response.status_code == 200
    payload = response.json()
    assert payload["counts"]["invalid"] == 2
    assert [item["login"] for item in payload["items"]] == [None, None]
    assert twitch.calls == []
    assert await count_streamers(session_factory) == 0


@pytest.mark.asyncio
async def test_more_than_200_entries_returns_400(session_factory) -> None:
    twitch = FakeTwitch()
    supervisor = FakeSupervisor()
    app = make_app(twitch, supervisor)
    lines = "\n".join(f"u{index}" for index in range(201))

    response = await post_bulk(app, lines)

    assert response.status_code == 400
    assert "200" in response.json()["detail"]
    assert twitch.calls == []
    assert await count_streamers(session_factory) == 0


@pytest.mark.asyncio
async def test_oversized_input_is_rejected(session_factory) -> None:
    twitch = FakeTwitch()
    supervisor = FakeSupervisor()
    app = make_app(twitch, supervisor)

    response = await post_bulk(app, "a" * 65537)

    assert response.status_code == 422
    assert twitch.calls == []
    assert await count_streamers(session_factory) == 0


@pytest.mark.asyncio
async def test_twitch_failure_returns_502_without_writing(session_factory) -> None:
    twitch = FakeTwitch(error=TwitchUnavailable("Twitch antwortet nicht"))
    supervisor = FakeSupervisor()
    app = make_app(twitch, supervisor)

    response = await post_bulk(app, "alpha\nbeta")

    assert response.status_code == 502
    assert "Twitch nicht erreichbar" in response.json()["detail"]
    assert await count_streamers(session_factory) == 0
    assert supervisor.kicks == 0
