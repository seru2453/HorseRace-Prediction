from __future__ import annotations

import math
import re
from statistics import mean

from services.common.models import PredictionData, RaceResultData, UserPredictionInput
from services.ml.targets import relative_finish, relative_finish_category


MARK_PROBABILITIES = {
    "◎": 0.30,
    "○": 0.22,
    "▲": 0.16,
    "△": 0.10,
    "☆": 0.06,
    "消": 0.01,
}


class PredictionReviewer:
    def review(
        self,
        predictions: list[PredictionData],
        result: RaceResultData,
        user_predictions: dict[int, UserPredictionInput] | None = None,
    ) -> dict:
        winners = {entry.horse_number for entry in result.entries if entry.finish_position == 1}
        result_by_number = {entry.horse_number: entry for entry in result.entries}
        predicted_field_sizes = [prediction.field_size for prediction in predictions if prediction.field_size]
        field_size = max(predicted_field_sizes) if predicted_field_sizes else (len(result.entries) or None)
        rows = []
        for prediction in predictions:
            result_entry = result_by_number.get(prediction.horse_number)
            finish_position = result_entry.finish_position if result_entry else None
            rank_error = (
                int(finish_position) - int(prediction.final_rank)
                if finish_position is not None and prediction.final_rank is not None
                else None
            )
            actual = 1.0 if prediction.horse_number in winners else 0.0
            user = (user_predictions or {}).get(prediction.horse_number)
            user_prob = self._user_probability(user)
            score_factors = prediction.explanation.get("score_factors", [])
            rows.append(
                {
                    "horse_number": prediction.horse_number,
                    "horse_name": prediction.horse_name,
                    "finish_position": finish_position,
                    "field_size": field_size,
                    "relative_finish": relative_finish(finish_position, field_size),
                    "relative_finish_category": relative_finish_category(
                        relative_finish(finish_position, field_size)
                    ),
                    "result_status": result_entry.result_status if result_entry else "no_result_row",
                    "finish_time": result_entry.finish_time if result_entry else None,
                    "margin": result_entry.margin if result_entry else None,
                    "final_three_furlongs": result_entry.final_three_furlongs if result_entry else None,
                    "popularity": result_entry.popularity if result_entry else None,
                    "actual_win": actual,
                    "ai_probability": prediction.win_probability,
                    "predicted_probability": prediction.win_probability,
                    "raw_probability": prediction.raw_probability,
                    "calibrated_probability": prediction.calibrated_probability,
                    "calibration_version": prediction.calibration_version,
                    "ai_brier": self._brier(prediction.win_probability, actual),
                    "ai_log_loss": self._log_loss(prediction.win_probability, actual),
                    "user_probability": user_prob,
                    "user_mark": user.mark if user else None,
                    "user_confidence": user.confidence if user else None,
                    "user_ratings": self._user_ratings(user),
                    "score_factors": score_factors,
                    "factor_impacts": self._factor_impacts(score_factors),
                    "base_score": prediction.base_ability_score,
                    "ability_index": prediction.ability_index,
                    "ability_components": prediction.ability_components,
                    "ability_sample_status": prediction.ability_sample_status,
                    "condition_adjustment": prediction.condition_adjustment,
                    "current_form": prediction.current_form,
                    "condition_fit": prediction.condition_fit,
                    "previous_race_residual": prediction.previous_race_residual,
                    "rebound_score": prediction.rebound_score,
                    "jev_features": prediction.jev_features,
                    "raw_condition_adjustment": prediction.raw_condition_adjustment,
                    "feature_group_adjustments": prediction.feature_group_adjustments,
                    "course_adjustment": prediction.feature_group_adjustments.get("COURSE", 0.0),
                    "surface_adjustment": prediction.feature_group_adjustments.get("SURFACE", 0.0),
                    "track_bias_adjustment": prediction.feature_group_adjustments.get("TRACK_BIAS", 0.0),
                    "pedigree_adjustment": prediction.feature_group_adjustments.get("PEDIGREE", 0.0),
                    "uncertainty": prediction.uncertainty,
                    "confidence": prediction.confidence,
                    "confidence_label": prediction.confidence_label,
                    "base_rank": prediction.base_rank,
                    "final_rank": prediction.final_rank,
                    "rank_delta": prediction.rank_delta,
                    "finish_rank_error": rank_error,
                    "absolute_finish_rank_error": abs(rank_error) if rank_error is not None else None,
                    "rank_error_direction": (
                        "overvalued" if rank_error and rank_error > 0
                        else "undervalued" if rank_error and rank_error < 0
                        else "matched" if rank_error == 0
                        else None
                    ),
                    "largest_positive_adjustment": prediction.largest_positive_adjustment,
                    "largest_negative_adjustment": prediction.largest_negative_adjustment,
                    "adjustment_source": prediction.adjustment_source,
                    "condition_sample_statuses": prediction.condition_sample_statuses,
                    "new_horse_flags": prediction.new_horse_flags,
                    "simulation_version": prediction.simulation_version,
                    "model_version": prediction.model_version,
                    "feature_version": prediction.feature_version,
                    "parameter_version": prediction.parameter_version,
                    "user_brier": self._brier(user_prob, actual) if user_prob is not None else None,
                    "user_log_loss": self._log_loss(user_prob, actual) if user_prob is not None else None,
                }
            )
        ai_brier_score = round(mean(row["ai_brier"] for row in rows), 6) if rows else None
        user_brier_score = self._nullable_mean([row["user_brier"] for row in rows])
        return {
            "race_id": result.race_id,
            "ai_brier_score": ai_brier_score,
            "ai_log_loss": round(mean(row["ai_log_loss"] for row in rows), 6) if rows else None,
            "user_brier_score": user_brier_score,
            "user_log_loss": self._nullable_mean([row["user_log_loss"] for row in rows]),
            "user_probability_summary": self._user_probability_summary(rows),
            "comparison": self._comparison(ai_brier_score, user_brier_score),
            "insights": self._insights(rows),
            "factor_attribution": self._factor_attribution(rows),
            "ranking_metrics": self._ranking_metrics(rows),
            "note": "データが十分に蓄積するまでは条件別精度の統計的な意味は限定的です。",
            "rows": rows,
        }

    def _ranking_metrics(self, rows: list[dict]) -> dict:
        finished = [
            row for row in rows
            if row.get("finish_position") is not None and row.get("final_rank") is not None
        ]
        winner = next((row for row in finished if int(row["finish_position"]) == 1), None)
        predicted_top3 = {row["horse_number"] for row in rows if row.get("final_rank") and int(row["final_rank"]) <= 3}
        actual_top3 = {row["horse_number"] for row in finished if int(row["finish_position"]) <= 3}
        ranked_rows = [row for row in rows if row.get("final_rank") is not None]
        predicted_top5 = {
            row["horse_number"] for row in ranked_rows if int(row["final_rank"]) <= 5
        }
        actual_top5 = {
            row["horse_number"] for row in finished if int(row["finish_position"]) <= 5
        }
        top5_complete = len(ranked_rows) >= 5 and len(finished) >= 5
        top5_capture_count = len(predicted_top5 & actual_top5) if top5_complete else None
        winner_numbers = {row["horse_number"] for row in finished if int(row["finish_position"]) == 1}
        return {
            "winner_predicted_rank": winner.get("final_rank") if winner else None,
            "top3_capture_count": len(predicted_top3 & actual_top3),
            "top3_capture_rate": round(len(predicted_top3 & actual_top3) / len(actual_top3), 6) if actual_top3 else None,
            "winner_in_top5": bool(winner_numbers & predicted_top5) if winner_numbers and len(ranked_rows) >= 5 else None,
            "top3_in_predicted_top5": len(actual_top3 & predicted_top5) if top5_complete else None,
            "top5_capture_count": top5_capture_count,
            "top5_capture_rate": round(min(top5_capture_count, 5) / 5, 6) if top5_capture_count is not None else None,
            "top5_capture_4of5": top5_capture_count >= 4 if top5_capture_count is not None else None,
            "top5_capture_5of5": top5_capture_count >= 5 if top5_capture_count is not None else None,
            "mean_rank_error": round(
                mean(abs(int(row["final_rank"]) - int(row["finish_position"])) for row in finished), 6
            ) if finished else None,
        }

    def _brier(self, probability: float, actual: float) -> float:
        return (float(probability) - actual) ** 2

    def _log_loss(self, probability: float, actual: float) -> float:
        p = min(0.999999, max(0.000001, float(probability)))
        return -(actual * math.log(p) + (1.0 - actual) * math.log(1.0 - p))

    def _user_probability(self, user: UserPredictionInput | None) -> float | None:
        if user is None:
            return None
        if user.user_win_probability is not None:
            return user.user_win_probability
        if user.mark is None:
            return None
        return MARK_PROBABILITIES.get(user.mark)

    def _nullable_mean(self, values: list[float | None]) -> float | None:
        present = [value for value in values if value is not None]
        return round(mean(present), 6) if present else None

    def _user_probability_summary(self, rows: list[dict]) -> dict:
        values = [row["user_probability"] for row in rows if row["user_probability"] is not None]
        total = round(sum(values), 6)
        coverage = round(len(values) / len(rows), 6) if rows else 0.0
        if not values:
            status = "missing"
            note = "自分予想の印または勝率がありません。"
        elif total > 1.05:
            status = "over_total"
            note = "自分予想の勝率換算合計が100%を超えています。"
        elif total < 0.6:
            status = "under_total"
            note = "自分予想の勝率換算合計が低めです。"
        else:
            status = "ok"
            note = "自分予想の勝率換算合計はレビュー比較に使いやすい範囲です。"
        return {
            "probability_total": total,
            "filled_count": len(values),
            "horse_count": len(rows),
            "coverage": coverage,
            "status": status,
            "note": note,
        }

    def _comparison(self, ai_brier_score: float | None, user_brier_score: float | None) -> dict:
        if ai_brier_score is None or user_brier_score is None:
            return {"winner": None, "brier_delta": None, "note": "AIと自分予想の比較には自分の印または勝率が必要です。"}
        delta = round(user_brier_score - ai_brier_score, 6)
        if abs(delta) < 0.000001:
            winner = "tie"
            note = "AIと自分予想は同程度でした。"
        elif delta > 0:
            winner = "ai"
            note = "このレースではAI予測のBrierが良好でした。"
        else:
            winner = "user"
            note = "このレースでは自分予想のBrierが良好でした。"
        return {"winner": winner, "brier_delta": delta, "note": note}

    def _insights(self, rows: list[dict]) -> dict:
        ranked_rows = [row for row in rows if row.get("absolute_finish_rank_error") is not None]
        return {
            "biggest_ai_misses": [
                self._insight_row(row, "absolute_finish_rank_error")
                for row in sorted(
                    ranked_rows,
                    key=lambda row: (row["absolute_finish_rank_error"], row["ai_brier"]),
                    reverse=True,
                )[:3]
            ],
            "biggest_user_misses": [
                self._insight_row(row, "user_brier")
                for row in sorted(
                    [row for row in rows if row["user_brier"] is not None],
                    key=lambda row: row["user_brier"],
                    reverse=True,
                )[:3]
            ],
            "largest_disagreements": [
                self._disagreement_row(row)
                for row in sorted(
                    [row for row in rows if row["user_probability"] is not None],
                    key=lambda row: abs(row["ai_probability"] - row["user_probability"]),
                    reverse=True,
                )[:3]
            ],
        }

    def _factor_impacts(self, score_factors: list[str]) -> list[dict]:
        impacts = []
        for factor in score_factors:
            if str(factor).startswith("学習補正 "):
                continue
            amount = self._factor_amount(factor)
            if amount is None:
                direction = "neutral"
            elif amount > 0:
                direction = "positive"
            elif amount < 0:
                direction = "negative"
            else:
                direction = "neutral"
            impacts.append(
                {
                    "factor": factor,
                    "factor_key": self._factor_key(factor),
                    "direction": direction,
                    "amount": amount,
                }
            )
        return impacts

    def _factor_amount(self, factor: str) -> float | None:
        match = re.search(r"([+-]\d+(?:\.\d+)?)$", factor.strip())
        return float(match.group(1)) if match else None

    def _factor_key(self, factor: str) -> str:
        stripped = factor.strip()
        stripped = re.sub(r"\s+[+-]\d+(?:\.\d+)?$", "", stripped)
        stripped = re.sub(r"[+-]\d+(?:\.\d+)?kg", "{kg}", stripped)
        stripped = re.sub(r"\d+(?:\.\d+)?m", "{meters}m", stripped)
        return stripped

    def _factor_attribution(self, rows: list[dict]) -> dict:
        return {
            "overvalued_positive_factors": self._factor_rows(
                row
                for row in rows
                if row["actual_win"] == 0.0 and row["ai_probability"] >= 0.08
            ),
            "winner_positive_factors": self._factor_rows(
                row
                for row in rows
                if row["actual_win"] == 1.0
            ),
            "winner_negative_factors": self._factor_rows(
                (row, "negative")
                for row in rows
                if row["actual_win"] == 1.0
            ),
            "rank_overvalued_positive_factors": self._factor_rows(
                row
                for row in rows
                if row.get("finish_rank_error") is not None and row["finish_rank_error"] >= 3
            ),
            "rank_undervalued_negative_factors": self._factor_rows(
                (row, "negative")
                for row in rows
                if row.get("finish_rank_error") is not None and row["finish_rank_error"] <= -3
            ),
            "note": "rank_overvalued/undervaluedは予測順位と実着順が3順位以上ずれた全馬の要因です。件数が少ない間は原因候補として扱います。",
        }

    def _factor_rows(self, rows) -> list[dict]:
        buckets: dict[str, dict] = {}
        for item in rows:
            if isinstance(item, tuple):
                row, direction_filter = item
            else:
                row = item
                direction_filter = "positive"
            for impact in row.get("factor_impacts", []):
                if impact.get("direction") != direction_filter:
                    continue
                factor_key = impact.get("factor_key") or self._factor_key(impact["factor"])
                bucket = buckets.setdefault(
                    factor_key,
                    {
                        "factor_key": factor_key,
                        "factor": impact["factor"],
                        "count": 0,
                        "total_amount": 0.0,
                        "ai_probabilities": [],
                        "ai_briers": [],
                        "rank_errors": [],
                        "samples": [],
                    },
                )
                bucket["count"] += 1
                bucket["total_amount"] += float(impact.get("amount") or 0.0)
                bucket["ai_probabilities"].append(float(row["ai_probability"]))
                bucket["ai_briers"].append(float(row["ai_brier"]))
                if row.get("absolute_finish_rank_error") is not None:
                    bucket["rank_errors"].append(float(row["absolute_finish_rank_error"]))
                if len(bucket["samples"]) < 3:
                    bucket["samples"].append(
                        {
                            "horse_number": row["horse_number"],
                            "horse_name": row["horse_name"],
                            "finish_position": row["finish_position"],
                            "ai_probability": row["ai_probability"],
                            "ai_brier": round(row["ai_brier"], 6),
                            "predicted_rank": row.get("final_rank"),
                            "rank_error": row.get("finish_rank_error"),
                        }
                    )
        result = []
        for bucket in buckets.values():
            result.append(
                {
                    "factor_key": bucket["factor_key"],
                    "factor": bucket["factor"],
                    "count": bucket["count"],
                    "average_amount": round(bucket["total_amount"] / bucket["count"], 6),
                    "average_ai_probability": round(mean(bucket["ai_probabilities"]), 6),
                    "average_ai_brier": round(mean(bucket["ai_briers"]), 6),
                    "average_absolute_rank_error": (
                        round(mean(bucket["rank_errors"]), 6) if bucket["rank_errors"] else None
                    ),
                    "samples": bucket["samples"],
                    "statistical_note": "sample_small" if bucket["count"] < 30 else "ok",
                }
            )
        return sorted(
            result,
            key=lambda item: (
                item.get("average_absolute_rank_error") or 0.0,
                item["average_ai_brier"],
                item["count"],
            ),
            reverse=True,
        )[:10]

    def _insight_row(self, row: dict, score_key: str) -> dict:
        return {
            "horse_number": row["horse_number"],
            "horse_name": row["horse_name"],
            "finish_position": row["finish_position"],
            "result_status": row["result_status"],
            "actual_win": row["actual_win"],
            "ai_probability": row["ai_probability"],
            "user_probability": row["user_probability"],
            "predicted_rank": row.get("final_rank"),
            "rank_error": row.get("finish_rank_error"),
            "rank_error_direction": row.get("rank_error_direction"),
            "score": round(row[score_key], 6) if row[score_key] is not None else None,
        }

    def _disagreement_row(self, row: dict) -> dict:
        return {
            "horse_number": row["horse_number"],
            "horse_name": row["horse_name"],
            "ai_probability": row["ai_probability"],
            "user_probability": row["user_probability"],
            "difference": round(abs(row["ai_probability"] - row["user_probability"]), 6),
            "finish_position": row["finish_position"],
            "result_status": row["result_status"],
        }

    def _user_ratings(self, user: UserPredictionInput | None) -> dict[str, int | None]:
        if user is None:
            return {}
        return {
            "track_rating": user.track_rating,
            "pedigree_rating": user.pedigree_rating,
            "jockey_rating": user.jockey_rating,
            "rotation_rating": user.rotation_rating,
            "pace_rating": user.pace_rating,
        }
