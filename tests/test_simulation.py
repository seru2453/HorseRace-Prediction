from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.common.models import PredictionData
from services.data_provider.mock import MockProvider
from services.simulation.monte_carlo import MonteCarloSimulator


def test_simulation_seed_is_reproducible(tmp_path):
    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/test.sqlite3")
    first = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, seed=42, save=False)).simulation
    second = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, seed=42, save=False)).simulation

    assert first.ranking == second.ranking
    assert first.position_distribution == second.position_distribution
    assert first.selection_notes == second.selection_notes
    assert first.selection_notes[0].label == "本命候補"
    assert first.selection_notes[0].horse_number == first.ranking[0]["horse_number"]
    assert first.ranking[0]["simulation_rank"] == 1
    assert first.ranking[0]["win_gap_to_leader"] == 0
    assert 1 <= first.ranking[0]["most_likely_position"] <= len(first.ranking)
    assert first.ranking[0]["most_likely_position_rate"] > 0
    assert "top3_gap_to_cutline" in first.ranking[0]
    assert any(item.label == "注意点" for item in first.selection_notes)
    assert first.decision_summary["label"]
    assert first.decision_summary["favorite_gap"] is not None
    assert first.decision_summary["average_confidence"] is not None
    assert first.candidate_groups["core"]["horses"]
    assert first.candidate_groups["core"]["horses"][0]["horse_number"] == first.ranking[0]["horse_number"]
    assert "opposition" in first.candidate_groups
    assert "reserves" in first.candidate_groups
    assert first.scenario_consensus
    assert first.scenario_consensus[0]["top3_scenario_count"] >= first.scenario_consensus[-1]["top3_scenario_count"]
    assert first.scenario_consensus[0]["scenario_count"] == len(first.scenarios)


def test_simulation_rejects_too_few_iterations(tmp_path):
    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/test.sqlite3")

    try:
        service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=999, save=False))
    except ValueError as exc:
        assert "iterations" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_inside_and_outside_scenarios_do_not_reduce_to_horse_number_order():
    simulator = MonteCarloSimulator()
    predictions = [
        _scenario_prediction(1, 0.15, frame=1, style="closer"),
        _scenario_prediction(2, 0.14, frame=2, style="closer"),
        _scenario_prediction(3, 0.13, frame=3, style="closer"),
        _scenario_prediction(10, 0.35, frame=6, style="front"),
        _scenario_prediction(11, 0.23, frame=6, style="stalker"),
    ]

    scenarios = {item.name: item.top_horse_numbers for item in simulator._scenarios(predictions)}

    assert scenarios["内有利"][0] == 10
    assert scenarios["内有利"] != [1, 2, 3]
    assert scenarios["外有利"] != [11, 10, 3]


def test_scenario_utility_uses_position_pace_and_observed_track_bias():
    simulator = MonteCarloSimulator()
    front = _scenario_prediction(7, 0.20, frame=4, style="front", pressure=0.70)
    closer = _scenario_prediction(8, 0.20, frame=4, style="closer", pressure=0.70)

    assert simulator._scenario_utility(front, 1.0, 1, 8) > simulator._scenario_utility(closer, 1.0, 1, 8)
    assert simulator._scenario_utility(closer, -1.0, 1, 8) > simulator._scenario_utility(front, -1.0, 1, 8)

    positive_bias = closer.model_copy(update={"track_bias_adjustment": 0.02})
    negative_bias = closer.model_copy(update={"track_bias_adjustment": -0.02})
    assert simulator._scenario_utility(positive_bias, 1.0, 1, 8) > simulator._scenario_utility(
        negative_bias, 1.0, 1, 8
    )


def _scenario_prediction(
    horse_number: int,
    probability: float,
    *,
    frame: int,
    style: str,
    pressure: float = 0.45,
) -> PredictionData:
    return PredictionData(
        race_id="scenario-race",
        horse_id=f"horse-{horse_number}",
        horse_number=horse_number,
        horse_name=f"Horse {horse_number}",
        base_score=probability,
        win_probability=probability,
        top2_probability=min(1.0, probability * 2),
        top3_probability=min(1.0, probability * 3),
        uncertainty=0.3,
        confidence=0.7,
        explanation={"positive": [], "negative": [], "score_factors": []},
        model_version="test",
        feature_version="test",
        frame_number=frame,
        running_style=style,
        field_early_pressure=pressure,
    )
