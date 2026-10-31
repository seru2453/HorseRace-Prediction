from __future__ import annotations

import math
import re
from dataclasses import dataclass
from statistics import mean, pstdev

from services.common.models import HorseFeature, PredictionData
from services.features.generator import FEATURE_VERSION
from services.prediction.config import DEFAULT_STABILITY_CONFIG, StabilityConfig

MODEL_VERSION = "baseline-v1"
PARAMETER_VERSION = "rule-based-stable-v33"


@dataclass(frozen=True)
class FactorImpact:
    key: str
    label: str
    group: str
    amount: float


_FACTOR_GROUPS = {
    "FORM": ("近走", "前走複勝圏", "前走頭数補正", "前走大敗", "頭数補正着順", "上位半分率", "直近重み"),
    "JEV": ("Jev定性評価",),
    "REBOUND": ("一変", "リバウンド"),
    "TRACK_BIAS": ("トラックバイアス",),
    "PEDIGREE": ("父系", "母父", "血統"),
    "JOCKEY": ("同騎手", "継続騎乗"),
    "COURSE": ("同競馬場", "コース枠", "内枠"),
    "DISTANCE": ("同距離", "距離替わり"),
    "SURFACE": ("同馬場", "馬場替わり"),
    "ROTATION": ("間隔", "ローテ", "標準ローテ", "余裕ローテ", "使い詰め", "長期休養"),
    "PACE": ("先行", "差し", "道中押し上げ"),
    "MARKET": ("単勝オッズ", "市場", "メンバー内市場", "人気"),
    "LEARNING": ("学習補正",),
    "PHYSICAL": ("斤量", "前走比斤量", "馬体重", "年齢"),
    "ABILITY": (
        "基礎点", "基礎能力指数ブレンド", "通算", "メンバー内通算", "賞金", "メンバー内賞金", "年齢", "人気以上", "人気薄",
        "重賞", "大幅格上挑戦", "格上挑戦", "相手関係", "上がり3F", "補正後走破速度", "近走走破速度",
    ),
}


def _factor_impact(factor: str) -> FactorImpact | None:
    match = re.search(r"([+-]\d+(?:\.\d+)?)$", factor.strip())
    if not match:
        return None
    label = factor[:match.start()].strip()
    group = next((name for name, prefixes in _FACTOR_GROUPS.items() if any(label.startswith(p) for p in prefixes)), None)
    if group is None:
        return None
    return FactorImpact(key=label, label=label, group=group, amount=float(match.group(1)))


def _soft_saturate(raw: float, cap: float) -> float:
    if cap <= 0:
        return 0.0
    return cap * math.tanh(raw / cap)


def _composite_factor_amount(group: str, impacts: list[FactorImpact]) -> float:
    if group == "MARKET":
        buckets = {
            "odds": [item.amount for item in impacts if item.label.startswith("単勝オッズ")],
            "market_position": [
                item.amount for item in impacts
                if item.label.startswith(("市場", "メンバー内市場", "市場人気"))
            ],
        }
    else:
        buckets = {
            "recent_performance": [
                item.amount for item in impacts
                if item.label.startswith(("近走平均着順", "近走複勝率", "前走複勝圏", "前走大敗気味", "近走勝ち切り", "近走凡走率", "近走上向き", "近走下降気味"))
            ],
            "consistency": [
                item.amount for item in impacts
                if item.label.startswith(("近走安定", "近走振れ幅大", "前走頭数補正", "頭数補正着順", "上位半分率"))
            ],
            "weighted_recent": [item.amount for item in impacts if item.label.startswith("直近重み")],
        }
    return sum(sum(values) / len(values) for values in buckets.values() if values)


