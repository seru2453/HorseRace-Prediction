from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from database.repository import RaceRepository
from database.session import make_session_factory
from services.common.models import HorseFeature, field
from services.data_provider.mock import MockProvider
from services.features.generator import FeatureGenerator
from services.prediction.config import StabilityConfig
from services.prediction.rule_based import RuleBasedPredictor


def _feature(horse_number: int, *, career_starts: int, first_conditions: bool) -> HorseFeature:
    return HorseFeature(
        race_id="stability-race",
        horse_id=f"horse-{horse_number}",
        horse_number=horse_number,
        horse_name=f"Horse {horse_number}",
        features={
            "horse_number": horse_number,
            "frame_number": horse_number,
            "distance": 1600,
            "career_starts": career_starts,
            "past_5_average_finish": 4.0,
            "past_5_top3_rate": 0.4,
            "first_surface": first_conditions,
            "first_distance": first_conditions,
            "first_racecourse": first_conditions,
            "scratched": False,
        },
        unavailable_features=[],
        as_of="2026-08-30T00:00:00+00:00",
    )


def test_low_sample_condition_rate_is_shrunk_toward_prior():
    generator = FeatureGenerator()

    low_sample = generator._shrunk_rate(1.0, 2)
    sufficient = generator._shrunk_rate(1.0, 20)

    assert 0.30 < low_sample < sufficient < 1.0
    assert generator._sample_status(2) == "LOW_SAMPLE"


def test_unknown_condition_is_neutral_not_zero():
    generator = FeatureGenerator()

    assert generator._shrunk_rate(None, 0) == 0.30
    assert generator._sample_status(0) == "UNKNOWN"


def test_base_ability_shrinks_sparse_results_and_uses_margin_and_opponent_level():
    generator = FeatureGenerator()
    strong_run = {
        "finish_position": 2,
        "field_size": 16,
        "time_behind_winner_seconds": 0.2,
        "race_name": "日本ダービー",
    }

    sparse = generator._ability_profile([strong_run])
    repeated = generator._ability_profile([strong_run] * 5)

    assert 0.5 < sparse["base_ability_index"] < repeated["base_ability_index"] < 1.0
    assert sparse["weighted_margin_score"] > 0.9
    assert sparse["weighted_opponent_score"] == 1.0
    assert generator._ability_profile([])["base_ability_index"] is None


def test_condition_and_group_caps_are_applied():
    config = StabilityConfig(
        condition_adjustment_cap=0.08,
        feature_group_caps={"MARKET": 0.05},
    )
    item = _feature(1, career_starts=10, first_conditions=False)
    item.features.update(
        {
            "win_odds": 1.5,
            "implied_win_probability": 1 / 1.5,
            "field_implied_win_probability_share": 0.5,
            "field_implied_win_probability_rank_ratio": 0.0,
            "popularity": 1,
        }
    )

    detail = RuleBasedPredictor(stability_config=config)._score_detail(item)

    assert detail["feature_group_adjustments"]["MARKET"] == 0.05
    assert abs(detail["condition_adjustment"]) <= 0.08


def test_composite_ability_index_changes_base_ability_without_becoming_condition_adjustment():
    strong = _feature(1, career_starts=10, first_conditions=False)
    weak = _feature(2, career_starts=10, first_conditions=False)
    strong.features["base_ability_index"] = 0.8
    weak.features["base_ability_index"] = 0.2

    strong_detail = RuleBasedPredictor()._score_detail(strong)
    weak_detail = RuleBasedPredictor()._score_detail(weak)

    assert strong_detail["base_ability_score"] > weak_detail["base_ability_score"]
    assert strong_detail["condition_adjustment"] == weak_detail["condition_adjustment"]
    assert any("基礎能力指数ブレンド" in factor for factor in strong_detail["factors"])


def test_new_horse_has_more_uncertainty_than_experienced_horse():
    predictions = RuleBasedPredictor().predict(
        [
            _feature(1, career_starts=0, first_conditions=True),
            _feature(2, career_starts=12, first_conditions=False),
        ]
    )
    by_number = {prediction.horse_number: prediction for prediction in predictions}

    assert by_number[1].uncertainty > by_number[2].uncertainty
    assert by_number[1].confidence_label == "LOW"


def test_probability_score_exponent_modestly_increases_probability_spread():
    strong = _feature(1, career_starts=10, first_conditions=False)
    weak = _feature(2, career_starts=10, first_conditions=False)
    strong.features["base_ability_index"] = 0.9
    weak.features["base_ability_index"] = 0.1

    sharpened = RuleBasedPredictor().predict([strong, weak])
    linear = RuleBasedPredictor(
        stability_config=StabilityConfig(probability_score_exponent=1.0)
    ).predict([strong, weak])

    sharpened_spread = sharpened[0].win_probability - sharpened[-1].win_probability
    linear_spread = linear[0].win_probability - linear[-1].win_probability
    assert sharpened_spread > linear_spread
    assert abs(sum(item.win_probability for item in sharpened) - 1.0) < 0.001


def test_prediction_keeps_stability_breakdown_and_calibrated_probability(tmp_path):
    service = RaceAnalysisService(
        provider=MockProvider(),
        database_url=f"sqlite:///{tmp_path}/stability.sqlite3",
    )
    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))

    assert abs(sum(item.win_probability for item in result.predictions) - 1.0) < 0.001
    for prediction in result.predictions:
        assert prediction.base_ability_score is not None
        assert prediction.ability_index is not None
        assert prediction.ability_components["finish_quality"] is not None
        assert prediction.ability_sample_status in {"LOW_SAMPLE", "SUFFICIENT"}
        assert abs(prediction.condition_adjustment) <= StabilityConfig().condition_adjustment_cap
        assert prediction.base_rank is not None
        assert prediction.final_rank is not None
        assert prediction.rank_delta == prediction.base_rank - prediction.final_rank
        assert 0.0 <= prediction.raw_probability <= 1.0
        assert 0.0 <= prediction.calibrated_probability <= 1.0
        assert prediction.calibration_version != "none"


def test_pedigree_and_track_bias_use_only_saved_prior_results(tmp_path):
    database_url = f"sqlite:///{tmp_path}/pedigree.sqlite3"
    provider = MockProvider()
    analysis = RaceAnalysisService(provider=provider, database_url=database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000)
    )
    result = provider.get_race_results(analysis.race.race_id)
    assert result is not None
    with make_session_factory(database_url)() as session:
        repo = RaceRepository(session)
        repo.save_result(result)
        same_day_pedigree = repo.pedigree_condition_stats(analysis.race, analysis.entries)
        future_race = analysis.race.model_copy(
            update={
                "race_id": "20260816-sapporo-12",
                "race_number": field(12, 1.0, "test"),
            }
        )
        pedigree = repo.pedigree_condition_stats(
            analysis.race.model_copy(update={"race_date": field("2026-08-17", 1.0, "test")}),
            analysis.entries,
        )
        track_bias = repo.track_bias_stats(future_race)

    assert same_day_pedigree[analysis.entries[0].horse.horse_id]["sire_condition_starts"] == 0
    assert pedigree[analysis.entries[0].horse.horse_id]["sire_condition_starts"] == 1
    assert pedigree[analysis.entries[0].horse.horse_id]["sire_condition_top3_rate"] in {0.0, 1.0}
    assert track_bias["race_count"] == 1
    assert track_bias["sample_status"] == "LOW_SAMPLE"
    assert abs(FeatureGenerator()._track_bias_adjustment(track_bias["raw_bias"], 1, 1)) < 0.01
