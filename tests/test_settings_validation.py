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