class RuleBasedPredictor:
    def __init__(
        self,
        factor_adjustments: dict[str, float] | None = None,
        stability_config: StabilityConfig | None = None,
    ) -> None:
        self.factor_adjustments = factor_adjustments or {}
        self.config = stability_config or DEFAULT_STABILITY_CONFIG

    def predict(self, features: list[HorseFeature]) -> list[PredictionData]:
        active = [item for item in features if not item.features.get("scratched")]
        details = [self._score_detail(item) for item in active]
        base_ranks = self._rank_map([detail["base_ability_score"] for detail in details])
        initial_final_ranks = self._rank_map([detail["final_score"] for detail in details])
        for detail, base_rank, final_rank in zip(details, base_ranks, initial_final_ranks, strict=True):
            detail["base_rank"] = base_rank
            detail["initial_rank_delta"] = base_rank - final_rank
            if abs(detail["initial_rank_delta"]) >= self.config.rank_delta_high_risk and not detail["sufficient_evidence"]:
                detail["condition_adjustment"] *= 0.5
                detail["final_score"] = max(0.05, detail["base_ability_score"] + detail["condition_adjustment"])
                detail["hard_cap_final_score"] = max(
                    0.05, detail["base_ability_score"] + detail["hard_cap_condition_adjustment"] * 0.5
                )
                detail["soft_saturation_final_score"] = max(
                    0.05, detail["base_ability_score"] + detail["soft_saturation_condition_adjustment"] * 0.5
                )
                detail["factors"].append("順位変動ガード 条件補正×0.50")
        final_ranks = self._rank_map([detail["final_score"] for detail in details])
        raw_probabilities, calibrated, confidences, uncertainties = self._probabilities(
            active, details, base_ranks, final_ranks, "final_score"
        )
        soft_ranks = self._rank_map([detail["soft_saturation_final_score"] for detail in details])
        soft_raw, soft_calibrated, _, _ = self._probabilities(
            active, details, base_ranks, soft_ranks, "soft_saturation_final_score"
        )
        diagnostics = self._probability_diagnostics(
            [item.horse_id for item in active], details, raw_probabilities, calibrated
        )
        predictions: list[PredictionData] = []
        for item, detail, raw_probability, win, soft_raw_probability, soft_win, confidence, uncertainty, base_rank, final_rank in zip(
            active,
            details,
            raw_probabilities,
            calibrated,
            soft_raw,
            soft_calibrated,
            confidences,
            uncertainties,
            base_ranks,
            final_ranks,
            strict=True,
        ):
            rank_delta = base_rank - final_rank
            predictions.append(
                PredictionData(
                    race_id=item.race_id,
                    horse_id=item.horse_id,
                    horse_number=item.horse_number,
                    horse_name=item.horse_name,
                    base_score=round(detail["final_score"], 6),
                    win_probability=round(win, 6),
                    top2_probability=round(min(0.98, win * 1.85 + 0.04), 6),
                    top3_probability=round(min(0.995, win * 2.65 + 0.08), 6),
                    uncertainty=round(uncertainty, 4),
                    confidence=round(confidence, 4),
                    explanation=self._explain(item, detail["factors"]),
                    model_version=MODEL_VERSION,
                    feature_version=FEATURE_VERSION,
                    champion_version=MODEL_VERSION,
                    base_ability_score=round(detail["base_ability_score"], 6),
                    ability_index=(
                        round(float(item.features["base_ability_index"]), 6)
                        if item.features.get("base_ability_index") is not None
                        else None
                    ),
                    ability_components={
                        "finish_quality": item.features.get("weighted_finish_quality"),
                        "margin_score": item.features.get("weighted_margin_score"),
                        "opponent_score": item.features.get("weighted_opponent_score"),
                        "track_adjusted_speed_score": item.features.get(
                            "weighted_track_adjusted_speed_score"
                        ),
                    },
                    ability_sample_status=str(item.features.get("ability_sample_status") or "UNKNOWN"),
                    raw_condition_adjustment=round(detail["raw_condition_adjustment"], 6),
                    condition_adjustment=round(detail["condition_adjustment"], 6),
                    feature_group_adjustments={
                        key: round(value, 6) for key, value in detail["feature_group_adjustments"].items()
                    },
                    raw_group_adjustments={
                        key: round(value, 6) for key, value in detail["raw_group_adjustments"].items()
                    },
                    hard_cap_final_score=round(detail["hard_cap_final_score"], 6),
                    soft_saturation_final_score=round(detail["soft_saturation_final_score"], 6),
                    score_stability_mode="soft_saturation" if self.config.use_soft_saturation else "hard_cap",
                    soft_saturation_raw_probability=round(soft_raw_probability, 6),
                    soft_saturation_calibrated_probability=round(soft_win, 6),
                    unclassified_score_factors=detail["unclassified_score_factors"],
                    raw_probability=round(raw_probability, 6),
                    calibrated_probability=round(win, 6),
                    probability_diagnostics=diagnostics[item.horse_id],
                    calibration_version="score-power-v1+conservative-uniform-shrinkage-v1",
                    base_rank=base_rank,
                    final_rank=final_rank,
                    rank_delta=rank_delta,
                    largest_positive_adjustment=round(detail["largest_positive_adjustment"], 6),
                    largest_negative_adjustment=round(detail["largest_negative_adjustment"], 6),
                    adjustment_source=detail["adjustment_source"],
                    confidence_label=self._confidence_label(confidence),
                    condition_sample_statuses={
                        key: str(item.features.get(key) or "UNKNOWN")
                        for key in (
                            "ability_sample_status",
                            "same_distance_sample_status",
                            "same_surface_sample_status",
                            "same_racecourse_sample_status",
                            "same_jockey_sample_status",
                            "sire_condition_sample_status",
                            "damsire_condition_sample_status",
                            "track_bias_sample_status",
                        )
                    },
                    new_horse_flags={
                        "career_shallow": item.features.get("career_starts") is None
                        or int(item.features.get("career_starts") or 0) <= 2,
                        "first_surface": bool(item.features.get("first_surface")),
                        "first_distance": bool(item.features.get("first_distance")),
                        "first_racecourse": bool(item.features.get("first_racecourse")),
                    },
                    frame_number=item.features.get("frame_number"),
                    running_style=item.features.get("running_style"),
                    average_first_corner_ratio=item.features.get("average_first_corner_ratio"),
                    field_early_pressure=item.features.get("field_early_pressure"),
                    track_bias_adjustment=float(item.features.get("track_bias_adjustment") or 0.0),
                    track_bias_sample_status=str(item.features.get("track_bias_sample_status") or "UNKNOWN"),
                    parameter_version=(
                        f"{PARAMETER_VERSION}+soft-saturation-challenger-v1"
                        if self.config.use_soft_saturation
                        else f"{PARAMETER_VERSION}+stability-v1"
                    ),
                    qualitative_adjustment=0.0,
                    qualitative_confidence=(
                        round(float((item.features.get("jev_qualitative") or {})["confidence"]), 4)
                        if (item.features.get("jev_qualitative") or {}).get("confidence") is not None
                        else None
                    ),
                    qualitative_status=str((item.features.get("jev_qualitative") or {}).get("status") or "disabled"),
                    qualitative_model_version=(item.features.get("jev_qualitative") or {}).get("model"),
                    qualitative_input_hash=(item.features.get("jev_qualitative") or {}).get("input_hash"),
                    current_form=item.features.get("current_form"),
                    condition_fit=item.features.get("condition_fit"),
                    previous_race_residual=item.features.get("previous_race_residual"),
                    rebound_score=item.features.get("rebound_score"),
                    field_size=item.features.get("field_size"),
                    jev_features={
                        key: item.features.get(key)
                        for key in item.features
                        if key.startswith("jev_")
                        and (
                            item.features.get(key) is None
                            or isinstance(item.features.get(key), (int, float))
                        )
                    },
                )
            )
        return sorted(predictions, key=lambda p: p.win_probability, reverse=True)

    def _probabilities(
        self,
        active: list[HorseFeature],
        details: list[dict],
        base_ranks: list[int],
        final_ranks: list[int],
        score_key: str,
    ) -> tuple[list[float], list[float], list[float], list[float]]:
        scores = [float(detail[score_key]) ** self.config.probability_score_exponent for detail in details]
        total = sum(scores) or 1.0
        raw_probabilities = [score / total for score in scores]
        uniform = 1.0 / len(active) if active else 0.0
        calibrated = []
        confidences = []
        uncertainties = []
        for item, detail, base_rank, final_rank, raw_probability in zip(
            active, details, base_ranks, final_ranks, raw_probabilities, strict=True
        ):
            rank_delta = base_rank - final_rank
            confidence = self._confidence(item, rank_delta, detail["largest_absolute_adjustment"])
            uncertainty = max(self.config.uncertainty_min, min(self.config.uncertainty_max, 1.0 - confidence))
            shrinkage = min(self.config.probability_shrinkage_max, uncertainty * 0.35)
            calibrated.append(raw_probability * (1.0 - shrinkage) + uniform * shrinkage)
            confidences.append(confidence)
            uncertainties.append(uncertainty)
        calibrated_total = sum(calibrated) or 1.0
        calibrated = [value / calibrated_total for value in calibrated]
        return raw_probabilities, calibrated, confidences, uncertainties

    def _probability_diagnostics(
        self,
        horse_ids: list[str],
        details: list[dict],
        raw_probabilities: list[float],
        calibrated_probabilities: list[float],
    ) -> dict[str, dict[str, float | None]]:
        count = len(details)
        scores = [float(detail["final_score"]) for detail in details]
        score_mean = mean(scores) if scores else 0.0
        score_stddev = pstdev(scores) if len(scores) > 1 else 0.0
        raw_stddev = pstdev(raw_probabilities) if len(raw_probabilities) > 1 else 0.0
        calibrated_stddev = pstdev(calibrated_probabilities) if len(calibrated_probabilities) > 1 else 0.0
        ordered_probabilities = sorted(calibrated_probabilities, reverse=True)
        leader = ordered_probabilities[0] if ordered_probabilities else None
        gap_to_second = leader - ordered_probabilities[1] if len(ordered_probabilities) > 1 else None
        gap_to_fifth = leader - ordered_probabilities[4] if len(ordered_probabilities) > 4 else None
        entropy = -sum(probability * math.log(probability) for probability in calibrated_probabilities if probability > 0)
        normalized_entropy = entropy / math.log(count) if count > 1 else 0.0
        calibration_spread_ratio = calibrated_stddev / raw_stddev if raw_stddev > 0 else None
        output = {}
        for horse_id, detail, raw_probability in zip(horse_ids, details, raw_probabilities, strict=True):
            score = float(detail["final_score"])
            relative_index = 50.0 + 15.0 * ((score - score_mean) / score_stddev) if score_stddev > 0 else 50.0
            output[horse_id] = {
                "relative_score_index": round(relative_index, 6),
                "final_score_stddev": round(score_stddev, 6),
                "raw_probability_stddev": round(raw_stddev, 6),
                "calibrated_probability_stddev": round(calibrated_stddev, 6),
                "calibration_spread_ratio": round(calibration_spread_ratio, 6) if calibration_spread_ratio is not None else None,
                "normalized_entropy": round(normalized_entropy, 6),
                "leader_probability": round(leader, 6) if leader is not None else None,
                "leader_gap_to_second": round(gap_to_second, 6) if gap_to_second is not None else None,
                "leader_gap_to_fifth": round(gap_to_fifth, 6) if gap_to_fifth is not None else None,
                "raw_probability": round(raw_probability, 6),
            }
        return output

    def _score(self, item: HorseFeature) -> float:
        score, _ = self._score_breakdown(item)
        return score

    def _score_breakdown(self, item: HorseFeature) -> tuple[float, list[str]]:
        detail = self._score_detail(item)
        return detail["final_score"], detail["factors"]

    def _score_detail(self, item: HorseFeature) -> dict:
        raw_score, factors = self._raw_score_breakdown(item)
        grouped: dict[str, float] = {}
        grouped_impacts: dict[str, list[FactorImpact]] = {}
        unclassified: list[str] = []
        condition_impacts: list[FactorImpact] = []
        for factor in factors[1:]:
            impact = _factor_impact(factor)
            if impact is None:
                if self._factor_amount(factor) is not None:
                    unclassified.append(factor)
                continue
            if impact.group == "ABILITY":
                continue
            grouped_impacts.setdefault(impact.group, []).append(impact)
            condition_impacts.append(impact)
        legacy_grouped: dict[str, float] = {}
        challenger_grouped: dict[str, float] = {}
        for group, impacts in grouped_impacts.items():
            amounts = [impact.amount for impact in impacts]
            legacy_grouped[group] = sum(amounts)
            if group in {"FORM", "MARKET"}:
                # Combine correlated views into a few separate, readable signal families.
                challenger_grouped[group] = _composite_factor_amount(group, impacts)
            else:
                challenger_grouped[group] = sum(amounts)
        grouped = challenger_grouped if self.config.use_composite_form_market else legacy_grouped
        raw_condition = sum(impact.amount for impact in condition_impacts)
        legacy_base_ability = max(0.05, raw_score - raw_condition)
        base_ability = legacy_base_ability
        ability_index = item.features.get("base_ability_index")
        if ability_index is not None:
            indexed_base_ability = 1.0 + (
                float(ability_index) - self.config.ability_prior
            ) * self.config.ability_score_scale
            blend = self.config.ability_index_blend_weight
            base_ability = legacy_base_ability * (1.0 - blend) + indexed_base_ability * blend
            factors.append(f"基礎能力指数ブレンド {base_ability - legacy_base_ability:+.2f}")
        applied_groups: dict[str, float] = {}
        hard_groups: dict[str, float] = {}
        soft_groups: dict[str, float] = {}
        for group, value in legacy_grouped.items():
            cap = self.config.feature_group_caps.get(group, self.config.condition_adjustment_cap)
            hard_groups[group] = max(-cap, min(cap, value))
        for group, value in challenger_grouped.items():
            cap = self.config.feature_group_caps.get(group, self.config.condition_adjustment_cap)
            soft_groups[group] = _soft_saturate(value, cap)
        for group, value in grouped.items():
            cap = self.config.feature_group_caps.get(group, self.config.condition_adjustment_cap)
            applied_groups[group] = soft_groups[group] if self.config.use_soft_saturation else hard_groups[group]
            if abs(value - applied_groups[group]) >= 0.005:
                factors.append(f"安定化 {group}上限 {value:+.2f}→{applied_groups[group]:+.2f}")
        grouped_total = sum(applied_groups.values())
        hard_group_total = sum(hard_groups.values())
        soft_group_total = sum(soft_groups.values())
        hard_condition = max(-self.config.condition_adjustment_cap, min(self.config.condition_adjustment_cap, hard_group_total))
        soft_condition = _soft_saturate(soft_group_total, self.config.condition_adjustment_cap)
        applied_condition = soft_condition if self.config.use_soft_saturation else hard_condition
        if abs(grouped_total - applied_condition) >= 0.005:
            factors.append(f"安定化 条件補正上限 {grouped_total:+.2f}→{applied_condition:+.2f}")
        positive = [item for item in condition_impacts if item.amount > 0]
        negative = [item for item in condition_impacts if item.amount < 0]
        largest_positive = max((item.amount for item in positive), default=0.0)
        largest_negative = min((item.amount for item in negative), default=0.0)
        largest = max(condition_impacts, key=lambda item: abs(item.amount), default=None)
        sufficient_evidence = any(
            item.features.get(key) == "SUFFICIENT"
            for key in (
                "same_distance_sample_status",
                "same_surface_sample_status",
                "same_racecourse_sample_status",
                "same_jockey_sample_status",
                "sire_condition_sample_status",
                "damsire_condition_sample_status",
                "track_bias_sample_status",
            )
        )
        return {
            "base_ability_score": base_ability,
            "raw_condition_adjustment": raw_condition,
            "condition_adjustment": applied_condition,
            "feature_group_adjustments": applied_groups,
            "final_score": max(0.05, base_ability + applied_condition),
            "factors": factors,
            "largest_positive_adjustment": largest_positive,
            "largest_negative_adjustment": largest_negative,
            "largest_absolute_adjustment": abs(largest.amount) if largest else 0.0,
            "adjustment_source": largest.label if largest else None,
            "raw_group_adjustments": legacy_grouped,
            "unclassified_score_factors": unclassified,
            "hard_cap_final_score": max(0.05, base_ability + hard_condition),
            "soft_saturation_final_score": max(0.05, base_ability + soft_condition),
            "hard_cap_condition_adjustment": hard_condition,
            "soft_saturation_condition_adjustment": soft_condition,
            "soft_saturation_shadow": self.config.use_soft_saturation,
            "sufficient_evidence": sufficient_evidence,
        }

    def _factor_amount(self, factor: str) -> float | None:
        match = re.search(r"([+-]\d+(?:\.\d+)?)$", factor.strip())
        return float(match.group(1)) if match else None

    def _factor_group(self, factor: str) -> str | None:
        impact = _factor_impact(factor)
        return impact.group if impact else None

    def _rank_map(self, scores: list[float]) -> list[int]:
        order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
        ranks = [0] * len(scores)
        for rank, index in enumerate(order, start=1):
            ranks[index] = rank
        return ranks

    def _raw_score_breakdown(self, item: HorseFeature) -> tuple[float, list[str]]:
        f = item.features
        score = 1.0
        factors = ["基礎点 +1.00"]
        avg_finish = f.get("past_5_average_finish")
        top3_rate = f.get("past_5_top3_rate")
        if avg_finish:
            delta = max(0.0, (8.0 - float(avg_finish)) / 10.0)
            score += delta
            factors.append(f"近走平均着順 +{delta:.2f}")
        if top3_rate is not None:
            delta = float(top3_rate) * 0.45
            score += delta
            factors.append(f"近走複勝率 +{delta:.2f}")
        if f.get("last_run_top3") is True:
            score += 0.04
            factors.append("前走複勝圏 +0.04")
        last_run_ratio = f.get("last_run_finish_ratio")
        if last_run_ratio is not None:
            ratio = float(last_run_ratio)
            if ratio <= 0.35:
                score += 0.03
                factors.append("前走頭数補正 +0.03")
            elif ratio >= 0.75:
                score -= 0.05
                factors.append("前走大敗気味 -0.05")
        consistency_label = f.get("recent_consistency_label")
        if consistency_label == "stable" and top3_rate is not None and float(top3_rate) >= 0.4:
            score += 0.04
            factors.append("近走安定 +0.04")
        elif consistency_label == "volatile":
            score -= 0.04
            factors.append("近走振れ幅大 -0.04")
        finish_ratio = f.get("past_5_average_finish_ratio")
        if finish_ratio is not None:
            delta = max(-0.08, min(0.14, (0.55 - float(finish_ratio)) * 0.22))
            score += delta
            factors.append(f"頭数補正着順 {delta:+.2f}")
        top_half_rate = f.get("past_5_top_half_rate")
        if top_half_rate is not None:
            delta = float(top_half_rate) * 0.08
            score += delta
            factors.append(f"上位半分率 +{delta:.2f}")
        weighted_finish_ratio = f.get("weighted_recent_finish_ratio")
        if weighted_finish_ratio is not None:
            delta = max(-0.06, min(0.10, (0.5 - float(weighted_finish_ratio)) * 0.18))
            score += delta
            factors.append(f"直近重み着順 {delta:+.2f}")
        weighted_top3 = f.get("weighted_recent_top3_rate")
        if weighted_top3 is not None:
            delta = float(weighted_top3) * 0.08
            score += delta
            factors.append(f"直近重み複勝率 +{delta:.2f}")
        career_win_rate = f.get("career_win_rate")
        if career_win_rate is not None:
            delta = min(0.10, float(career_win_rate) * 0.25)
            score += delta
            factors.append(f"通算勝率 +{delta:.2f}")
        career_top3_rate = f.get("career_top3_rate")
        if career_top3_rate is not None:
            delta = min(0.10, float(career_top3_rate) * 0.16)
            score += delta
            factors.append(f"通算複勝率 +{delta:.2f}")
        prize_money = f.get("total_prize_money")
        if prize_money is not None:
            delta = min(0.08, float(prize_money) / 200_000_000 * 0.08)
            score += delta
            factors.append(f"賞金実績 +{delta:.2f}")
        prize_rank = f.get("field_total_prize_money_rank_ratio")
        if prize_rank is not None:
            ratio = float(prize_rank)
            if ratio <= 0.2:
                score += 0.06
                factors.append("メンバー内賞金上位 +0.06")
            elif ratio <= 0.4:
                score += 0.03
                factors.append("メンバー内賞金中上位 +0.03")
            elif ratio >= 0.8:
                score -= 0.03
                factors.append("メンバー内賞金下位 -0.03")
        career_win_rank = f.get("field_career_win_rate_rank_ratio")
        if career_win_rank is not None and float(career_win_rank) <= 0.25:
            score += 0.03
            factors.append("メンバー内通算勝率上位 +0.03")
        career_top3_rank = f.get("field_career_top3_rate_rank_ratio")
        if career_top3_rank is not None:
            ratio = float(career_top3_rank)
            if ratio <= 0.25:
                score += 0.04
                factors.append("メンバー内通算複勝率上位 +0.04")
            elif ratio >= 0.8:
                score -= 0.03
                factors.append("メンバー内通算複勝率下位 -0.03")
        age_band = f.get("age_band")
        if age_band == "prime":
            score += 0.04
            factors.append("年齢充実期 +0.04")
        elif age_band == "mature":
            score += 0.01
            factors.append("年齢成熟期 +0.01")
        elif age_band == "veteran":
            score -= 0.03
            factors.append("年齢ベテラン -0.03")
        elif age_band == "senior":
            score -= 0.06
            factors.append("年齢高齢 -0.06")
        overperformance = f.get("market_overperformance_rate")
        if overperformance is not None:
            delta = min(0.08, float(overperformance) * 0.08)
            score += delta
            factors.append(f"人気以上の好走 +{delta:.2f}")
        longshot_top3 = f.get("longshot_top3_rate")
        if longshot_top3 is not None and float(longshot_top3) > 0:
            delta = min(0.06, float(longshot_top3) * 0.06)
            score += delta
            factors.append(f"人気薄好走 +{delta:.2f}")
        graded_starts = f.get("recent_graded_starts")
        if graded_starts is not None and int(graded_starts) > 0:
            delta = min(0.05, int(graded_starts) * 0.015)
            score += delta
            factors.append(f"近走重賞経験 +{delta:.2f}")
        graded_top3 = f.get("recent_graded_top3_rate")
        if graded_top3 is not None and float(graded_top3) > 0:
            delta = min(0.08, float(graded_top3) * 0.08)
            score += delta
            factors.append(f"重賞好走率 +{delta:.2f}")
        grade_step = f.get("grade_step_from_last")
        if grade_step is not None:
            step = int(grade_step)
            if step >= 2:
                score -= 0.06
                factors.append("大幅格上挑戦 -0.06")
            elif step == 1:
                score -= 0.03
                factors.append("格上挑戦 -0.03")
            elif step <= -1:
                score += 0.04
                factors.append("相手関係緩和 +0.04")
        win_rate = f.get("past_5_win_rate")
        if win_rate is not None:
            delta = float(win_rate) * 0.35
            score += delta
            factors.append(f"近走勝ち切り +{delta:.2f}")
        bad_finish_rate = f.get("past_5_bad_finish_rate")
        if bad_finish_rate is not None and float(bad_finish_rate) > 0:
            delta = min(0.22, float(bad_finish_rate) * 0.25)
            score -= delta
            factors.append(f"近走凡走率 -{delta:.2f}")
        recent_3_avg = f.get("recent_3_average_finish")
        if avg_finish is not None and recent_3_avg is not None:
            trend = float(avg_finish) - float(recent_3_avg)
            if trend >= 0.75:
                score += 0.08
                factors.append("近走上向き +0.08")
            elif trend <= -0.75:
                score -= 0.08
                factors.append("近走下降気味 -0.08")
        same_distance_top3 = f.get("same_distance_shrunk_top3_rate", f.get("same_distance_top3_rate"))
        if same_distance_top3 is not None:
            delta = (float(same_distance_top3) - 0.35) * 0.16
            score += delta
            factors.append(f"同距離適性 {delta:+.2f}")
        same_distance_band_top3 = f.get("same_distance_band_shrunk_top3_rate", f.get("same_distance_band_top3_rate"))
        if same_distance_band_top3 is not None:
            delta = (float(same_distance_band_top3) - 0.35) * 0.10
            score += delta
            factors.append(f"同距離帯適性 {delta:+.2f}")
        same_surface_top3 = f.get("same_surface_shrunk_top3_rate", f.get("same_surface_top3_rate"))
        if same_surface_top3 is not None:
            delta = (float(same_surface_top3) - 0.35) * 0.12
            score += delta
            factors.append(f"同馬場適性 {delta:+.2f}")
        same_track_condition_top3 = f.get("same_track_condition_shrunk_top3_rate", f.get("same_track_condition_top3_rate"))
        if same_track_condition_top3 is not None:
            delta = (float(same_track_condition_top3) - 0.35) * 0.10
            score += delta
            factors.append(f"同馬場状態適性 {delta:+.2f}")
        same_racecourse_top3 = f.get("same_racecourse_shrunk_top3_rate", f.get("same_racecourse_top3_rate"))
        if same_racecourse_top3 is not None:
            delta = (float(same_racecourse_top3) - 0.35) * 0.14
            score += delta
            factors.append(f"同競馬場適性 {delta:+.2f}")
        same_surface_distance_top3 = f.get("same_surface_distance_shrunk_top3_rate", f.get("same_surface_distance_top3_rate"))
        if same_surface_distance_top3 is not None:
            delta = (float(same_surface_distance_top3) - 0.35) * 0.14
            score += delta
            factors.append(f"同馬場距離適性 {delta:+.2f}")
        same_surface_distance_band_top3 = f.get("same_surface_distance_band_shrunk_top3_rate", f.get("same_surface_distance_band_top3_rate"))
        if same_surface_distance_band_top3 is not None:
            delta = (float(same_surface_distance_band_top3) - 0.35) * 0.10
            score += delta
            factors.append(f"同馬場距離帯適性 {delta:+.2f}")
        same_racecourse_distance_top3 = f.get("same_racecourse_distance_shrunk_top3_rate", f.get("same_racecourse_distance_top3_rate"))
        if same_racecourse_distance_top3 is not None:
            delta = (float(same_racecourse_distance_top3) - 0.35) * 0.12
            score += delta
            factors.append(f"同競馬場距離適性 {delta:+.2f}")
        same_jockey_top3 = f.get("same_jockey_shrunk_top3_rate", f.get("same_jockey_top3_rate"))
        if same_jockey_top3 is not None:
            delta = (float(same_jockey_top3) - 0.35) * 0.10
            score += delta
            factors.append(f"同騎手実績 {delta:+.2f}")
        if f.get("same_jockey_as_last_run") is True:
            score += 0.03
            factors.append("継続騎乗 +0.03")
        sire_starts = int(f.get("sire_condition_starts") or 0)
        sire_rate = f.get("sire_condition_shrunk_top3_rate")
        if sire_starts > 0 and sire_rate is not None:
            delta = max(-0.04, min(0.04, (float(sire_rate) - self.config.prior_top3_rate) * 0.12))
            score += delta
            factors.append(f"父系条件傾向 {delta:+.2f}")
        damsire_starts = int(f.get("damsire_condition_starts") or 0)
        damsire_rate = f.get("damsire_condition_shrunk_top3_rate")
        if damsire_starts > 0 and damsire_rate is not None:
            delta = max(-0.03, min(0.03, (float(damsire_rate) - self.config.prior_top3_rate) * 0.08))
            score += delta
            factors.append(f"母父条件傾向 {delta:+.2f}")
        carried_weight = f.get("carried_weight")
        if carried_weight is not None:
            delta = max(-0.12, min(0.12, (57.0 - float(carried_weight)) * 0.025))
            score += delta
            factors.append(f"斤量 {delta:+.2f}")
        carried_weight_change = f.get("carried_weight_change_from_last")
        if carried_weight_change is not None:
            weight_delta = float(carried_weight_change)
            if weight_delta >= 2.0:
                delta = min(0.07, weight_delta * 0.015)
                score -= delta
                factors.append(f"前走比斤量+{weight_delta:.1f}kg -{delta:.2f}")
            elif weight_delta >= 1.0:
                score -= 0.02
                factors.append(f"前走比斤量+{weight_delta:.1f}kg -0.02")
            elif weight_delta <= -2.0:
                delta = min(0.05, abs(weight_delta) * 0.012)
                score += delta
                factors.append(f"前走比斤量{weight_delta:.1f}kg +{delta:.2f}")
            elif weight_delta <= -1.0:
                score += 0.02
                factors.append(f"前走比斤量{weight_delta:.1f}kg +0.02")
        weight_change = f.get("horse_weight_change")
        if weight_change is not None:
            if abs(int(weight_change)) <= 8:
                score += 0.05
                factors.append("馬体重変動 +0.05")
            elif abs(int(weight_change)) >= 10:
                score -= 0.07
                factors.append("馬体重変動大 -0.07")
        recent_weight_change = f.get("latest_recent_horse_weight_change")
        if recent_weight_change is not None:
            recent_delta = int(recent_weight_change)
            if abs(recent_delta) <= 8:
                score += 0.02
                factors.append("近走馬体重安定 +0.02")
            elif recent_delta <= -12:
                score -= 0.04
                factors.append("近走馬体重急減 -0.04")
            elif recent_delta >= 12:
                score -= 0.03
                factors.append("近走馬体重急増 -0.03")
        implied = f.get("implied_win_probability")
        if implied is not None:
            delta = min(0.18, max(0.0, float(implied) * 0.8))
            score += delta
            factors.append(f"単勝オッズ評価 +{delta:.2f}")
        market_share = f.get("field_implied_win_probability_share")
        if market_share is not None:
            share = float(market_share)
            if share >= 0.20:
                score += 0.08
                factors.append("市場支持シェア上位 +0.08")
            elif share >= 0.12:
                score += 0.04
                factors.append("市場支持シェア中位 +0.04")
            elif share <= 0.04:
                score -= 0.03
                factors.append("市場支持シェア下位 -0.03")
        market_rank = f.get("field_implied_win_probability_rank_ratio")
        if market_rank is not None:
            ratio = float(market_rank)
            if ratio <= 0.2:
                score += 0.04
                factors.append("メンバー内市場上位 +0.04")
            elif ratio >= 0.8:
                score -= 0.03
                factors.append("メンバー内市場下位 -0.03")
        popularity = f.get("popularity")
        if popularity is not None:
            pop = int(popularity)
            if pop <= 3:
                score += 0.12
                factors.append("市場人気上位 +0.12")
            elif pop <= 5:
                score += 0.07
                factors.append("市場人気中位 +0.07")
            elif pop >= 10:
                score -= 0.05
                factors.append("市場人気薄 -0.05")
        days_since = f.get("days_since_last_run")
        if days_since is not None:
            days = int(days_since)
            if days < 14:
                score -= 0.06
                factors.append("間隔短め -0.06")
            elif 21 <= days <= 70:
                score += 0.04
                factors.append("標準ローテ +0.04")
            elif 71 <= days <= 120:
                score += 0.02
                factors.append("余裕ローテ +0.02")
            elif days >= 180:
                score -= 0.08
                factors.append("長期休養明け -0.08")
        recent_run_count = f.get("recent_run_count_90d")
        if recent_run_count is not None and int(recent_run_count) >= 4:
            score -= 0.04
            factors.append("使い詰め気味 -0.04")
        avg_f3 = f.get("past_5_average_final_three_furlongs")
        if avg_f3 is not None and float(avg_f3) <= 35.5:
            score += 0.04
            factors.append("上がり3F +0.04")
        adjusted_speed_delta = f.get("weighted_adjusted_speed_delta_mps")
        if adjusted_speed_delta is None:
            adjusted_speed_delta = f.get("average_adjusted_speed_delta_mps")
        if adjusted_speed_delta is not None:
            speed_delta = float(adjusted_speed_delta)
            if speed_delta >= 0.25:
                score += 0.05
                factors.append("補正後走破速度 +0.05")
            elif speed_delta >= 0.08:
                score += 0.03
                factors.append("補正後走破速度 +0.03")
            elif speed_delta <= -0.20:
                score -= 0.04
                factors.append("補正後走破速度 -0.04")
        else:
            average_speed = f.get("average_recent_speed_mps")
            if average_speed is not None:
                speed = float(average_speed)
                if speed >= 16.7:
                    score += 0.03
                    factors.append("近走走破速度 +0.03")
        running_style = f.get("running_style")
        if running_style == "front" and f.get("distance") and int(f["distance"]) <= 2000:
            score += 0.04
            factors.append("先行脚質 +0.04")
        elif running_style == "closer" and f.get("distance") and int(f["distance"]) >= 2000:
            score += 0.02
            factors.append("差し脚質 +0.02")
        early_pressure = f.get("field_early_pressure")
        if early_pressure is not None and running_style:
            pressure = float(early_pressure)
            if pressure >= 0.55 and running_style == "closer":
                score += 0.04
                factors.append("先行圧高めで差し補正 +0.04")
            elif pressure >= 0.55 and running_style == "front":
                score -= 0.03
                factors.append("先行圧高め -0.03")
            elif pressure <= 0.35 and running_style == "front":
                score += 0.04
                factors.append("先行圧低めで前残り補正 +0.04")
        position_gain = f.get("average_position_gain")
        if position_gain is not None and float(position_gain) >= 2.0:
            score += 0.04
            factors.append("道中押し上げ +0.04")
        if f.get("surface_change") not in (None, "same"):
            score -= 0.06
            factors.append("馬場替わり -0.06")
        distance_change_abs = f.get("distance_change_abs")
        if distance_change_abs is not None and int(distance_change_abs) >= 100:
            meters = int(distance_change_abs)
            if meters >= 700:
                delta = 0.08
            elif meters >= 400:
                delta = 0.05
            else:
                delta = 0.03
            score -= delta
            factors.append(f"距離替わり{meters}m -{delta:.2f}")
        frame = f.get("frame_number")
        course_frame_bias = f.get("course_frame_bias")
        if course_frame_bias is not None:
            delta = float(course_frame_bias)
            if abs(delta) >= 0.005:
                score += delta
                factors.append(f"コース枠補正 {delta:+.2f}")
        elif frame in (1, 2) and f.get("distance") and int(f["distance"]) <= 1800:
            score += 0.04
            factors.append("内枠補正 +0.04")
        track_bias_adjustment = float(f.get("track_bias_adjustment") or 0.0)
        if abs(track_bias_adjustment) >= 0.005:
            score += track_bias_adjustment
            factors.append(f"トラックバイアス {track_bias_adjustment:+.2f}")
        score = self._apply_factor_adjustments(score, factors)
        return max(score, 0.05), factors

    def _apply_factor_adjustments(self, score: float, factors: list[str]) -> float:
        if not self.factor_adjustments:
            return score
        applied: set[str] = set()
        for factor in list(factors):
            factor_key = self._factor_key(factor)
            if factor_key in applied:
                continue
            adjustment = self.factor_adjustments.get(factor_key)
            if adjustment is None:
                continue
            delta = max(-0.05, min(0.05, float(adjustment)))
            if abs(delta) < 0.000001:
                continue
            score += delta
            applied.add(factor_key)
            factors.append(f"学習補正 {factor_key} {delta:+.2f}")
        return score

    def _factor_key(self, factor: str) -> str:
        stripped = factor.strip()
        if stripped.startswith("学習補正 "):
            stripped = stripped.removeprefix("学習補正 ").strip()
        stripped = re.sub(r"\s+[+-]\d+(?:\.\d+)?$", "", stripped)
        stripped = re.sub(r"[+-]\d+(?:\.\d+)?kg", "{kg}", stripped)
        stripped = re.sub(r"\d+(?:\.\d+)?m", "{meters}m", stripped)
        return stripped

    def _confidence(self, item: HorseFeature, rank_delta: int = 0, largest_adjustment: float = 0.0) -> float:
        confidence_features = {
            key: value for key, value in item.features.items() if not key.startswith("jev_")
        }
        available = sum(1 for value in confidence_features.values() if value is not None)
        total = len(confidence_features)
        data_ratio = available / total if total else 0.0
        unavailable_ratio = len(item.unavailable_features) / total if total else 0.0
        history_penalty = 0.14 if "past_5_average_finish" in item.unavailable_features else 0.0
        condition_penalty = 0.06 if "same_distance_top3_rate" in item.unavailable_features else 0.0
        career_starts = item.features.get("career_starts")
        if career_starts is None or int(career_starts) == 0:
            career_penalty = 0.18
        elif int(career_starts) <= 2:
            career_penalty = 0.10
        else:
            career_penalty = 0.0
        first_condition_penalty = min(
            0.08,
            sum(
                0.025
                for key in ("first_surface", "first_distance", "first_racecourse")
                if item.features.get(key) is True
            ),
        )
        if abs(rank_delta) >= self.config.rank_delta_high_risk:
            rank_penalty = 0.12
        elif abs(rank_delta) >= self.config.rank_delta_warning:
            rank_penalty = 0.06
        else:
            rank_penalty = 0.0
        extreme_penalty = 0.06 if largest_adjustment >= self.config.extreme_single_adjustment else 0.0
        raw = (
            0.35
            + data_ratio * 0.38
            - unavailable_ratio * 0.08
            - history_penalty
            - condition_penalty
            - career_penalty
            - first_condition_penalty
            - rank_penalty
            - extreme_penalty
        )
        return max(1.0 - self.config.uncertainty_max, min(1.0 - self.config.uncertainty_min, raw))

    def _confidence_label(self, confidence: float) -> str:
        if confidence >= 0.65:
            return "HIGH"
        if confidence >= 0.50:
            return "MEDIUM"
        return "LOW"

    def _explain(self, item: HorseFeature, factors: list[str]) -> dict[str, list[str]]:
        f = item.features
        plus: list[str] = []
        minus: list[str] = []
        if f.get("past_5_win_rate") is not None and f["past_5_win_rate"] >= 0.2:
            plus.append("近走で勝ち切った実績がある")
        if f.get("past_5_top3_rate") is not None and f["past_5_top3_rate"] >= 0.4:
            plus.append("近走の複勝圏率が相対的に高い")
        if f.get("last_run_top3") is True:
            plus.append("前走で複勝圏に入っている")
        if f.get("last_run_finish_ratio") is not None and float(f["last_run_finish_ratio"]) <= 0.35:
            plus.append("前走の頭数補正後着順が良好")
        if f.get("last_run_finish_ratio") is not None and float(f["last_run_finish_ratio"]) >= 0.75:
            minus.append("前走は頭数補正後でも大敗気味")
        if f.get("recent_consistency_label") == "stable":
            plus.append("近走の着順が安定している")
        if f.get("recent_consistency_label") == "volatile":
            minus.append("近走の着順に振れ幅がある")
        if f.get("past_5_average_finish_ratio") is not None and f["past_5_average_finish_ratio"] <= 0.35:
            plus.append("頭数補正後の近走着順が良好")
        if f.get("past_5_top_half_rate") is not None and f["past_5_top_half_rate"] >= 0.6:
            plus.append("近走で上位半分に入る率が高い")
        if f.get("weighted_recent_finish_ratio") is not None and f["weighted_recent_finish_ratio"] <= 0.35:
            plus.append("直近重視でも着順内容が良い")
        if f.get("weighted_recent_top3_rate") is not None and f["weighted_recent_top3_rate"] >= 0.5:
            plus.append("直近重視の複勝圏率が高い")
        if f.get("career_win_rate") is not None and f["career_win_rate"] >= 0.2:
            plus.append("通算勝率を補助的に評価")
        if f.get("career_top3_rate") is not None and f["career_top3_rate"] >= 0.45:
            plus.append("通算の複勝圏率が高め")
        if f.get("total_prize_money") is not None and int(f["total_prize_money"]) >= 100_000_000:
            plus.append("賞金実績が一定以上ある")
        if f.get("field_total_prize_money_rank_ratio") is not None and float(f["field_total_prize_money_rank_ratio"]) <= 0.2:
            plus.append("メンバー内で賞金実績が上位")
        if f.get("field_total_prize_money_rank_ratio") is not None and float(f["field_total_prize_money_rank_ratio"]) >= 0.8:
            minus.append("メンバー内では賞金実績が下位")
        if f.get("field_career_win_rate_rank_ratio") is not None and float(f["field_career_win_rate_rank_ratio"]) <= 0.25:
            plus.append("メンバー内で通算勝率が上位")
        if f.get("field_career_top3_rate_rank_ratio") is not None and float(f["field_career_top3_rate_rank_ratio"]) <= 0.25:
            plus.append("メンバー内で通算複勝率が上位")
        if f.get("field_career_top3_rate_rank_ratio") is not None and float(f["field_career_top3_rate_rank_ratio"]) >= 0.8:
            minus.append("メンバー内では通算複勝率が下位")
        if f.get("age_band") == "prime":
            plus.append("年齢面では充実期")
        if f.get("age_band") == "mature":
            plus.append("年齢面では成熟期")
        if f.get("age_band") in {"veteran", "senior"}:
            minus.append("年齢面では上積み余地を慎重に見る")
        if f.get("market_overperformance_rate") is not None and f["market_overperformance_rate"] >= 0.4:
            plus.append("近走で人気以上に走る傾向")
        if f.get("longshot_top3_rate") is not None and f["longshot_top3_rate"] > 0:
            plus.append("人気薄での好走実績がある")
        if f.get("recent_graded_starts") is not None and int(f["recent_graded_starts"]) > 0:
            plus.append("近走で重賞レベルを経験している")
        if f.get("recent_graded_top3_rate") is not None and float(f["recent_graded_top3_rate"]) >= 0.4:
            plus.append("近走重賞での好走実績を評価")
        if f.get("grade_step_from_last") is not None and int(f["grade_step_from_last"]) <= -1:
            plus.append("前走より相手関係が楽になる可能性")
        if f.get("grade_step_from_last") is not None and int(f["grade_step_from_last"]) >= 1:
            minus.append("前走より相手関係が強くなる可能性")
        if f.get("past_5_bad_finish_rate") is not None and f["past_5_bad_finish_rate"] >= 0.4:
            minus.append("近走の凡走率が高い")
        if (
            f.get("past_5_average_finish") is not None
            and f.get("recent_3_average_finish") is not None
            and float(f["past_5_average_finish"]) - float(f["recent_3_average_finish"]) >= 0.75
        ):
            plus.append("直近3走の平均着順が上向き")
        if f.get("same_distance_top3_rate") is not None and f["same_distance_top3_rate"] >= 0.5:
            plus.append("同距離での複勝圏率を評価")
        if f.get("same_distance_band_top3_rate") is not None and f["same_distance_band_top3_rate"] >= 0.5:
            plus.append("同じ距離帯での複勝圏率を評価")
        if f.get("same_surface_top3_rate") is not None and f["same_surface_top3_rate"] >= 0.5:
            plus.append("同じ馬場種別での複勝圏率を評価")
        if f.get("same_track_condition_top3_rate") is not None and f["same_track_condition_top3_rate"] >= 0.5:
            plus.append("同じ馬場状態での複勝圏率を評価")
        if f.get("same_racecourse_top3_rate") is not None and f["same_racecourse_top3_rate"] >= 0.5:
            plus.append("同じ競馬場での複勝圏率を評価")
        if f.get("same_surface_distance_top3_rate") is not None and f["same_surface_distance_top3_rate"] >= 0.5:
            plus.append("同じ馬場距離での複勝圏率を評価")
        if f.get("same_surface_distance_band_top3_rate") is not None and f["same_surface_distance_band_top3_rate"] >= 0.5:
            plus.append("同じ馬場距離帯での複勝圏率を評価")
        if f.get("same_racecourse_distance_top3_rate") is not None and f["same_racecourse_distance_top3_rate"] >= 0.5:
            plus.append("同じ競馬場距離での複勝圏率を評価")
        if f.get("same_jockey_top3_rate") is not None and f["same_jockey_top3_rate"] >= 0.5:
            plus.append("同じ騎手での近走複勝圏率を評価")
        if f.get("same_jockey_as_last_run") is True:
            plus.append("前走から継続騎乗")
        if f.get("horse_weight_change") is not None and abs(int(f["horse_weight_change"])) <= 8:
            plus.append("馬体重変動が許容範囲")
        if f.get("horse_weight_change") is not None and abs(int(f["horse_weight_change"])) >= 10:
            minus.append("馬体重変動が大きい")
        if f.get("latest_recent_horse_weight_change") is not None and abs(int(f["latest_recent_horse_weight_change"])) <= 8:
            plus.append("近走の馬体重推移が安定している")
        if f.get("latest_recent_horse_weight_change") is not None and int(f["latest_recent_horse_weight_change"]) <= -12:
            minus.append("近走で馬体重が大きく減っている")
        if f.get("latest_recent_horse_weight_change") is not None and int(f["latest_recent_horse_weight_change"]) >= 12:
            minus.append("近走で馬体重が大きく増えている")
        if f.get("carried_weight_change_from_last") is not None and float(f["carried_weight_change_from_last"]) <= -1.0:
            plus.append("前走より斤量が軽くなる")
        if f.get("carried_weight_change_from_last") is not None and float(f["carried_weight_change_from_last"]) >= 2.0:
            minus.append("前走より斤量が重くなる")
        if f.get("win_odds") is not None and float(f["win_odds"]) <= 10.0:
            plus.append("単勝オッズ上も一定の支持がある")
        if f.get("field_implied_win_probability_share") is not None and float(f["field_implied_win_probability_share"]) >= 0.20:
            plus.append("市場支持がメンバー内で厚い")
        if f.get("field_implied_win_probability_share") is not None and float(f["field_implied_win_probability_share"]) <= 0.04:
            minus.append("市場支持シェアは薄い")
        if f.get("field_implied_win_probability_rank_ratio") is not None and float(f["field_implied_win_probability_rank_ratio"]) <= 0.2:
            plus.append("単勝オッズ順位がメンバー内上位")
        if f.get("field_implied_win_probability_rank_ratio") is not None and float(f["field_implied_win_probability_rank_ratio"]) >= 0.8:
            minus.append("単勝オッズ順位はメンバー内下位")
        if f.get("popularity") is not None and int(f["popularity"]) <= 3:
            plus.append("市場人気上位を補助的に評価")
        if f.get("popularity") is not None and int(f["popularity"]) >= 10:
            minus.append("市場人気は薄め")
        if f.get("days_since_last_run") is not None and 21 <= int(f["days_since_last_run"]) <= 70:
            plus.append("前走からの間隔が標準的")
        if f.get("days_since_last_run") is not None and 71 <= int(f["days_since_last_run"]) <= 120:
            plus.append("前走から適度に間隔を空けている")
        if f.get("days_since_last_run") is not None and int(f["days_since_last_run"]) < 14:
            minus.append("前走からの間隔が短い")
        if f.get("days_since_last_run") is not None and int(f["days_since_last_run"]) >= 180:
            minus.append("長期休養明け")
        if f.get("recent_run_count_90d") is not None and int(f["recent_run_count_90d"]) >= 4:
            minus.append("90日内の出走回数が多め")
        if f.get("past_5_average_final_three_furlongs") is not None and float(f["past_5_average_final_three_furlongs"]) <= 35.5:
            plus.append("近走の上がり3Fが良好")
        if f.get("average_adjusted_speed_delta_mps") is not None and float(f["average_adjusted_speed_delta_mps"]) >= 0.08:
            plus.append("近走の走破時計を条件補正後の速度で評価")
        if f.get("average_adjusted_speed_delta_mps") is not None and float(f["average_adjusted_speed_delta_mps"]) <= -0.20:
            minus.append("近走の条件補正後速度は物足りない")
        if f.get("running_style") == "front":
            plus.append("近走では先行できる位置取り")
        if f.get("running_style") == "closer":
            plus.append("近走では差し寄りの脚質")
        if f.get("average_position_gain") is not None and float(f["average_position_gain"]) >= 2.0:
            plus.append("道中で位置を押し上げる傾向")
        if f.get("field_early_pressure") is not None and float(f["field_early_pressure"]) >= 0.55 and f.get("running_style") == "closer":
            plus.append("先行圧が高めで差しが届く余地")
        if f.get("field_early_pressure") is not None and float(f["field_early_pressure"]) <= 0.35 and f.get("running_style") == "front":
            plus.append("先行圧が低めで前残りの余地")
        if f.get("field_early_pressure") is not None and float(f["field_early_pressure"]) >= 0.55 and f.get("running_style") == "front":
            minus.append("先行馬が多く展開負荷が高め")
        if f.get("surface_change") not in (None, "same"):
            minus.append("前走から馬場種別が変わる")
        distance_change_abs = f.get("distance_change_abs")
        if distance_change_abs is not None and int(distance_change_abs) >= 600:
            minus.append("前走から距離条件が大きく変わる")
        elif distance_change_abs is not None and int(distance_change_abs) >= 100:
            minus.append("前走から距離条件が変わる")
        if f.get("course_frame_bias") is not None and float(f["course_frame_bias"]) >= 0.03:
            plus.append("コース形状上、枠順のロス軽減が見込める")
        if f.get("course_frame_bias") is not None and float(f["course_frame_bias"]) <= -0.02:
            minus.append("コース形状上、枠順のロスが出る可能性")
        if f.get("course_frame_bias") is None and f.get("frame_number") in (1, 2):
            plus.append("内枠によるロス軽減の可能性")
        if "past_5_average_finish" in item.unavailable_features:
            minus.append("過去走データが不足しているため簡易評価")
        if f.get("same_jockey_top3_rate") is None:
            minus.append("同騎手での近走データが不足")
        if not plus:
            plus.append("取得済み基本情報から大きな減点なし")
        return {"positive": plus, "negative": minus, "score_factors": factors}
