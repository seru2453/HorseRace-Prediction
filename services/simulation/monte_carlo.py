from __future__ import annotations

from collections import defaultdict
import math

import numpy as np

from services.common.models import PredictionData, ScenarioResult, SelectionNote, SimulationResult
from services.prediction.config import DEFAULT_STABILITY_CONFIG, StabilityConfig


class MonteCarloSimulator:
    def __init__(self, stability_config: StabilityConfig | None = None) -> None:
        self.config = stability_config or DEFAULT_STABILITY_CONFIG

    def simulate(self, race_id: str, predictions: list[PredictionData], iterations: int = 10_000, seed: int = 20260816) -> SimulationResult:
        if not 1_000 <= iterations <= 100_000:
            raise ValueError("iterations must be between 1,000 and 100,000.")
        ordered = sorted(predictions, key=lambda p: p.horse_number)
        rng = np.random.default_rng(seed)
        position_counts: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        score_counts: dict[int, float] = defaultdict(float)
        base_probability = np.array([max(1e-6, p.win_probability) for p in ordered], dtype=float)
        base_probability = base_probability / base_probability.sum()
        base_utility = np.log(base_probability)
        uncertainty = np.array([max(0.08, p.uncertainty) for p in ordered], dtype=float)
        temperature = 0.55 + uncertainty * 0.85

        for _ in range(iterations):
            start_noise = rng.normal(0.0, uncertainty * 0.18)
            day_noise = rng.gumbel(0.0, temperature, size=len(ordered))
            sampled = base_utility + start_noise + day_noise
            order = np.argsort(-sampled)
            for pos, idx in enumerate(order, start=1):
                horse_number = ordered[int(idx)].horse_number
                position_counts[horse_number][pos] += 1
                score_counts[horse_number] += float(np.exp(sampled[int(idx)]))

        ranking = []
        for p in ordered:
            dist = position_counts[p.horse_number]
            likely_position, likely_count = max(dist.items(), key=lambda item: item[1])
            ranking.append(
                {
                    "horse_number": p.horse_number,
                    "horse_name": p.horse_name,
                    "win_rate": round(dist[1] / iterations, 6),
                    "top2_rate": round((dist[1] + dist[2]) / iterations, 6),
                    "top3_rate": round((dist[1] + dist[2] + dist[3]) / iterations, 6),
                    "most_likely_position": likely_position,
                    "most_likely_position_rate": round(likely_count / iterations, 6),
                    "mean_sample_score": round(score_counts[p.horse_number] / iterations, 6),
                }
            )
        ranking.sort(key=lambda item: item["win_rate"], reverse=True)
        leader_win_rate = ranking[0]["win_rate"] if ranking else 0.0
        top3_cutline = ranking[2]["top3_rate"] if len(ranking) >= 3 else (ranking[-1]["top3_rate"] if ranking else 0.0)
        for index, item in enumerate(ranking, start=1):
            item["simulation_rank"] = index
            item["win_gap_to_leader"] = round(leader_win_rate - item["win_rate"], 6)
            item["top3_gap_to_cutline"] = round(item["top3_rate"] - top3_cutline, 6)
        distribution = {
            p.horse_number: {position: round(count / iterations, 6) for position, count in sorted(position_counts[p.horse_number].items())}
            for p in ordered
        }
        scenarios = self._scenarios(ordered)
        return SimulationResult(
            race_id=race_id,
            iterations=iterations,
            seed=seed,
            ranking=ranking,
            position_distribution=distribution,
            scenarios=scenarios,
            selection_notes=self._selection_notes(ranking, ordered),
            decision_summary=self._decision_summary(ranking, ordered),
            candidate_groups=self._candidate_groups(ranking),
            scenario_consensus=self._scenario_consensus(scenarios, ordered),
        )

    def _scenarios(self, predictions: list[PredictionData]) -> list[ScenarioResult]:
        by_score = sorted(predictions, key=lambda p: p.win_probability, reverse=True)
        frames = [p.frame_number for p in predictions if p.frame_number is not None]
        frame_min = min(frames) if frames else None
        frame_max = max(frames) if frames else None
        by_inner = sorted(
            predictions,
            key=lambda p: self._scenario_utility(p, 1.0, frame_min, frame_max),
            reverse=True,
        )
        by_outer = sorted(
            predictions,
            key=lambda p: self._scenario_utility(p, -1.0, frame_min, frame_max),
            reverse=True,
        )
        by_uncertain = sorted(predictions, key=lambda p: (p.uncertainty, p.base_score), reverse=True)
        return [
            ScenarioResult(name="平均ペース", top_horse_numbers=[p.horse_number for p in by_score[:3]], note="補正後勝率を中心にした代表展開"),
            ScenarioResult(name="内有利", top_horse_numbers=[p.horse_number for p in by_inner[:3]], note="能力を中心に、実枠・脚質・先行圧・取得済み馬場傾向を加味"),
            ScenarioResult(name="外有利", top_horse_numbers=[p.horse_number for p in by_outer[:3]], note="能力を中心に、実枠・脚質・先行圧・取得済み馬場傾向を加味"),
            ScenarioResult(name="波乱含み", top_horse_numbers=[p.horse_number for p in by_uncertain[:3]], note="モデル誤差と当日変動が大きい展開"),
        ]

    def _scenario_utility(
        self,
        prediction: PredictionData,
        orientation: float,
        frame_min: int | None,
        frame_max: int | None,
    ) -> float:
        utility = math.log(max(1e-6, prediction.win_probability))
        frame_position = self._frame_position(prediction.frame_number, frame_min, frame_max)
        early_position = self._early_position(prediction)
        utility += orientation * frame_position * self.config.scenario_frame_weight
        utility += orientation * early_position * self.config.scenario_style_weight
        utility += self._pace_fit(prediction, early_position) * self.config.scenario_pace_weight
        utility += prediction.track_bias_adjustment * self.config.scenario_track_bias_weight
        return utility

    def _frame_position(self, frame: int | None, frame_min: int | None, frame_max: int | None) -> float:
        if frame is None or frame_min is None or frame_max is None or frame_max == frame_min:
            return 0.0
        center = (frame_min + frame_max) / 2.0
        return max(-1.0, min(1.0, (center - frame) / ((frame_max - frame_min) / 2.0)))

    def _early_position(self, prediction: PredictionData) -> float:
        if prediction.average_first_corner_ratio is not None:
            ratio = max(0.0, min(1.0, prediction.average_first_corner_ratio))
            return 1.0 - ratio * 2.0
        return {"front": 0.8, "stalker": 0.3, "closer": -0.7}.get(prediction.running_style or "", 0.0)

    def _pace_fit(self, prediction: PredictionData, early_position: float) -> float:
        pressure = prediction.field_early_pressure
        if pressure is None:
            return 0.0
        if pressure >= 0.55:
            return -early_position
        if pressure <= 0.35:
            return early_position
        return 0.0

    def _selection_notes(self, ranking: list[dict], predictions: list[PredictionData]) -> list[SelectionNote]:
        if not ranking:
            return []
        by_number = {item.horse_number: item for item in predictions}
        notes = []
        leader = ranking[0]
        notes.append(self._note("本命候補", leader, by_number, 1, self._leader_risk(leader, by_number)))

        for priority, item in enumerate(ranking[1:3], start=2):
            notes.append(self._note("相手候補", item, by_number, priority, "medium"))

        longshots = [
            item
            for item in ranking[3:]
            if self._is_longshot_candidate(item, by_number.get(item["horse_number"]), len(ranking))
        ]
        if longshots:
            longshots.sort(
                key=lambda item: (
                    (by_number.get(item["horse_number"]).rebound_score or 0.0)
                    if by_number.get(item["horse_number"])
                    else 0.0,
                    item["top3_rate"],
                ),
                reverse=True,
            )
            notes.append(self._note("穴候補", longshots[0], by_number, 4, "high"))

        risk_messages = self._risk_messages(ranking, by_number)
        for index, message in enumerate(risk_messages, start=5):
            notes.append(
                SelectionNote(
                    label="注意点",
                    priority=index,
                    risk_level="high",
                    note=message,
                )
            )
        return notes[:7]

    def _is_longshot_candidate(
        self,
        item: dict,
        prediction: PredictionData | None,
        field_size: int,
    ) -> bool:
        stable_top3_case = item["top3_rate"] >= max(0.18, item["win_rate"] * 2.4)
        if prediction is None or prediction.rebound_score is None:
            return stable_top3_case
        ability_rank = prediction.base_rank
        rebound_case = (
            prediction.rebound_score >= 0.60
            and ability_rank is not None
            and ability_rank <= max(4, field_size // 2)
            and (prediction.previous_race_residual is None or prediction.previous_race_residual < 0)
            and prediction.uncertainty >= 0.30
        )
        return stable_top3_case or rebound_case

    def _note(self, label: str, item: dict, predictions: dict[int, PredictionData], priority: int, risk_level: str) -> SelectionNote:
        prediction = predictions.get(item["horse_number"])
        confidence = prediction.confidence if prediction else None
        note = {
            "本命候補": "勝率とシミュレーション順位が最上位です。",
            "相手候補": "上位候補に続く安定圏です。",
            "穴候補": "勝ち切りより複勝圏の残り目を評価します。",
        }.get(label, "")
        if confidence is not None and confidence < 0.5:
            note += " ただしデータ信頼度は低めです。"
        return SelectionNote(
            label=label,
            horse_number=item["horse_number"],
            horse_name=item["horse_name"],
            priority=priority,
            confidence=round(confidence, 4) if confidence is not None else None,
            win_rate=item["win_rate"],
            top3_rate=item["top3_rate"],
            risk_level=risk_level,
            note=note,
        )

    def _leader_risk(self, leader: dict, predictions: dict[int, PredictionData]) -> str:
        confidence = predictions[leader["horse_number"]].confidence
        if leader["win_rate"] >= 0.28 and confidence >= 0.58:
            return "low"
        if leader["win_rate"] >= 0.2 and confidence >= 0.5:
            return "medium"
        return "high"

    def _risk_messages(self, ranking: list[dict], predictions: dict[int, PredictionData]) -> list[str]:
        messages = []
        leader = ranking[0]
        second = ranking[1] if len(ranking) > 1 else None
        if leader["win_rate"] < 0.22:
            messages.append("抜けた本命が不在です。上位の序列は過信しない前提で見ます。")
        if second and leader["win_rate"] - second["win_rate"] < 0.035:
            messages.append("1番手と2番手の勝率差が小さく、頭固定の信頼度は低めです。")
        average_confidence = sum(item.confidence for item in predictions.values()) / len(predictions)
        if average_confidence < 0.55:
            messages.append("全体のデータ信頼度が低めです。結果レビューで補正する前提の分析です。")
        if len(ranking) >= 4 and ranking[2]["top3_rate"] - ranking[3]["top3_rate"] < 0.04:
            messages.append("複勝圏の3番手以降が接戦です。3列目候補は広めに確認します。")
        return messages

    def _decision_summary(self, ranking: list[dict], predictions: list[PredictionData]) -> dict:
        if not ranking:
            return {}
        by_number = {item.horse_number: item for item in predictions}
        leader = ranking[0]
        second = ranking[1] if len(ranking) > 1 else None
        fourth = ranking[3] if len(ranking) > 3 else None
        favorite_gap = round(leader["win_rate"] - second["win_rate"], 6) if second else None
        top3_gap = round(ranking[2]["top3_rate"] - fourth["top3_rate"], 6) if len(ranking) > 3 else None
        average_confidence = sum(item.confidence for item in predictions) / len(predictions)
        leader_confidence = by_number[leader["horse_number"]].confidence

        if leader["win_rate"] >= 0.26 and (favorite_gap is None or favorite_gap >= 0.06) and leader_confidence >= 0.58:
            stance = "favorite"
            label = "本命寄り"
            note = "1番手の勝率差と信頼度が比較的はっきりしています。"
        elif favorite_gap is not None and favorite_gap < 0.035:
            stance = "wide_open"
            label = "混戦"
            note = "1番手と2番手の差が小さく、上位の入れ替わりを前提に見ます。"
        elif top3_gap is not None and top3_gap < 0.04:
            stance = "top3_contested"
            label = "複勝圏混戦"
            note = "3番手以降の複勝圏が詰まっています。"
        elif average_confidence < 0.55:
            stance = "low_confidence"
            label = "信頼度低め"
            note = "取得データの信頼度が低めで、レビューによる補正前提です。"
        else:
            stance = "balanced"
            label = "標準"
            note = "大きな偏りはなく、上位候補を中心に確認します。"

        return {
            "stance": stance,
            "label": label,
            "favorite_gap": favorite_gap,
            "top3_gap": top3_gap,
            "leader_confidence": round(leader_confidence, 4),
            "average_confidence": round(average_confidence, 4),
            "note": note,
        }

    def _candidate_groups(self, ranking: list[dict]) -> dict:
        if not ranking:
            return {}
        core = [
            self._candidate_payload(item)
            for item in ranking
            if item["simulation_rank"] == 1 or item["win_gap_to_leader"] <= 0.035
        ][:3]
        core_numbers = {item["horse_number"] for item in core}
        opposition = [
            self._candidate_payload(item)
            for item in ranking
            if item["horse_number"] not in core_numbers and (item["simulation_rank"] <= 4 or item["top3_gap_to_cutline"] >= -0.03)
        ][:4]
        selected_numbers = core_numbers | {item["horse_number"] for item in opposition}
        reserves = [
            self._candidate_payload(item)
            for item in ranking
            if item["horse_number"] not in selected_numbers and (item["top3_rate"] >= 0.28 or item["top3_gap_to_cutline"] >= -0.08)
        ][:4]
        return {
            "core": {
                "label": "本命圏",
                "horses": core,
                "note": "勝率首位、または首位との差が小さい候補です。",
            },
            "opposition": {
                "label": "相手圏",
                "horses": opposition,
                "note": "上位入線の現実味が残る相手候補です。",
            },
            "reserves": {
                "label": "押さえ圏",
                "horses": reserves,
                "note": "勝ち切りより複勝圏の残り目を確認する候補です。",
            },
        }

    def _candidate_payload(self, item: dict) -> dict:
        return {
            "horse_number": item["horse_number"],
            "horse_name": item["horse_name"],
            "simulation_rank": item.get("simulation_rank"),
            "win_rate": item["win_rate"],
            "top3_rate": item["top3_rate"],
            "win_gap_to_leader": item.get("win_gap_to_leader"),
            "top3_gap_to_cutline": item.get("top3_gap_to_cutline"),
        }

    def _scenario_consensus(self, scenarios: list[ScenarioResult], predictions: list[PredictionData]) -> list[dict]:
        if not scenarios:
            return []
        names_by_number = {item.horse_number: item.horse_name for item in predictions}
        counts: dict[int, int] = defaultdict(int)
        scenario_names: dict[int, list[str]] = defaultdict(list)
        for scenario in scenarios:
            for horse_number in scenario.top_horse_numbers:
                counts[horse_number] += 1
                scenario_names[horse_number].append(scenario.name)
        rows = [
            {
                "horse_number": horse_number,
                "horse_name": names_by_number.get(horse_number, ""),
                "top3_scenario_count": count,
                "scenario_count": len(scenarios),
                "consensus_rate": round(count / len(scenarios), 6),
                "scenarios": scenario_names[horse_number],
            }
            for horse_number, count in counts.items()
        ]
        return sorted(rows, key=lambda item: (-item["top3_scenario_count"], item["horse_number"]))
