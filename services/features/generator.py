from __future__ import annotations

from datetime import datetime, timezone

from services.common.models import HorseFeature, RaceData, RaceEntryData
from services.ml.targets import relative_finish
from services.prediction.config import DEFAULT_STABILITY_CONFIG

FEATURE_VERSION = "mvp-feature-v33"

GRADE_LEVELS = {
    "G1": 4,
    "Jpn1": 4,
    "G2": 3,
    "Jpn2": 3,
    "G3": 2,
    "Jpn3": 2,
    "L": 1,
    "OP": 1,
}

KNOWN_G1_RACES = {
    "有馬記念",
    "日本ダービー",
    "東京優駿",
    "皐月賞",
    "菊花賞",
    "天皇賞",
    "ジャパンC",
    "ジャパンカップ",
    "大阪杯",
    "宝塚記念",
    "安田記念",
    "マイルCS",
    "スプリンターズS",
    "高松宮記念",
    "ヴィクトリアM",
    "エリザベス女王杯",
    "秋華賞",
    "桜花賞",
    "オークス",
    "優駿牝馬",
    "ホープフルS",
    "朝日杯FS",
    "阪神JF",
    "フェブラリーS",
    "チャンピオンズC",
}

KNOWN_G2_RACES = {
    "札幌記念",
    "金鯱賞",
    "京都記念",
    "中山記念",
    "毎日王冠",
    "オールカマー",
    "日経賞",
    "阪神大賞典",
    "京都大賞典",
    "アルゼンチン共和国杯",
    "セントライト記念",
    "神戸新聞杯",
    "ローズS",
    "フィリーズレビュー",
    "スワンS",
    "阪神C",
    "府中牝馬S",
}

KNOWN_G3_RACES = {
    "七夕賞",
    "函館記念",
    "小倉記念",
    "新潟記念",
    "中山金杯",
    "京都金杯",
    "東京新聞杯",
    "共同通信杯",
    "きさらぎ賞",
    "毎日杯",
    "アーリントンC",
    "ユニコーンS",
    "エプソムC",
    "関屋記念",
    "新潟2歳S",
    "札幌2歳S",
    "シンザン記念",
    "フェアリーS",
    "クイーンS",
    "福島牝馬S",
    "中山牝馬S",
    "愛知杯",
    "根岸S",
    "武蔵野S",
    "プロキオンS",
    "みやこS",
    "カペラS",
}


