from app.detect.weights import (
    CHAT_PROFILE_WEIGHTS,
    DEFAULT_WEIGHTS,
    NEW_SIGNAL_KEYS,
    active_weights,
    sanitize_weights,
)


def test_default_weights_cover_all_signals():
    assert set(DEFAULT_WEIGHTS) == {
        "chat_rate", "emote_burst", "clip_created", "viewers", "clip_burst",
        "unique_chatters", "hype_keywords", "copypasta", "clip_commands", "streamer_command",
    }
    for key in NEW_SIGNAL_KEYS:
        assert DEFAULT_WEIGHTS[key] == 0.0


def test_chat_profile_sums_to_one():
    assert set(CHAT_PROFILE_WEIGHTS) == set(DEFAULT_WEIGHTS)
    assert abs(sum(CHAT_PROFILE_WEIGHTS.values()) - 1.0) < 1e-9


def test_sanitize_fills_missing_keys_and_falls_back():
    assert sanitize_weights(None) == DEFAULT_WEIGHTS
    filled = sanitize_weights({"chat_rate": 0.5})
    assert filled["chat_rate"] == 0.5
    assert filled["unique_chatters"] == 0.0
    assert sanitize_weights({key: 0.0 for key in DEFAULT_WEIGHTS}) == DEFAULT_WEIGHTS


def test_active_weights_zeroes_new_signals_in_shadow_mode():
    weights = dict(CHAT_PROFILE_WEIGHTS)
    shadow = active_weights(weights, shadow_mode=True)
    assert shadow["chat_rate"] == weights["chat_rate"]
    for key in NEW_SIGNAL_KEYS:
        assert shadow[key] == 0.0
    assert active_weights(weights, shadow_mode=False)["unique_chatters"] == 0.15


def test_sanitize_clamps_to_unit_interval_and_ignores_non_numeric():
    cleaned = sanitize_weights({"chat_rate": 2.5, "emote_burst": -1.0, "viewers": "nope"})
    assert cleaned["chat_rate"] == 1.0
    assert cleaned["emote_burst"] == 0.0
    assert cleaned["viewers"] == DEFAULT_WEIGHTS["viewers"]
