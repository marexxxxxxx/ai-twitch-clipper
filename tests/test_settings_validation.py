from __future__ import annotations

import pytest

from app.runtime_settings import SettingsValidationError, validate_updates


@pytest.mark.parametrize("value", ["720p60,best", "audio_only", "worst", "1080p60,720p60,best"])
async def test_quality_accepts_streamlink_names(value: str) -> None:
    clean = await validate_updates({"quality": value})
    assert clean["quality"] == value


@pytest.mark.parametrize(
    "value",
    [
        "--record=/x",
        "best,--output=/x",
        "720p60, best",
        "",
        "   ",
        "best;rm -rf /",
        "720p60,best/720p",
        "..",
        "best..x",
    ],
)
async def test_quality_rejects_argument_injection(value: str) -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({"quality": value})


@pytest.mark.parametrize("pre,post", [(600, 60), (360, 60)])
async def test_window_rejects_more_than_ring_buffer(pre: int, post: int) -> None:
    with pytest.raises(SettingsValidationError) as excinfo:
        await validate_updates({"pre_roll_s": pre, "post_roll_s": post})
    assert "360" in str(excinfo.value)


async def test_window_rejects_pre_roll_alone_too_large() -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({"pre_roll_s": 600})


@pytest.mark.parametrize("pre,post", [(300, 60), (240, 60)])
async def test_window_accepts_fitting_values(pre: int, post: int) -> None:
    clean = await validate_updates({"pre_roll_s": pre, "post_roll_s": post})
    assert clean == {"pre_roll_s": pre, "post_roll_s": post}


async def test_window_partial_update_uses_current_values() -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({"post_roll_s": 120}, current={"pre_roll_s": 300, "post_roll_s": 60})
    clean = await validate_updates({"post_roll_s": 60}, current={"pre_roll_s": 300, "post_roll_s": 30})
    assert clean == {"post_roll_s": 60}


@pytest.mark.parametrize("pre,post", [(20, 10), (240, 5)])
async def test_window_rejects_below_minimum(pre: int, post: int) -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({"pre_roll_s": pre, "post_roll_s": post})


@pytest.mark.parametrize("value", [1, 2, 5])
async def test_required_ticks_accepts_range(value: int) -> None:
    clean = await validate_updates({"required_ticks": value})
    assert clean == {"required_ticks": value}


@pytest.mark.parametrize("value", [0, 6, -1, "x"])
async def test_required_ticks_rejects_out_of_range(value: object) -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({"required_ticks": value})


@pytest.mark.parametrize("value", [1, 6, 60])
async def test_max_clips_per_hour_accepts_range(value: int) -> None:
    clean = await validate_updates({"max_clips_per_hour": value})
    assert clean == {"max_clips_per_hour": value}


@pytest.mark.parametrize("value", [0, 61, -1, "x"])
async def test_max_clips_per_hour_rejects_out_of_range(value: object) -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({"max_clips_per_hour": value})


@pytest.mark.parametrize("key", ["shadow_mode", "streamer_command_triggers"])
@pytest.mark.parametrize("value", [True, False])
async def test_chat_toggles_accept_only_real_booleans(key: str, value: bool) -> None:
    clean = await validate_updates({key: value})
    assert clean == {key: value}


@pytest.mark.parametrize("key", ["shadow_mode", "streamer_command_triggers"])
@pytest.mark.parametrize("value", ["yes", "true", 1, 0, None])
async def test_chat_toggles_reject_non_booleans(key: str, value: object) -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({key: value})


async def test_hype_keywords_trims_and_allows_empty() -> None:
    assert await validate_updates({"hype_keywords": "  geil, lol  "}) == {
        "hype_keywords": "geil, lol"
    }
    assert await validate_updates({"hype_keywords": ""}) == {"hype_keywords": ""}
    assert await validate_updates({"hype_keywords": "x" * 500}) == {"hype_keywords": "x" * 500}


@pytest.mark.parametrize("value", [2, 5, 10])
async def test_copypasta_min_users_accepts_range(value: int) -> None:
    clean = await validate_updates({"copypasta_min_users": value})
    assert clean == {"copypasta_min_users": value}


@pytest.mark.parametrize("value", [0, 1, 11, "x"])
async def test_copypasta_min_users_rejects_out_of_range(value: object) -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({"copypasta_min_users": value})


@pytest.mark.parametrize("value", [5, 10, 30])
async def test_copypasta_window_accepts_range(value: int) -> None:
    clean = await validate_updates({"copypasta_window_s": value})
    assert clean == {"copypasta_window_s": value}


@pytest.mark.parametrize("value", [4, 31, -1, "x"])
async def test_copypasta_window_rejects_out_of_range(value: object) -> None:
    with pytest.raises(SettingsValidationError):
        await validate_updates({"copypasta_window_s": value})
