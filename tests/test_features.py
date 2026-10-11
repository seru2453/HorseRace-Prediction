from pathlib import Path

from services.features.generator import FeatureGenerator
from services.common.models import field
from services.parser.race_parser import RaceParser


def test_jra_histories_feed_feature_generation():
    parsed = RaceParser().parse_entries_html(Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8"))
    parsed.entries[1].career_win_rate = field(0.05, 0.8, "test")
    parsed.entries[1].career_top3_rate = field(0.10, 0.8, "test")
    parsed.entries[1].total_prize_money = field(10_000_000, 0.8, "test")
    parsed.entries[1].win_odds = field(4.1, 0.8, "test")
    histories = parsed.histories or {}
    features = FeatureGenerator().generate(parsed.race, parsed.entries, histories)
    first = features[0].features

    assert first["course_frame_bias"] == 0.04
    assert first["course_frame_bias_label"] == "inner_favorable"
    assert first["age"] == 7
    assert first["age_band"] == "veteran"
    assert first["past_5_average_finish"] == 7.5
    assert first["past_5_win_rate"] == 0.0
    assert first["past_5_top3_rate"] == 0.5
    assert first["past_5_bad_finish_rate"] == 0.5
    assert first["recent_3_average_finish"] == 7.5
    assert first["last_run_finish_position"] == 3
    assert first["last_run_finish_ratio"] == 3 / 16
    assert first["last_run_top3"] is True
    assert first["last_run_top_half"] is True
    assert first["recent_finish_range"] == 9
    assert first["recent_consistency_label"] == "volatile"
    assert first["past_5_average_finish_ratio"] == 3 / 16
    assert first["past_5_top_half_rate"] == 1.0
    assert first["weighted_recent_finish_ratio"] == 3 / 16
    assert first["weighted_recent_top3_rate"] == 5 / 9
    assert first["past_5_average_final_three_furlongs"] == 35.4
    assert first["distance_band"] == "middle"
    assert first["same_distance_top3_rate"] == 0.5
    assert first["same_distance_band_top3_rate"] == 0.5
    assert first["same_surface_top3_rate"] == 0.5
    assert first["same_surface_distance_top3_rate"] == 0.5
    assert first["same_surface_distance_band_top3_rate"] == 0.5
    assert first["days_since_last_run"] == 35
    assert first["recent_run_count_90d"] == 2
    assert first["carried_weight_change_from_last"] == 4.0
    assert first["distance_change"] == "same"
    assert first["distance_change_meters"] == 0
    assert first["distance_change_abs"] == 0
    assert first["surface_change"] == "same"
    assert first["win_odds"] == 12.3
    assert first["popularity"] == 4
    assert round(first["implied_win_probability"], 4) == 0.0813
    assert first["field_implied_win_probability_rank_ratio"] == 1.0
    assert round(first["field_implied_win_probability_share"], 4) == 0.25
    assert first["career_starts"] == 22
    assert first["career_wins"] == 3
    assert round(first["career_win_rate"], 4) == 0.1364
    assert round(first["career_top3_rate"], 4) == 0.3182
    assert first["total_prize_money"] == 123456000
    assert first["field_total_prize_money_rank_ratio"] == 0.0
    assert first["field_career_win_rate_rank_ratio"] == 0.0
    assert first["field_career_top3_rate_rank_ratio"] == 0.0
    assert first["running_style"] == "closer"
    assert round(first["average_first_corner_ratio"], 4) == 0.875
    assert first["average_position_gain"] == 7.0
    assert first["field_early_pressure"] == 0.0
    assert first["average_recent_popularity"] == 15.0
    assert first["market_overperformance_rate"] == 1.0
    assert first["longshot_top3_rate"] == 1.0
    assert first["recent_graded_starts"] == 2
    assert first["recent_graded_top3_rate"] == 0.5
    assert first["latest_race_grade_level"] == 2
    assert first["current_race_grade_level"] == 3
    assert first["grade_step_from_last"] == 1
    assert round(first["average_recent_speed_mps"], 4) == 16.8776
    assert round(first["best_recent_speed_mps"], 4) == 16.8776
    assert round(first["average_adjusted_speed_delta_mps"], 4) == 0.4376
    assert round(first["weighted_adjusted_speed_delta_mps"], 4) == 0.4376
    assert round(first["best_adjusted_speed_delta_mps"], 4) == 0.4376
    assert 0.5 < first["base_ability_index"] < 0.52
    assert first["ability_sample_count"] == 2
    assert first["ability_sample_status"] == "LOW_SAMPLE"
    assert first["weighted_margin_score"] is None
    assert first["weighted_opponent_score"] == 0.6
    assert first["weighted_track_adjusted_speed_score"] > 0.8
    assert first["latest_recent_horse_weight"] == 468
    assert first["average_recent_horse_weight"] == 472.0
    assert first["latest_recent_horse_weight_change"] == -8


def test_track_condition_feeds_feature_generation():
    parsed = RaceParser().parse_entries_html(Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8"))
    parsed.race.track_condition = field("稍重", 0.8, "test")
    histories = parsed.histories or {}
    features = FeatureGenerator().generate(parsed.race, parsed.entries, histories)

    assert features[0].features["same_track_condition_top3_rate"] == 1.0


def test_long_distance_aptitude_is_separate_from_recent_form():
    parsed = RaceParser().parse_entries_html(Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8"))
    entry = parsed.entries[0]
    horse_id = entry.horse.horse_id
    recent = [
        {"race_date": f"2026-08-{15 - index:02d}", "finish_position": 9, "field_size": 18,
         "distance": 1600, "surface": "芝", "racecourse": "東京"}
        for index in range(5)
    ]
    long_history = recent + [
        {"race_date": f"2026-07-{20 - index:02d}", "finish_position": 2, "field_size": 16,
         "distance": 2000, "surface": "芝", "racecourse": "札幌"}
        for index in range(10)
    ]

    feature = FeatureGenerator().generate(
        parsed.race,
        [entry],
        {horse_id: recent},
        aptitude_histories={horse_id: long_history},
    )[0].features

    assert feature["past_5_average_finish"] == 9.0
    assert feature["aptitude_history_count"] == 15
    assert feature["exact_distance_starts"] == 10
    assert feature["exact_distance_top3_rate"] == 1.0
    assert feature["same_racecourse_near_distance_starts"] == 10
    assert feature["preferred_distance_center"] == 2000.0
    assert feature["near_distance_sample_status"] == "SUFFICIENT"


def test_racecourse_feeds_feature_generation():
    parsed = RaceParser().parse_entries_html(Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8"))
    parsed.race.racecourse = field("福島", 0.8, "test")
    histories = parsed.histories or {}
    features = FeatureGenerator().generate(parsed.race, parsed.entries, histories)

    assert features[0].features["same_racecourse_top3_rate"] == 1.0
    assert features[0].features["same_racecourse_distance_top3_rate"] == 1.0


def test_same_jockey_feeds_feature_generation():
    parsed = RaceParser().parse_entries_html(Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8"))
    parsed.entries[0].jockey_name = field("吉田 豊", 0.8, "test")
    histories = parsed.histories or {}
    features = FeatureGenerator().generate(parsed.race, parsed.entries, histories)

    assert features[0].features["same_jockey_top3_rate"] == 1.0
    assert features[0].features["same_jockey_as_last_run"] is True


def test_last_run_finish_is_used_without_field_size():
    parsed = RaceParser().parse_entries_html(Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8"))
    histories = parsed.histories or {}
    histories[parsed.entries[0].horse.horse_id][0]["field_size"] = None
    features = FeatureGenerator().generate(parsed.race, parsed.entries, histories)
    first = features[0]

    assert first.features["last_run_finish_position"] == 3
    assert first.features["last_run_top3"] is True
    assert first.features["last_run_finish_ratio"] is None
    assert first.features["last_run_top_half"] is None
    assert "last_run_finish_position" not in first.unavailable_features
    assert "last_run_top3" not in first.unavailable_features
    assert "last_run_finish_ratio" in first.unavailable_features
    assert "last_run_top_half" in first.unavailable_features


def test_new_horse_has_unknown_form_residual_and_rebound():
    parsed = RaceParser().parse_entries_html(Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8"))
    horse_id = parsed.entries[0].horse.horse_id
    features = FeatureGenerator().generate(parsed.race, parsed.entries, {horse_id: []})
    first = next(item for item in features if item.horse_id == horse_id)

    assert first.features["current_form"] is None
    assert first.features["previous_race_actual_performance"] is None
    assert first.features["previous_race_residual"] is None
    assert first.features["rebound_score"] is None