class FeatureGenerator:
    def generate(
        self,
        race: RaceData,
        entries: list[RaceEntryData],
        histories: dict[str, list] | None = None,
        aptitude_histories: dict[str, list] | None = None,
        pedigree_stats: dict[str, dict] | None = None,
        track_bias: dict | None = None,
    ) -> list[HorseFeature]:
        histories = histories or {}
        aptitude_histories = aptitude_histories or {}
        pedigree_stats = pedigree_stats or {}
        track_bias = track_bias or {}
        generated: list[HorseFeature] = []
        running_profiles = {
            entry.horse.horse_id: self._running_profile(histories.get(entry.horse.horse_id, []))
            for entry in entries
        }
        field_early_pressure = self._field_early_pressure(list(running_profiles.values()))
        field_prize_rank_ratios = self._field_rank_ratios(entries, lambda entry: entry.total_prize_money.value)
        field_career_win_rank_ratios = self._field_rank_ratios(entries, lambda entry: entry.career_win_rate.value)
        field_career_top3_rank_ratios = self._field_rank_ratios(entries, lambda entry: entry.career_top3_rate.value)
        field_implied_win_rank_ratios = self._field_rank_ratios(entries, lambda entry: self._implied_win_probability(entry.win_odds.value))
        field_implied_win_shares = self._field_value_shares(entries, lambda entry: self._implied_win_probability(entry.win_odds.value))
        for entry in entries:
            history = histories.get(entry.horse.horse_id, [])
            aptitude_history = aptitude_histories.get(entry.horse.horse_id) or history
            unavailable = []
            recent_finishes = [item.get("finish_position") for item in history if item.get("finish_position") is not None][:5]
            recent_f3 = [item.get("final_three_furlongs") for item in history if item.get("final_three_furlongs") is not None][:5]
            consistency_profile = self._consistency_profile(recent_finishes)
            finish_ratio_profile = self._finish_ratio_profile(history)
            weighted_recent_profile = self._weighted_recent_profile(history)
            grade_profile = self._grade_profile(history, race.race_name.value, race.grade.value)
            speed_profile = self._speed_profile(history)
            ability_profile = self._ability_profile(history)
            current_form = self._current_form(history)
            body_weight_profile = self._body_weight_profile(history)
            last_run = history[0] if history else {}
            last_run_profile = self._last_run_profile(last_run)
            days_since_last_run = self._days_between(last_run.get("race_date"), race.race_date.value)
            recent_run_count_90d = self._recent_run_count(history, race.race_date.value, 90)
            if not recent_finishes:
                unavailable.append("past_5_average_finish")
                unavailable.append("past_5_win_rate")
                unavailable.append("past_5_top3_rate")
                unavailable.append("past_5_bad_finish_rate")
                unavailable.append("recent_3_average_finish")
                unavailable.append("recent_finish_range")
                unavailable.append("recent_consistency_label")
            if finish_ratio_profile["past_5_average_finish_ratio"] is None:
                unavailable.append("past_5_average_finish_ratio")
                unavailable.append("past_5_top_half_rate")
            if weighted_recent_profile["weighted_recent_finish_ratio"] is None:
                unavailable.append("weighted_recent_finish_ratio")
                unavailable.append("weighted_recent_top3_rate")
            if not recent_f3:
                unavailable.append("past_5_average_final_three_furlongs")
            if not last_run.get("race_date"):
                unavailable.append("days_since_last_run")
                unavailable.append("recent_run_count_90d")
            for key in (
                "last_run_finish_position",
                "last_run_finish_ratio",
                "last_run_top3",
                "last_run_top_half",
            ):
                if last_run_profile[key] is None:
                    unavailable.append(key)
            if entry.win_odds.value is None:
                unavailable.append("win_odds")
                unavailable.append("implied_win_probability")
            if entry.popularity.value is None:
                unavailable.append("popularity")
            if entry.age.value is None:
                unavailable.append("age")
                unavailable.append("age_band")
            if entry.career_starts.value is None:
                unavailable.append("career_starts")
            if entry.career_win_rate.value is None:
                unavailable.append("career_win_rate")
            if entry.career_top3_rate.value is None:
                unavailable.append("career_top3_rate")
            if entry.total_prize_money.value is None:
                unavailable.append("total_prize_money")
            if entry.carried_weight.value is None or last_run.get("carried_weight") is None:
                unavailable.append("carried_weight_change_from_last")
            if last_run.get("distance") is None:
                unavailable.append("distance_change")
                unavailable.append("distance_change_meters")
                unavailable.append("distance_change_abs")
            if last_run.get("surface") is None:
                unavailable.append("surface_change")
            current_distance_band = self._distance_band(race.distance.value)
            distance_aptitude = self._distance_aptitude_profile(
                aptitude_history,
                race.distance.value,
                race.racecourse.value,
                race.surface.value,
            )
            course_frame_bias, course_frame_bias_label = self._course_frame_bias(
                race.racecourse.value,
                race.surface.value,
                race.distance.value,
                entry.frame_number.value,
            )
            distance_match_top3 = self._condition_top3_rate(history, "distance", race.distance.value)
            distance_band_match_top3 = self._distance_band_top3_rate(history, current_distance_band)
            surface_match_top3 = self._condition_top3_rate(history, "surface", race.surface.value)
            track_condition_match_top3 = self._condition_top3_rate(history, "track_condition", race.track_condition.value)
            racecourse_match_top3 = self._condition_top3_rate(history, "racecourse", race.racecourse.value)
            surface_distance_match_top3 = self._multi_condition_top3_rate(
                history,
                {"surface": race.surface.value, "distance": race.distance.value},
            )
            surface_distance_band_match_top3 = self._surface_distance_band_top3_rate(
                history,
                race.surface.value,
                current_distance_band,
            )
            racecourse_distance_match_top3 = self._multi_condition_top3_rate(
                history,
                {"racecourse": race.racecourse.value, "distance": race.distance.value},
            )
            same_jockey_top3 = self._condition_top3_rate(history, "jockey_name", entry.jockey_name.value)
            same_jockey_as_last = self._same_jockey_as_last_run(last_run, entry.jockey_name.value)
            condition_counts = {
                "same_distance": self._condition_sample_count(history, {"distance": race.distance.value}),
                "same_distance_band": self._distance_band_sample_count(history, current_distance_band),
                "same_surface": self._condition_sample_count(history, {"surface": race.surface.value}),
                "same_track_condition": self._condition_sample_count(history, {"track_condition": race.track_condition.value}),
                "same_racecourse": self._condition_sample_count(history, {"racecourse": race.racecourse.value}),
                "same_surface_distance": self._condition_sample_count(
                    history, {"surface": race.surface.value, "distance": race.distance.value}
                ),
                "same_surface_distance_band": self._surface_distance_band_sample_count(
                    history, race.surface.value, current_distance_band
                ),
                "same_racecourse_distance": self._condition_sample_count(
                    history, {"racecourse": race.racecourse.value, "distance": race.distance.value}
                ),
                "same_jockey": self._condition_sample_count(history, {"jockey_name": entry.jockey_name.value}),
            }
            condition_fit = self._condition_fit(
                (
                    (distance_match_top3, condition_counts["same_distance"]),
                    (surface_match_top3, condition_counts["same_surface"]),
                    (track_condition_match_top3, condition_counts["same_track_condition"]),
                    (racecourse_match_top3, condition_counts["same_racecourse"]),
                )
            )
            previous_actual = self._previous_actual_performance(last_run)
            previous_expected = self._optional_float(last_run.get("expected_performance"))
            previous_residual = self._optional_float(last_run.get("performance_residual"))
            if previous_residual is None and previous_actual is not None and previous_expected is not None:
                previous_residual = previous_actual - previous_expected
            rebound_score = self._rebound_score(
                ability_profile.get("base_ability_index"),
                current_form,
                condition_fit,
                previous_actual,
                previous_residual,
            )
            pedigree = pedigree_stats.get(entry.horse.horse_id) or {}
            track_bias_adjustment = self._track_bias_adjustment(
                track_bias.get("raw_bias"), entry.frame_number.value, int(track_bias.get("race_count") or 0)
            )
            running_profile = running_profiles.get(entry.horse.horse_id) or self._running_profile(history)
            market_profile = self._market_performance_profile(history)
            if distance_match_top3 is None:
                unavailable.append("same_distance_top3_rate")
            if distance_band_match_top3 is None:
                unavailable.append("distance_band")
                unavailable.append("same_distance_band_top3_rate")
            if distance_aptitude["distance_sample_count"] == 0:
                unavailable.append("distance_aptitude")
            if surface_match_top3 is None:
                unavailable.append("same_surface_top3_rate")
            if track_condition_match_top3 is None:
                unavailable.append("same_track_condition_top3_rate")
            if racecourse_match_top3 is None:
                unavailable.append("same_racecourse_top3_rate")
            if surface_distance_match_top3 is None:
                unavailable.append("same_surface_distance_top3_rate")
            if surface_distance_band_match_top3 is None:
                unavailable.append("same_surface_distance_band_top3_rate")
            if racecourse_distance_match_top3 is None:
                unavailable.append("same_racecourse_distance_top3_rate")
            if same_jockey_top3 is None:
                unavailable.append("same_jockey_top3_rate")
            if same_jockey_as_last is None:
                unavailable.append("same_jockey_as_last_run")
            if running_profile["running_style"] is None:
                unavailable.append("running_style")
                unavailable.append("average_first_corner_ratio")
                unavailable.append("average_position_gain")
            if field_early_pressure is None:
                unavailable.append("field_early_pressure")
            if market_profile["average_recent_popularity"] is None:
                unavailable.append("average_recent_popularity")
                unavailable.append("market_overperformance_rate")
                unavailable.append("longshot_top3_rate")
            if grade_profile["recent_graded_starts"] is None:
                unavailable.append("recent_graded_starts")
                unavailable.append("recent_graded_top3_rate")
                unavailable.append("latest_race_grade_level")
                unavailable.append("current_race_grade_level")
                unavailable.append("grade_step_from_last")
            if speed_profile["average_recent_speed_mps"] is None:
                unavailable.append("average_recent_speed_mps")
                unavailable.append("best_recent_speed_mps")
                unavailable.append("average_adjusted_speed_delta_mps")
                unavailable.append("weighted_adjusted_speed_delta_mps")
                unavailable.append("best_adjusted_speed_delta_mps")
            if ability_profile["base_ability_index"] is None:
                unavailable.append("base_ability_index")
                unavailable.append("weighted_finish_quality")
                unavailable.append("weighted_margin_score")
                unavailable.append("weighted_opponent_score")
                unavailable.append("weighted_track_adjusted_speed_score")
            if current_form is None:
                unavailable.append("current_form")
            if condition_fit is None:
                unavailable.append("condition_fit")
            if previous_actual is None:
                unavailable.append("previous_race_actual_performance")
            if previous_expected is None:
                unavailable.append("previous_race_expected_performance")
            if previous_residual is None:
                unavailable.append("previous_race_residual")
            if rebound_score is None:
                unavailable.append("rebound_score")
            if body_weight_profile["latest_recent_horse_weight"] is None:
                unavailable.append("latest_recent_horse_weight")
                unavailable.append("average_recent_horse_weight")
                unavailable.append("latest_recent_horse_weight_change")
            if course_frame_bias is None:
                unavailable.append("course_frame_bias")
                unavailable.append("course_frame_bias_label")
            if field_prize_rank_ratios.get(entry.horse.horse_id) is None:
                unavailable.append("field_total_prize_money_rank_ratio")
            if field_career_win_rank_ratios.get(entry.horse.horse_id) is None:
                unavailable.append("field_career_win_rate_rank_ratio")
            if field_career_top3_rank_ratios.get(entry.horse.horse_id) is None:
                unavailable.append("field_career_top3_rate_rank_ratio")
            if field_implied_win_rank_ratios.get(entry.horse.horse_id) is None:
                unavailable.append("field_implied_win_probability_rank_ratio")
            if field_implied_win_shares.get(entry.horse.horse_id) is None:
                unavailable.append("field_implied_win_probability_share")
            features = {
                "horse_number": entry.horse_number.value,
                "frame_number": entry.frame_number.value,
                "course_frame_bias": course_frame_bias,
                "course_frame_bias_label": course_frame_bias_label,
                "age": entry.age.value,
                "age_band": self._age_band(entry.age.value),
                "carried_weight": entry.carried_weight.value,
                "carried_weight_change_from_last": self._carried_weight_change_from_last(entry.carried_weight.value, last_run.get("carried_weight")),
                "horse_weight": entry.horse_weight.value,
                "horse_weight_change": entry.horse_weight_change.value,
                "win_odds": entry.win_odds.value,
                "popularity": entry.popularity.value,
                "implied_win_probability": self._implied_win_probability(entry.win_odds.value),
                "field_implied_win_probability_rank_ratio": field_implied_win_rank_ratios.get(entry.horse.horse_id),
                "field_implied_win_probability_share": field_implied_win_shares.get(entry.horse.horse_id),
                "career_starts": entry.career_starts.value,
                "career_wins": entry.career_wins.value,
                "career_win_rate": entry.career_win_rate.value,
                "career_top3_rate": entry.career_top3_rate.value,
                "total_prize_money": entry.total_prize_money.value,
                "field_total_prize_money_rank_ratio": field_prize_rank_ratios.get(entry.horse.horse_id),
                "field_career_win_rate_rank_ratio": field_career_win_rank_ratios.get(entry.horse.horse_id),
                "field_career_top3_rate_rank_ratio": field_career_top3_rank_ratios.get(entry.horse.horse_id),
                "surface": race.surface.value,
                "distance": race.distance.value,
                "distance_band": current_distance_band,
                "racecourse": race.racecourse.value,
                "track_condition": race.track_condition.value,
                "field_size": race.field_size.value or len(entries),
                "past_5_average_finish": (sum(recent_finishes) / len(recent_finishes)) if recent_finishes else None,
                "past_5_win_rate": self._win_rate(recent_finishes),
                "past_5_top3_rate": (sum(1 for p in recent_finishes if p <= 3) / len(recent_finishes)) if recent_finishes else None,
                "past_5_bad_finish_rate": self._bad_finish_rate(recent_finishes),
                "recent_3_average_finish": (sum(recent_finishes[:3]) / len(recent_finishes[:3])) if recent_finishes[:3] else None,
                "last_run_finish_position": last_run_profile["last_run_finish_position"],
                "last_run_finish_ratio": last_run_profile["last_run_finish_ratio"],
                "last_run_top3": last_run_profile["last_run_top3"],
                "last_run_top_half": last_run_profile["last_run_top_half"],
                "recent_finish_range": consistency_profile["recent_finish_range"],
                "recent_consistency_label": consistency_profile["recent_consistency_label"],
                "past_5_average_finish_ratio": finish_ratio_profile["past_5_average_finish_ratio"],
                "past_5_top_half_rate": finish_ratio_profile["past_5_top_half_rate"],
                "weighted_recent_finish_ratio": weighted_recent_profile["weighted_recent_finish_ratio"],
                "weighted_recent_top3_rate": weighted_recent_profile["weighted_recent_top3_rate"],
                "past_5_average_final_three_furlongs": (sum(recent_f3) / len(recent_f3)) if recent_f3 else None,
                "same_distance_top3_rate": distance_match_top3,
                "same_distance_sample_count": condition_counts["same_distance"],
                "same_distance_shrunk_top3_rate": self._shrunk_rate(distance_match_top3, condition_counts["same_distance"]),
                **distance_aptitude,
                "same_distance_sample_status": self._sample_status(condition_counts["same_distance"]),
                "same_distance_band_top3_rate": distance_band_match_top3,
                "same_distance_band_sample_count": condition_counts["same_distance_band"],
                "same_distance_band_shrunk_top3_rate": self._shrunk_rate(distance_band_match_top3, condition_counts["same_distance_band"]),
                "same_surface_top3_rate": surface_match_top3,
                "same_surface_sample_count": condition_counts["same_surface"],
                "same_surface_shrunk_top3_rate": self._shrunk_rate(surface_match_top3, condition_counts["same_surface"]),
                "same_surface_sample_status": self._sample_status(condition_counts["same_surface"]),
                "same_track_condition_top3_rate": track_condition_match_top3,
                "same_track_condition_sample_count": condition_counts["same_track_condition"],
                "same_track_condition_shrunk_top3_rate": self._shrunk_rate(track_condition_match_top3, condition_counts["same_track_condition"]),
                "same_racecourse_top3_rate": racecourse_match_top3,
                "same_racecourse_sample_count": condition_counts["same_racecourse"],
                "same_racecourse_shrunk_top3_rate": self._shrunk_rate(racecourse_match_top3, condition_counts["same_racecourse"]),
                "same_racecourse_sample_status": self._sample_status(condition_counts["same_racecourse"]),
                "same_surface_distance_top3_rate": surface_distance_match_top3,
                "same_surface_distance_sample_count": condition_counts["same_surface_distance"],
                "same_surface_distance_shrunk_top3_rate": self._shrunk_rate(surface_distance_match_top3, condition_counts["same_surface_distance"]),
                "same_surface_distance_band_top3_rate": surface_distance_band_match_top3,
                "same_surface_distance_band_sample_count": condition_counts["same_surface_distance_band"],
                "same_surface_distance_band_shrunk_top3_rate": self._shrunk_rate(surface_distance_band_match_top3, condition_counts["same_surface_distance_band"]),
                "same_racecourse_distance_top3_rate": racecourse_distance_match_top3,
                "same_racecourse_distance_sample_count": condition_counts["same_racecourse_distance"],
                "same_racecourse_distance_shrunk_top3_rate": self._shrunk_rate(racecourse_distance_match_top3, condition_counts["same_racecourse_distance"]),
                "same_jockey_top3_rate": same_jockey_top3,
                "same_jockey_sample_count": condition_counts["same_jockey"],
                "same_jockey_shrunk_top3_rate": self._shrunk_rate(same_jockey_top3, condition_counts["same_jockey"]),
                "same_jockey_sample_status": self._sample_status(condition_counts["same_jockey"]),
                "same_jockey_as_last_run": same_jockey_as_last,
                "sire_name": pedigree.get("sire_name") or entry.horse.sire.value,
                "sire_condition_starts": int(pedigree.get("sire_condition_starts") or 0),
                "sire_condition_top3_rate": pedigree.get("sire_condition_top3_rate"),
                "sire_condition_shrunk_top3_rate": self._shrunk_rate(
                    pedigree.get("sire_condition_top3_rate"),
                    int(pedigree.get("sire_condition_starts") or 0),
                    DEFAULT_STABILITY_CONFIG.pedigree_shrinkage_strength,
                ),
                "sire_condition_sample_status": self._sample_status(int(pedigree.get("sire_condition_starts") or 0)),
                "damsire_name": pedigree.get("damsire_name") or entry.horse.damsire.value,
                "damsire_condition_starts": int(pedigree.get("damsire_condition_starts") or 0),
                "damsire_condition_top3_rate": pedigree.get("damsire_condition_top3_rate"),
                "damsire_condition_shrunk_top3_rate": self._shrunk_rate(
                    pedigree.get("damsire_condition_top3_rate"),
                    int(pedigree.get("damsire_condition_starts") or 0),
                    DEFAULT_STABILITY_CONFIG.pedigree_shrinkage_strength,
                ),
                "damsire_condition_sample_status": self._sample_status(int(pedigree.get("damsire_condition_starts") or 0)),
                "track_bias_adjustment": track_bias_adjustment,
                "track_bias_sample_count": int(track_bias.get("race_count") or 0),
                "track_bias_sample_status": track_bias.get("sample_status") or "UNKNOWN",
                "running_style": running_profile["running_style"],
                "average_first_corner_ratio": running_profile["average_first_corner_ratio"],
                "average_position_gain": running_profile["average_position_gain"],
                "field_early_pressure": field_early_pressure,
                "average_recent_popularity": market_profile["average_recent_popularity"],
                "market_overperformance_rate": market_profile["market_overperformance_rate"],
                "longshot_top3_rate": market_profile["longshot_top3_rate"],
                "recent_graded_starts": grade_profile["recent_graded_starts"],
                "recent_graded_top3_rate": grade_profile["recent_graded_top3_rate"],
                "latest_race_grade_level": grade_profile["latest_race_grade_level"],
                "current_race_grade_level": grade_profile["current_race_grade_level"],
                "grade_step_from_last": grade_profile["grade_step_from_last"],
                "average_recent_speed_mps": speed_profile["average_recent_speed_mps"],
                "best_recent_speed_mps": speed_profile["best_recent_speed_mps"],
                "average_adjusted_speed_delta_mps": speed_profile["average_adjusted_speed_delta_mps"],
                "weighted_adjusted_speed_delta_mps": speed_profile["weighted_adjusted_speed_delta_mps"],
                "best_adjusted_speed_delta_mps": speed_profile["best_adjusted_speed_delta_mps"],
                "base_ability_index": ability_profile["base_ability_index"],
                "current_form": current_form,
                "condition_fit": condition_fit,
                "previous_race_actual_performance": previous_actual,
                "previous_race_expected_performance": previous_expected,
                "previous_race_residual": previous_residual,
                "rebound_score": rebound_score,
                "ability_sample_count": ability_profile["ability_sample_count"],
                "ability_sample_status": self._sample_status(ability_profile["ability_sample_count"]),
                "weighted_finish_quality": ability_profile["weighted_finish_quality"],
                "weighted_margin_score": ability_profile["weighted_margin_score"],
                "weighted_opponent_score": ability_profile["weighted_opponent_score"],
                "weighted_track_adjusted_speed_score": ability_profile[
                    "weighted_track_adjusted_speed_score"
                ],
                "latest_recent_horse_weight": body_weight_profile["latest_recent_horse_weight"],
                "average_recent_horse_weight": body_weight_profile["average_recent_horse_weight"],
                "latest_recent_horse_weight_change": body_weight_profile["latest_recent_horse_weight_change"],
                "days_since_last_run": days_since_last_run,
                "recent_run_count_90d": recent_run_count_90d,
                "distance_change": self._distance_change(last_run.get("distance"), race.distance.value),
                "distance_change_meters": self._distance_change_meters(last_run.get("distance"), race.distance.value),
                "distance_change_abs": self._distance_change_abs(last_run.get("distance"), race.distance.value),
                "surface_change": self._surface_change(last_run.get("surface"), race.surface.value),
                "first_surface": not any(str(item.get("surface") or "") == str(race.surface.value or "") for item in history),
                "first_distance": condition_counts["same_distance"] == 0,
                "first_racecourse": condition_counts["same_racecourse"] == 0,
                "scratched": bool(entry.scratched.value),
            }
            generated.append(
                HorseFeature(
                    race_id=race.race_id,
                    horse_id=entry.horse.horse_id,
                    horse_number=int(entry.horse_number.value),
                    horse_name=str(entry.horse.horse_name.value),
                    features=features,
                    unavailable_features=unavailable,
                    as_of=datetime.now(timezone.utc).isoformat(),
                )
            )
        return generated

    def _current_form(self, history: list[dict]) -> float | None:
        weighted: list[tuple[float, int]] = []
        for index, item in enumerate(history[:5]):
            value = self._previous_actual_performance(item)
            if value is not None:
                weighted.append((value, 5 - index))
        return self._weighted_average(weighted)

    def _condition_fit(self, values: tuple[tuple[float | None, int], ...]) -> float | None:
        available = [
            self._shrunk_rate(score, count)
            for score, count in values
            if score is not None and count > 0
        ]
        return sum(available) / len(available) if available else None

    def _previous_actual_performance(self, item: dict) -> float | None:
        stored = self._optional_float(item.get("actual_performance"))
        if stored is not None:
            return stored
        return relative_finish(item.get("finish_position"), item.get("field_size"))

    def _rebound_score(
        self,
        base_ability: object,
        current_form: float | None,
        condition_fit: float | None,
        previous_actual: float | None,
        previous_residual: float | None,
    ) -> float | None:
        ability = self._optional_float(base_ability)
        if ability is None or previous_actual is None:
            return None
        ability = max(0.0, min(1.0, ability))
        poor_last_run = 1.0 - previous_actual
        residual_signal = max(0.0, -previous_residual) if previous_residual is not None else None
        signals = [(ability, 0.35), (poor_last_run, 0.30)]
        if condition_fit is not None:
            signals.append((condition_fit, 0.20))
        if residual_signal is not None:
            signals.append((min(1.0, residual_signal), 0.15))
        elif current_form is not None:
            signals.append((current_form, 0.10))
        total_weight = sum(weight for _, weight in signals)
        return sum(value * weight for value, weight in signals) / total_weight

    def _optional_float(self, value: object) -> float | None:
        if value in (None, ""):
            return None
        return float(value)

    def _shrunk_rate(self, observed: float | None, count: int, strength: float | None = None) -> float:
        prior = DEFAULT_STABILITY_CONFIG.prior_top3_rate
        if observed is None or count <= 0:
            return prior
        prior_weight = float(strength or DEFAULT_STABILITY_CONFIG.shrinkage_strength)
        return (float(observed) * count + prior * prior_weight) / (count + prior_weight)

    def _sample_status(self, count: int) -> str:
        if count <= 0:
            return "UNKNOWN"
        if count < DEFAULT_STABILITY_CONFIG.sufficient_sample_size:
            return "LOW_SAMPLE"
        return "SUFFICIENT"

    def _condition_sample_count(self, history: list[dict], conditions: dict[str, object]) -> int:
        normalized = {
            key: str(value).strip()
            for key, value in conditions.items()
            if value not in (None, "")
        }
        if len(normalized) != len(conditions):
            return 0
        return sum(
            1
            for item in history[:5]
            if item.get("finish_position") is not None
            and all(str(item.get(key) or "").strip() == value for key, value in normalized.items())
        )

    def _distance_band_sample_count(self, history: list[dict], current_band: str | None) -> int:
        if current_band is None:
            return 0
        return sum(
            1
            for item in history[:5]
            if item.get("finish_position") is not None and self._distance_band(item.get("distance")) == current_band
        )

    def _surface_distance_band_sample_count(self, history: list[dict], surface: object, current_band: str | None) -> int:
        if surface in (None, "") or current_band is None:
            return 0
        return sum(
            1
            for item in history[:5]
            if item.get("finish_position") is not None
            and str(item.get("surface") or "").strip() == str(surface).strip()
            and self._distance_band(item.get("distance")) == current_band
        )

    def _track_bias_adjustment(self, raw_bias: object, frame_number: object, sample_count: int) -> float:
        if raw_bias in (None, "") or frame_number in (None, ""):
            return 0.0
        orientation = max(-1.0, min(1.0, (4.5 - float(frame_number)) / 3.5))
        shrink = sample_count / (sample_count + DEFAULT_STABILITY_CONFIG.shrinkage_strength)
        return float(raw_bias) * orientation * shrink

    def _days_between(self, previous_date: str | None, race_date: str | None) -> int | None:
        if not previous_date or not race_date:
            return None
        previous = datetime.fromisoformat(previous_date)
        current = datetime.fromisoformat(str(race_date))
        return (current - previous).days

    def _field_rank_ratios(self, entries: list[RaceEntryData], value_getter) -> dict[str, float | None]:
        result = {entry.horse.horse_id: None for entry in entries}
        samples: list[tuple[str, float]] = []
        for entry in entries:
            value = value_getter(entry)
            if value is None:
                continue
            samples.append((entry.horse.horse_id, float(value)))
        if len(samples) < 2:
            return result

        sorted_samples = sorted(samples, key=lambda item: item[1], reverse=True)
        denominator = len(sorted_samples) - 1
        previous_value: float | None = None
        previous_ratio = 0.0
        for index, (horse_id, value) in enumerate(sorted_samples):
            if previous_value is not None and value == previous_value:
                result[horse_id] = previous_ratio
                continue
            ratio = index / denominator
            result[horse_id] = ratio
            previous_value = value
            previous_ratio = ratio
        return result

    def _field_value_shares(self, entries: list[RaceEntryData], value_getter) -> dict[str, float | None]:
        result = {entry.horse.horse_id: None for entry in entries}
        samples: list[tuple[str, float]] = []
        for entry in entries:
            value = value_getter(entry)
            if value is None:
                continue
            numeric = float(value)
            if numeric <= 0:
                continue
            samples.append((entry.horse.horse_id, numeric))
        total = sum(value for _, value in samples)
        if total <= 0:
            return result
        for horse_id, value in samples:
            result[horse_id] = value / total
        return result

    def _recent_run_count(self, history: list[dict], race_date: str | None, days: int) -> int | None:
        if not race_date:
            return None
        current = datetime.fromisoformat(str(race_date))
        count = 0
        saw_date = False
        for item in history:
            previous_date = item.get("race_date")
            if not previous_date:
                continue
            saw_date = True
            previous = datetime.fromisoformat(previous_date)
            diff = (current - previous).days
            if 0 < diff <= days:
                count += 1
        return count if saw_date else None

    def _carried_weight_change_from_last(self, current_weight: object, previous_weight: object) -> float | None:
        if current_weight in (None, "") or previous_weight in (None, ""):
            return None
        return round(float(current_weight) - float(previous_weight), 1)

    def _distance_change(self, previous_distance: int | None, current_distance: int | None) -> str | None:
        if previous_distance is None or current_distance is None:
            return None
        diff = self._distance_change_meters(previous_distance, current_distance)
        if diff is None:
            return None
        if abs(diff) < 100:
            return "same"
        return "extend" if diff > 0 else "shorten"

    def _distance_change_meters(self, previous_distance: int | None, current_distance: int | None) -> int | None:
        if previous_distance is None or current_distance is None:
            return None
        return int(current_distance) - int(previous_distance)

    def _distance_change_abs(self, previous_distance: int | None, current_distance: int | None) -> int | None:
        diff = self._distance_change_meters(previous_distance, current_distance)
        return abs(diff) if diff is not None else None

    def _surface_change(self, previous_surface: str | None, current_surface: str | None) -> str | None:
        if not previous_surface or not current_surface:
            return None
        if previous_surface == current_surface:
            return "same"
        return f"{previous_surface}_to_{current_surface}"

    def _win_rate(self, finishes: list[int]) -> float | None:
        if not finishes:
            return None
        return sum(1 for position in finishes if int(position) == 1) / len(finishes)

    def _bad_finish_rate(self, finishes: list[int]) -> float | None:
        if not finishes:
            return None
        return sum(1 for position in finishes if int(position) >= 7) / len(finishes)

    def _consistency_profile(self, finishes: list[int]) -> dict[str, int | str | None]:
        if not finishes:
            return {"recent_finish_range": None, "recent_consistency_label": None}
        finish_range = max(int(position) for position in finishes) - min(int(position) for position in finishes)
        if len(finishes) < 2:
            label = "limited"
        elif finish_range <= 2:
            label = "stable"
        elif finish_range >= 7:
            label = "volatile"
        else:
            label = "mixed"
        return {"recent_finish_range": finish_range, "recent_consistency_label": label}

    def _last_run_profile(self, last_run: dict) -> dict[str, int | float | bool | None]:
        finish = last_run.get("finish_position")
        field_size = last_run.get("field_size")
        if finish is None:
            return {
                "last_run_finish_position": None,
                "last_run_finish_ratio": None,
                "last_run_top3": None,
                "last_run_top_half": None,
            }
        finish_int = int(finish)
        field_int = int(field_size) if field_size else None
        return {
            "last_run_finish_position": finish_int,
            "last_run_finish_ratio": (finish_int / field_int) if field_int else None,
            "last_run_top3": finish_int <= 3,
            "last_run_top_half": (finish_int <= max(1, field_int / 2)) if field_int else None,
        }

    def _finish_ratio_profile(self, history: list[dict]) -> dict[str, float | None]:
        samples = [
            (int(item["finish_position"]), int(item["field_size"]))
            for item in history[:5]
            if item.get("finish_position") is not None and item.get("field_size")
        ]
        if not samples:
            return {"past_5_average_finish_ratio": None, "past_5_top_half_rate": None}
        ratios = [finish / field_size for finish, field_size in samples]
        top_half = [finish <= max(1, field_size / 2) for finish, field_size in samples]
        return {
            "past_5_average_finish_ratio": sum(ratios) / len(ratios),
            "past_5_top_half_rate": sum(1 for value in top_half if value) / len(top_half),
        }

    def _weighted_recent_profile(self, history: list[dict]) -> dict[str, float | None]:
        weighted_ratios = []
        weighted_top3 = []
        for index, item in enumerate(history[:5]):
            weight = 5 - index
            if item.get("finish_position") is None:
                continue
            finish = int(item["finish_position"])
            if item.get("field_size"):
                weighted_ratios.append((finish / int(item["field_size"]), weight))
            weighted_top3.append((1.0 if finish <= 3 else 0.0, weight))
        ratio = self._weighted_average(weighted_ratios)
        top3 = self._weighted_average(weighted_top3)
        return {"weighted_recent_finish_ratio": ratio, "weighted_recent_top3_rate": top3}

    def _weighted_average(self, values: list[tuple[float, int]]) -> float | None:
        if not values:
            return None
        total_weight = sum(weight for _, weight in values)
        return sum(value * weight for value, weight in values) / total_weight

    def _implied_win_probability(self, win_odds: object) -> float | None:
        if win_odds in (None, ""):
            return None
        odds = float(win_odds)
        if odds <= 0:
            return None
        return 1.0 / odds

    def _condition_top3_rate(self, history: list[dict], key: str, value: object) -> float | None:
        if value in (None, ""):
            return None
        target = str(value).strip()
        matched = [
            item
            for item in history[:5]
            if str(item.get(key) or "").strip() == target and item.get("finish_position") is not None
        ]
        if not matched:
            return None
        return sum(1 for item in matched if int(item["finish_position"]) <= 3) / len(matched)

    def _multi_condition_top3_rate(self, history: list[dict], conditions: dict[str, object]) -> float | None:
        normalized = {
            key: str(value).strip()
            for key, value in conditions.items()
            if value not in (None, "")
        }
        if len(normalized) != len(conditions):
            return None
        matched = [
            item
            for item in history[:5]
            if item.get("finish_position") is not None
            and all(str(item.get(key) or "").strip() == value for key, value in normalized.items())
        ]
        if not matched:
            return None
        return sum(1 for item in matched if int(item["finish_position"]) <= 3) / len(matched)

    def _distance_band_top3_rate(self, history: list[dict], current_band: str | None) -> float | None:
        if current_band is None:
            return None
        matched = [
            item
            for item in history[:5]
            if item.get("finish_position") is not None
            and self._distance_band(item.get("distance")) == current_band
        ]
        if not matched:
            return None
        return sum(1 for item in matched if int(item["finish_position"]) <= 3) / len(matched)

    def _surface_distance_band_top3_rate(self, history: list[dict], surface: object, current_band: str | None) -> float | None:
        if surface in (None, "") or current_band is None:
            return None
        surface_text = str(surface).strip()
        matched = [
            item
            for item in history[:5]
            if item.get("finish_position") is not None
            and str(item.get("surface") or "").strip() == surface_text
            and self._distance_band(item.get("distance")) == current_band
        ]
        if not matched:
            return None
        return sum(1 for item in matched if int(item["finish_position"]) <= 3) / len(matched)

    def _distance_band(self, distance: object) -> str | None:
        if distance in (None, ""):
            return None
        meters = int(distance)
        if meters <= 1400:
            return "sprint"
        if meters <= 1800:
            return "mile"
        if meters <= 2200:
            return "middle"
        return "stayer"

    def _distance_aptitude_profile(
        self,
        history: list[dict],
        current_distance: object,
        racecourse: object,
        surface: object,
    ) -> dict[str, float | int | str | None]:
        try:
            target_distance = int(current_distance)
        except (TypeError, ValueError):
            target_distance = 0
        target_course = str(racecourse or "").strip()
        target_surface = str(surface or "").strip()
        valid: list[tuple[int, int, int, dict, int]] = []
        for index, row in enumerate(history):
            try:
                distance = int(row.get("distance"))
                finish = int(row.get("finish_position"))
            except (TypeError, ValueError):
                continue
            if distance <= 0 or finish <= 0:
                continue
            try:
                field_size = int(row.get("field_size") or 0)
            except (TypeError, ValueError):
                field_size = 0
            valid.append((distance, finish, field_size, row, index))

        surface_rows = [
            item for item in valid
            if not target_surface or str(item[3].get("surface") or "").strip() == target_surface
        ]
        exact = [item for item in surface_rows if item[0] == target_distance]
        near = [item for item in surface_rows if abs(item[0] - target_distance) <= 400]
        course_near = [
            item for item in near
            if target_course and str(item[3].get("racecourse") or "").strip() == target_course
        ]

        def rate(rows: list[tuple[int, int, int, dict, int]], maximum_finish: int) -> float | None:
            return sum(item[1] <= maximum_finish for item in rows) / len(rows) if rows else None

        def average_finish_ratio(rows: list[tuple[int, int, int, dict, int]]) -> float | None:
            ratios = [
                (field_size - finish) / (field_size - 1)
                for _, finish, field_size, _, _ in rows
                if field_size > 1
            ]
            return sum(ratios) / len(ratios) if ratios else None

        def distance_weight(delta: int) -> float:
            if delta == 0:
                return 1.0
            if delta <= 200:
                return 0.9
            if delta <= 400:
                return 0.75
            if delta <= 600:
                return 0.5
            if delta <= 800:
                return 0.3
            return 0.1

        def weighted_score(rows: list[tuple[int, int, int, dict, int]]) -> float | None:
            weighted_scores: list[tuple[float, float]] = []
            for distance, finish, field_size, _, index in rows:
                if field_size > 1:
                    finish_score = (field_size - finish) / (field_size - 1)
                else:
                    continue
                recency_weight = 0.9 ** index
                weighted_scores.append((finish_score, distance_weight(abs(distance - target_distance)) * recency_weight))
            return (
                sum(score * weight for score, weight in weighted_scores) / sum(weight for _, weight in weighted_scores)
                if weighted_scores
                else None
            )

        distance_weighted_score = weighted_score(surface_rows)
        near_weighted_score = weighted_score(near)

        preferred_rows = [item for item in surface_rows if item[1] <= 5]
        preferred_weights = [((1.0 if item[1] <= 3 else 0.6) * (0.9 ** item[4])) for item in preferred_rows]
        preferred_center = (
            sum(item[0] * weight for item, weight in zip(preferred_rows, preferred_weights, strict=True))
            / sum(preferred_weights)
            if preferred_rows
            else None
        )
        preferred_distances = [item[0] for item in preferred_rows]
        return {
            "aptitude_history_count": len(history),
            "distance_sample_count": len(surface_rows),
            "distance_sample_status": self._sample_status(len(surface_rows)),
            "exact_distance_starts": len(exact),
            "exact_distance_top3_rate": rate(exact, 3),
            "exact_distance_top5_rate": rate(exact, 5),
            "near_distance_starts": len(near),
            "near_distance_top3_rate": rate(near, 3),
            "near_distance_top5_rate": rate(near, 5),
            "near_distance_avg_finish_ratio": average_finish_ratio(near),
            "near_distance_best_finish": min((item[1] for item in near), default=None),
            "near_distance_weighted_score": near_weighted_score,
            "near_distance_sample_status": self._sample_status(len(near)),
            "preferred_distance_center": round(preferred_center, 2) if preferred_center is not None else None,
            "preferred_distance_low": min(preferred_distances, default=None),
            "preferred_distance_high": max(preferred_distances, default=None),
            "distance_versatility": round(min(1.0, (max(preferred_distances) - min(preferred_distances)) / 1600), 6)
            if len(preferred_distances) > 1
            else None,
            "distance_delta_weighted_score": distance_weighted_score,
            "same_racecourse_near_distance_starts": len(course_near),
            "same_racecourse_near_distance_top3_rate": rate(course_near, 3),
            "same_racecourse_near_distance_top5_rate": rate(course_near, 5),
            "same_racecourse_near_distance_sample_status": self._sample_status(len(course_near)),
        }

    def _course_frame_bias(
        self,
        racecourse: object,
        surface: object,
        distance: object,
        frame_number: object,
    ) -> tuple[float | None, str | None]:
        if racecourse in (None, "") or surface in (None, "") or distance in (None, "") or frame_number in (None, ""):
            return None, None
        course = str(racecourse)
        surface_text = str(surface)
        meters = int(distance)
        frame = int(frame_number)
        if course == "札幌" and surface_text == "芝" and 1500 <= meters <= 2000:
            if frame <= 3:
                return 0.04, "inner_favorable"
            if frame >= 7:
                return -0.03, "outer_risk"
            return 0.01, "neutral_plus"
        if course in {"函館", "福島", "小倉"} and surface_text == "芝" and meters <= 2000:
            if frame <= 3:
                return 0.03, "inner_favorable"
            if frame >= 7:
                return -0.02, "outer_risk"
            return 0.0, "neutral"
        return 0.0, "neutral"

    def _age_band(self, age: object) -> str | None:
        if age in (None, ""):
            return None
        value = int(age)
        if value <= 3:
            return "young"
        if value <= 5:
            return "prime"
        if value == 6:
            return "mature"
        if value == 7:
            return "veteran"
        return "senior"

    def _same_jockey_as_last_run(self, last_run: dict, current_jockey: object) -> bool | None:
        if not last_run or current_jockey in (None, "") or not last_run.get("jockey_name"):
            return None
        return str(last_run["jockey_name"]).strip() == str(current_jockey).strip()

    def _running_profile(self, history: list[dict]) -> dict[str, float | str | None]:
        samples = []
        gains = []
        for item in history[:5]:
            positions = self._corner_position_numbers(item.get("corner_positions"))
            field_size = item.get("field_size")
            if not positions or not field_size:
                continue
            first = positions[0]
            last = positions[-1]
            samples.append(first / int(field_size))
            gains.append(first - last)
        if not samples:
            return {"running_style": None, "average_first_corner_ratio": None, "average_position_gain": None}

        average_first_corner_ratio = sum(samples) / len(samples)
        average_position_gain = sum(gains) / len(gains)
        if average_first_corner_ratio <= 0.35:
            style = "front"
        elif average_first_corner_ratio <= 0.6:
            style = "stalker"
        else:
            style = "closer"
        return {
            "running_style": style,
            "average_first_corner_ratio": average_first_corner_ratio,
            "average_position_gain": average_position_gain,
        }

    def _corner_position_numbers(self, value: object) -> list[int]:
        if value in (None, ""):
            return []
        numbers = []
        for part in str(value).split("-"):
            try:
                numbers.append(int(part))
            except ValueError:
                continue
        return numbers

    def _field_early_pressure(self, profiles: list[dict[str, float | str | None]]) -> float | None:
        known = [profile for profile in profiles if profile.get("running_style")]
        if not known:
            return None
        early_count = sum(1 for profile in known if profile["running_style"] in {"front", "stalker"})
        return early_count / len(known)

    def _market_performance_profile(self, history: list[dict]) -> dict[str, float | None]:
        samples = [
            item
            for item in history[:5]
            if item.get("popularity") is not None and item.get("finish_position") is not None
        ]
        if not samples:
            return {
                "average_recent_popularity": None,
                "market_overperformance_rate": None,
                "longshot_top3_rate": None,
            }
        popularities = [int(item["popularity"]) for item in samples]
        overperform_count = sum(1 for item in samples if int(item["finish_position"]) < int(item["popularity"]))
        longshot_samples = [item for item in samples if int(item["popularity"]) >= 6]
        longshot_top3_count = sum(1 for item in longshot_samples if int(item["finish_position"]) <= 3)
        return {
            "average_recent_popularity": sum(popularities) / len(popularities),
            "market_overperformance_rate": overperform_count / len(samples),
            "longshot_top3_rate": (longshot_top3_count / len(longshot_samples)) if longshot_samples else 0.0,
        }

    def _grade_profile(self, history: list[dict], current_race_name: object, current_grade: object) -> dict[str, int | float | None]:
        current_level = self._race_grade_level(current_grade, current_race_name)
        samples = [
            item
            for item in history[:5]
            if item.get("race_name") not in (None, "") and item.get("finish_position") is not None
        ]
        if not samples:
            return {
                "recent_graded_starts": None,
                "recent_graded_top3_rate": None,
                "latest_race_grade_level": None,
                "current_race_grade_level": current_level,
                "grade_step_from_last": None,
            }
        levels = [self._race_grade_level(None, item.get("race_name")) for item in samples]
        graded_samples = [(item, level) for item, level in zip(samples, levels, strict=True) if level >= GRADE_LEVELS["G3"]]
        latest_level = levels[0]
        if not graded_samples:
            return {
                "recent_graded_starts": 0,
                "recent_graded_top3_rate": 0.0,
                "latest_race_grade_level": latest_level,
                "current_race_grade_level": current_level,
                "grade_step_from_last": (current_level - latest_level) if current_level is not None and latest_level is not None else None,
            }
        top3_count = sum(1 for item, _ in graded_samples if int(item["finish_position"]) <= 3)
        return {
            "recent_graded_starts": len(graded_samples),
            "recent_graded_top3_rate": top3_count / len(graded_samples),
            "latest_race_grade_level": latest_level,
            "current_race_grade_level": current_level,
            "grade_step_from_last": (current_level - latest_level) if current_level is not None and latest_level is not None else None,
        }

    def _race_grade_level(self, grade: object, race_name: object) -> int | None:
        grade_text = str(grade or "")
        race_text = str(race_name or "")
        normalized = (
            grade_text.replace("Ⅰ", "1").replace("Ⅱ", "2").replace("Ⅲ", "3")
            + " "
            + race_text.replace("Ⅰ", "1").replace("Ⅱ", "2").replace("Ⅲ", "3")
        )
        if "G1" in normalized or "Jpn1" in normalized:
            return GRADE_LEVELS["G1"]
        if "G2" in normalized or "Jpn2" in normalized:
            return GRADE_LEVELS["G2"]
        if "G3" in normalized or "Jpn3" in normalized:
            return GRADE_LEVELS["G3"]
        if any(name in race_text for name in KNOWN_G1_RACES):
            return GRADE_LEVELS["G1"]
        if any(name in race_text for name in KNOWN_G2_RACES):
            return GRADE_LEVELS["G2"]
        if any(name in race_text for name in KNOWN_G3_RACES):
            return GRADE_LEVELS["G3"]
        if "リステッド" in normalized or "(L)" in normalized or " L" in normalized:
            return GRADE_LEVELS["L"]
        if "オープン" in normalized or "OP" in normalized:
            return GRADE_LEVELS["OP"]
        return 0

    def _speed_profile(self, history: list[dict]) -> dict[str, float | None]:
        speeds = []
        adjusted_deltas = []
        weighted_adjusted_deltas = []
        for index, item in enumerate(history[:5]):
            distance = item.get("distance")
            seconds = self._finish_time_seconds(item.get("finish_time"))
            if distance is None or seconds is None or seconds <= 0:
                continue
            speed = int(distance) / seconds
            speeds.append(speed)
            baseline = self._speed_baseline(
                item.get("surface"),
                int(distance),
                item.get("track_condition"),
                item.get("racecourse"),
            )
            if baseline is not None:
                adjusted_delta = speed - baseline
                adjusted_deltas.append(adjusted_delta)
                weighted_adjusted_deltas.append((adjusted_delta, 5 - index))
        if not speeds:
            return {
                "average_recent_speed_mps": None,
                "best_recent_speed_mps": None,
                "average_adjusted_speed_delta_mps": None,
                "weighted_adjusted_speed_delta_mps": None,
                "best_adjusted_speed_delta_mps": None,
            }
        return {
            "average_recent_speed_mps": sum(speeds) / len(speeds),
            "best_recent_speed_mps": max(speeds),
            "average_adjusted_speed_delta_mps": (sum(adjusted_deltas) / len(adjusted_deltas)) if adjusted_deltas else None,
            "weighted_adjusted_speed_delta_mps": self._weighted_average(weighted_adjusted_deltas),
            "best_adjusted_speed_delta_mps": max(adjusted_deltas) if adjusted_deltas else None,
        }

    def _ability_profile(self, history: list[dict]) -> dict[str, float | int | None]:
        config = DEFAULT_STABILITY_CONFIG
        run_scores: list[tuple[float, float]] = []
        finish_scores: list[tuple[float, float]] = []
        margin_scores: list[tuple[float, float]] = []
        speed_scores: list[tuple[float, float]] = []
        opponent_scores: list[tuple[float, float]] = []
        for index, item in enumerate(history[: len(config.ability_recency_weights)]):
            recency_weight = config.ability_recency_weights[index]
            components: list[tuple[float, float]] = []
            finish_quality = self._finish_quality(item.get("finish_position"), item.get("field_size"))
            if finish_quality is not None:
                components.append((finish_quality, config.ability_finish_weight))
                finish_scores.append((finish_quality, recency_weight))
            margin_score = self._margin_score(item.get("time_behind_winner_seconds"))
            if margin_score is not None:
                components.append((margin_score, config.ability_margin_weight))
                margin_scores.append((margin_score, recency_weight))
            speed_score = self._track_adjusted_speed_score(item)
            if speed_score is not None:
                components.append((speed_score, config.ability_speed_weight))
                speed_scores.append((speed_score, recency_weight))
            opponent_score = self._opponent_score(item)
            if opponent_score is not None:
                components.append((opponent_score, config.ability_opponent_weight))
                opponent_scores.append((opponent_score, recency_weight))
            component_weight = sum(weight for _, weight in components)
            if component_weight > 0:
                run_score = sum(value * weight for value, weight in components) / component_weight
                run_scores.append((run_score, recency_weight))
        observed = self._weighted_average(run_scores)
        sample_count = len(run_scores)
        if observed is None:
            ability_index = None
        else:
            ability_index = (
                observed * sample_count + config.ability_prior * config.ability_shrinkage_strength
            ) / (sample_count + config.ability_shrinkage_strength)
        return {
            "base_ability_index": ability_index,
            "ability_sample_count": sample_count,
            "weighted_finish_quality": self._weighted_average(finish_scores),
            "weighted_margin_score": self._weighted_average(margin_scores),
            "weighted_opponent_score": self._weighted_average(opponent_scores),
            "weighted_track_adjusted_speed_score": self._weighted_average(speed_scores),
        }

    def _finish_quality(self, finish: object, field_size: object) -> float | None:
        if finish in (None, ""):
            return None
        finish_number = max(1, int(finish))
        if field_size not in (None, "") and int(field_size) > 1:
            return max(0.0, min(1.0, 1.0 - (finish_number - 1) / (int(field_size) - 1)))
        return max(0.0, min(1.0, 1.0 - (finish_number - 1) / 11.0))

    def _margin_score(self, seconds: object) -> float | None:
        if seconds in (None, ""):
            return None
        return max(0.0, min(1.0, 1.0 - float(seconds) / 3.0))

    def _track_adjusted_speed_score(self, item: dict) -> float | None:
        distance = item.get("distance")
        seconds = self._finish_time_seconds(item.get("finish_time"))
        if distance in (None, "") or seconds is None or seconds <= 0:
            return None
        baseline = self._speed_baseline(
            item.get("surface"),
            int(distance),
            item.get("track_condition"),
            item.get("racecourse"),
        )
        if baseline is None:
            return None
        adjusted_delta = int(distance) / seconds - baseline
        return max(0.0, min(1.0, 0.5 + adjusted_delta / 1.2))

    def _opponent_score(self, item: dict) -> float | None:
        if item.get("grade") in (None, "") and item.get("race_name") in (None, ""):
            return None
        level = self._race_grade_level(item.get("grade"), item.get("race_name"))
        if level is None:
            return None
        return max(0.0, min(1.0, 0.2 + level * 0.2))

    def _speed_baseline(self, surface: object, distance: int, track_condition: object, racecourse: object) -> float | None:
        surface_text = str(surface or "")
        if surface_text == "芝":
            baseline = self._distance_band_value(distance, sprint=16.95, mile=16.75, middle=16.55, stayer=16.15)
            baseline += {"良": 0.0, "稍重": -0.08, "重": -0.18, "不良": -0.25}.get(str(track_condition or ""), 0.0)
        elif surface_text == "ダート":
            baseline = self._distance_band_value(distance, sprint=15.80, mile=15.55, middle=15.25, stayer=14.90)
            baseline += {"良": 0.0, "稍重": 0.03, "重": 0.06, "不良": 0.04}.get(str(track_condition or ""), 0.0)
        else:
            return None
        baseline += {
            "札幌": -0.05,
            "函館": -0.06,
            "福島": -0.03,
            "新潟": 0.04,
            "東京": 0.03,
            "中山": -0.02,
            "京都": 0.01,
            "阪神": 0.0,
            "中京": 0.0,
            "小倉": 0.02,
        }.get(str(racecourse or ""), 0.0)
        return baseline

    def _distance_band_value(self, distance: int, sprint: float, mile: float, middle: float, stayer: float) -> float:
        if distance <= 1400:
            return sprint
        if distance <= 1800:
            return mile
        if distance <= 2400:
            return middle
        return stayer

    def _finish_time_seconds(self, value: object) -> float | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        parts = text.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + float(parts[1])
            return float(text)
        except ValueError:
            return None

    def _body_weight_profile(self, history: list[dict]) -> dict[str, int | float | None]:
        weights = [int(item["horse_weight"]) for item in history[:5] if item.get("horse_weight") is not None]
        if not weights:
            return {
                "latest_recent_horse_weight": None,
                "average_recent_horse_weight": None,
                "latest_recent_horse_weight_change": None,
            }
        return {
            "latest_recent_horse_weight": weights[0],
            "average_recent_horse_weight": sum(weights) / len(weights),
            "latest_recent_horse_weight_change": (weights[0] - weights[1]) if len(weights) >= 2 else None,
        }
