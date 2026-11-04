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


def test_mark_triggered_starts_cooldown_for_manual_trigger() -> None:
    detector = Detector(threshold=0.60, cooldown_s=180, pre_roll_s=240, weights=WEIGHTS)
    detector.mark_triggered(NOW)
    assert 179 <= detector.cooldown_remaining(NOW + timedelta(seconds=1)) <= 180
    detector.update(components(0.9), now=NOW + timedelta(seconds=2), buffer_s=300)
    blocked = detector.update(components(0.9), now=NOW + timedelta(seconds=7), buffer_s=300)
    assert not blocked.trigger
    assert blocked.cooldown_remaining_s > 0


def test_required_ticks_one_triggers_on_first_tick():
    detector = Detector(threshold=0.40, required_ticks=1, weights=WEIGHTS)
    decision = detector.update(components(0.9), now=NOW, buffer_s=300)
    assert decision.trigger


def test_hourly_cap_blocks_trigger():
    detector = Detector(threshold=0.40, required_ticks=1, max_clips_per_hour=2, weights=WEIGHTS)
    detector.update(components(0.9), now=NOW, buffer_s=300, recent_triggers=2)
    allowed, reasons = detector.can_trigger(NOW, buffer_s=300, recent_triggers=2)
    assert not allowed
    assert any("Stunde" in reason for reason in reasons)


def test_component_labels_cover_all_ten_signals():
    from app.detect.detector import COMPONENT_LABELS
    from app.detect.weights import DEFAULT_WEIGHTS

    assert set(COMPONENT_LABELS) == set(DEFAULT_WEIGHTS)
    assert COMPONENT_LABELS["unique_chatters"] == "Unique Chatter"
    assert COMPONENT_LABELS["hype_keywords"] == "Hype-Keywords"
    assert COMPONENT_LABELS["copypasta"] == "Copypasta"
    assert COMPONENT_LABELS["clip_commands"] == "Clip-Kommandos"
    assert COMPONENT_LABELS["streamer_command"] == "Streamer/Mod-Kommando"


def test_configure_updates_required_ticks_and_hourly_cap():
    detector = Detector(threshold=0.60, weights=WEIGHTS)
    detector.configure(required_ticks=3, max_clips_per_hour=5)
    snapshot = detector.snapshot()
    assert snapshot["required_ticks"] == 3
    assert snapshot["max_clips_per_hour"] == 5
    assert snapshot["triggers_last_hour"] == 0


def test_can_trigger_reports_cooldown_and_buffer_blockers():
    detector = Detector(threshold=0.60, cooldown_s=180, pre_roll_s=240, weights=WEIGHTS)
    detector.mark_triggered(NOW)
    allowed, reasons = detector.can_trigger(NOW + timedelta(seconds=5), buffer_s=100)
    assert not allowed
    assert any("Cooldown" in reason for reason in reasons)
    assert any("Puffer" in reason for reason in reasons)


def test_can_trigger_allows_when_all_constraints_are_met():
    detector = Detector(
        threshold=0.60,
        cooldown_s=180,
        pre_roll_s=240,
        max_clips_per_hour=6,
        weights=WEIGHTS,
    )
    assert detector.can_trigger(NOW, buffer_s=300, recent_triggers=5) == (True, [])


def test_update_uses_recent_triggers_and_reports_them_in_snapshot():
    detector = Detector(
        threshold=0.40,
        required_ticks=1,
        max_clips_per_hour=2,
        weights=WEIGHTS,
    )
    decision = detector.update(components(0.9), now=NOW, buffer_s=300, recent_triggers=2)
    assert not decision.trigger
    assert any("Stunde" in note for note in decision.notes)
    assert detector.snapshot()["triggers_last_hour"] == 2
