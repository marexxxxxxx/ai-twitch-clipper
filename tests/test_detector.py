from __future__ import annotations

from datetime import datetime, timedelta

from app.detect.detector import Detector, describe_trigger
from app.detect.signals import fuse, sanitize_weights

NOW = datetime(2026, 11, 4, 12, 0, 0)
WEIGHTS = {
    "chat_rate": 1.0,
    "emote_burst": 0.0,
    "clip_created": 0.0,
    "viewers": 0.0,
    "clip_burst": 0.0,
}


def components(value: float) -> dict[str, float]:
    return {
        "chat_rate": value,
        "emote_burst": value,
        "clip_created": value,
        "viewers": value,
        "clip_burst": value,
    }


def test_fuse_normalizes_weights() -> None:
    weights = {"chat_rate": 1.0, "emote_burst": 0.0, "clip_created": 0.0, "viewers": 0.0, "clip_burst": 0.0}
    assert fuse(components(1.0), weights) == 1.0
    assert fuse(components(0.5), weights) == 0.5


def test_fuse_clip_burst_adds_weight() -> None:
    weights = sanitize_weights(None)
    only_burst = {key: 0.0 for key in weights}
    only_burst["clip_burst"] = 1.0
    score = fuse(only_burst, weights)
    assert abs(score - (0.10 / 1.10)) < 1e-9


def test_hysteresis_requires_two_consecutive_ticks() -> None:
    detector = Detector(threshold=0.60, cooldown_s=180, pre_roll_s=240, weights=WEIGHTS)
    first = detector.update(components(0.9), now=NOW, buffer_s=300)
    assert not first.trigger
    assert first.consecutive == 1

    second = detector.update(components(0.9), now=NOW + timedelta(seconds=5), buffer_s=300)
    assert second.trigger
    assert second.score == 0.9


def test_low_score_resets_hysteresis() -> None:
    detector = Detector(threshold=0.60, pre_roll_s=240, weights=WEIGHTS)
    detector.update(components(0.9), now=NOW, buffer_s=300)
    detector.update(components(0.2), now=NOW + timedelta(seconds=5), buffer_s=300)
    decision = detector.update(components(0.9), now=NOW + timedelta(seconds=10), buffer_s=300)
    assert not decision.trigger
    assert decision.consecutive == 1


def test_buffer_must_cover_pre_roll() -> None:
    detector = Detector(threshold=0.60, pre_roll_s=240, weights=WEIGHTS)
    detector.update(components(0.9), now=NOW, buffer_s=100)
    decision = detector.update(components(0.9), now=NOW + timedelta(seconds=5), buffer_s=100)
    assert not decision.trigger
    assert any("Puffer" in note for note in decision.notes)


def test_cooldown_starts_with_trigger() -> None:
    detector = Detector(threshold=0.60, cooldown_s=180, pre_roll_s=240, weights=WEIGHTS)
    t0 = NOW
    detector.update(components(0.9), now=t0, buffer_s=300)
    assert detector.update(components(0.9), now=t0 + timedelta(seconds=5), buffer_s=300).trigger
    triggered_at = t0 + timedelta(seconds=5)

    detector.update(components(0.9), now=triggered_at + timedelta(seconds=5), buffer_s=300)
    blocked = detector.update(components(0.9), now=triggered_at + timedelta(seconds=10), buffer_s=300)
    assert not blocked.trigger
    assert blocked.cooldown_remaining_s > 0

    # Hysterese zurücksetzen, dann nach Ablauf des Cooldowns erneut zwei Ticks.
    detector.update(components(0.0), now=triggered_at + timedelta(seconds=15), buffer_s=300)
    later = triggered_at + timedelta(seconds=181)
    detector.update(components(0.9), now=later, buffer_s=300)
    assert detector.update(components(0.9), now=later + timedelta(seconds=5), buffer_s=300).trigger


def test_zero_weight_components_do_not_contribute() -> None:
    weights = {"chat_rate": 1.0, "emote_burst": 0.0, "clip_created": 0.0, "viewers": 0.0, "clip_burst": 0.0}
    detector = Detector(threshold=0.60, weights=weights)
    only_emotes = {key: 0.0 for key in weights}
    only_emotes["emote_burst"] = 1.0
    decision = detector.update(only_emotes, now=NOW, buffer_s=300)
    assert decision.score == 0.0
    assert not decision.trigger


def test_trigger_reason_names_components() -> None:
    reason = describe_trigger(components(1.0), sanitize_weights(None))
    assert "Chat-Rate" in reason
    assert "Neuer Clip" in reason
