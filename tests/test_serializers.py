from __future__ import annotations

from app.api.serializers import score_breakdown


def test_score_breakdown_has_signal_weight_contribution_and_detail() -> None:
    rows = score_breakdown(
        {
            "components": {"chat_rate": 0.9, "emote_burst": 0.4},
            "weights": {"chat_rate": 0.35, "emote_burst": 0.2},
            "components_detail": {
                "chat_rate": {"value": 12.0, "median": 2.5, "samples": 30}
            },
        }
    )

    assert [row["label"] for row in rows] == ["Chat-Rate", "Emote-Burst"]
    assert rows[0]["weight"] == 0.35
    assert rows[0]["contribution"] == 0.5727
    assert "Wert 12.00" in rows[0]["detail"]
    assert rows[1]["detail"] == ""


def test_score_breakdown_without_trigger_data_is_empty() -> None:
    assert score_breakdown(None) == []
