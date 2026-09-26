from __future__ import annotations

# ruff: noqa: E402 - project imports follow the CLI path bootstrap below.

import argparse
import csv
from datetime import date
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repository import RaceRepository
from database.session import make_session_factory
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.common.models import AnalysisResult, PredictionData
from services.data_provider.base import ProviderUnavailableError
from services.data_provider.jra_official import JraOfficialProvider
from services.data_provider.mock import MockProvider
from services.learning.review import PredictionReviewer
from services.learning.auto_review import AutomaticReviewService
from services.ml.baseline import (
    LOGISTIC_BASELINE_VERSION,
    LogisticBaselineTrainer,
    TrainingConfig,
    evaluate_challenger_dataset,
)


def provider_for_mode(mode: str):
    return JraOfficialProvider() if mode == "jra" else MockProvider()


def format_prediction_report(result: AnalysisResult) -> str:
    race = result.race
    race_title = f"{race.race_date.value} {race.racecourse.value} {race.race_number.value}R {race.race_name.value or ''}".strip()
    condition = (
        f"{race.surface.value or '-'}{race.distance.value or '-'}m / "
        f"天候 {race.weather.value or '-'} / 馬場 {race.track_condition.value or '-'} / {len(result.entries)}頭"
    )
    lines = [
        f"# {race_title}",
        condition,
        f"解析: {result.parse_quality.status} / {result.parse_quality.recognized_entries}/{result.parse_quality.expected_entries or '-'}頭 / issues {len(result.parse_quality.issues)}",
        "",
        "## AI予想 上位",
    ]
    top_win = max((item.win_probability for item in result.predictions), default=0.0)
    entries_by_number = {int(entry.horse_number.value): entry for entry in result.entries if entry.horse_number.value is not None}
    for index, prediction in enumerate(result.predictions[:5], start=1):
        strength = _strength_stars(prediction.win_probability, top_win)
        positives = "、".join(prediction.explanation.get("positive", [])[:5])
        negatives = "、".join(prediction.explanation.get("negative", [])[:5])
        market = _market_label(entries_by_number.get(prediction.horse_number))
        lines.extend(
            [
                f"{index}. {prediction.horse_number} {prediction.horse_name} {strength}",
                f"   勝率 {_percent(prediction.win_probability)} / 連対 {_percent(prediction.top2_probability)} / 複勝圏 {_percent(prediction.top3_probability)} / 信頼度 {_percent(prediction.confidence)}",
                f"   市場: {market}",
                f"   プラス: {positives or '-'}",
                f"   注意: {negatives or '-'}",
            ]
        )

    summary = result.simulation.decision_summary or {}
    lines.extend(
        [
            "",
            "## Simulation レース判定",
            f"{summary.get('label', '-')} / 1番手差 {_percent(summary.get('favorite_gap'))} / 複勝3番手差 {_percent(summary.get('top3_gap'))} / 平均信頼度 {_percent(summary.get('average_confidence'))}",
            summary.get("note", ""),
            "",
            "## 候補セット",
        ]
    )
    groups = result.simulation.candidate_groups or {}
    for key in ("core", "opposition", "reserves"):
        group = groups.get(key) or {}
        horses = group.get("horses") or []
        if not horses:
            continue
        rendered = " / ".join(f"{item['horse_number']} {item['horse_name']}({_percent(item['win_rate'])})" for item in horses)
        lines.append(f"{group.get('label', key)}: {rendered}")

    lines.extend(["", "## 展開一致度"])
    for item in (result.simulation.scenario_consensus or [])[:5]:
        lines.append(
            f"{item['horse_number']} {item['horse_name']}: {item['top3_scenario_count']}/{item['scenario_count']} "
            f"({_percent(item['consensus_rate'])}) {', '.join(item['scenarios'])}"
        )

    lines.extend(
        [
            "",
            "## 注意",
            "これは購入連携や利益保証ではなく、取得済みデータに基づく予想支援レポートです。",
        ]
    )
    return "\n".join(line for line in lines if line is not None)


def format_review_report(result: AnalysisResult, review: dict) -> str:
    return format_review_report_for_predictions(result.predictions, review)


def format_review_report_for_predictions(predictions: list[PredictionData], review: dict) -> str:
    rows = review.get("rows", [])
    winner_rows = [row for row in rows if row.get("finish_position") == 1]
    top3_rows = [row for row in rows if row.get("finish_position") is not None and row["finish_position"] <= 3]
    ai_order = {prediction.horse_number: index for index, prediction in enumerate(predictions, start=1)}
    winner_lines = [
        f"{row['horse_number']} {row['horse_name']} / AI順位 {ai_order.get(row['horse_number'], '-')} / AI勝率 {_percent(row.get('ai_probability'))}"
        for row in winner_rows
    ]
    top3_lines = [
        f"{row['finish_position']}着 {row['horse_number']} {row['horse_name']} / AI順位 {ai_order.get(row['horse_number'], '-')} / AI勝率 {_percent(row.get('ai_probability'))}"
        for row in sorted(top3_rows, key=lambda row: row["finish_position"])
    ]
    ai_top3_numbers = {prediction.horse_number for prediction in predictions[:3]}
    actual_top3_numbers = {row["horse_number"] for row in top3_rows}
    hit_count = len(ai_top3_numbers & actual_top3_numbers)
    lines = [
        "",
        "## 結果レビュー",
        f"AI Brier {review.get('ai_brier_score')} / AI LogLoss {review.get('ai_log_loss')}",
        f"勝ち馬: {' / '.join(winner_lines) if winner_lines else '-'}",
        f"3着内: {' / '.join(top3_lines) if top3_lines else '-'}",
        f"AI上位3頭の3着内ヒット: {hit_count}/3",
    ]
    misses = (review.get("insights") or {}).get("biggest_ai_misses") or []
    if misses:
        lines.append("AIの大きな外し:")
        for row in misses:
            lines.append(
                f"- {row['horse_number']} {row['horse_name']} / 結果 {_result_label(row)} / AI {_percent(row.get('ai_probability'))} / score {row.get('score')}"
            )
    return "\n".join(lines)


def format_review_unavailable(message: str) -> str:
    return "\n".join(["", "## 結果レビュー", f"結果レビューは実行できませんでした: {message}"])


def format_saved_races(rows: list[dict]) -> str:
    lines = ["# 保存済み予想"]
    if not rows:
        lines.append("保存済み予想はありません。")
        return "\n".join(lines)
    for row in rows:
        lines.append(
            " / ".join(
                [
                    row["race_id"],
                    f"{row.get('race_date') or '-'} {row.get('racecourse') or '-'} {row.get('race_number') or '-'}R",
                    row.get("race_name") or "",
                    f"snapshots {row.get('snapshot_count', 0)}",
                    f"結果 {'あり' if row.get('has_result') else 'なし'}",
                    f"レビュー {row.get('review_count', 0)}件",
                ]
            ).strip()
        )
    return "\n".join(lines)


def format_batch_review(rows: list[dict]) -> str:
    lines = ["# 未レビュー一括処理"]
    if not rows:
        lines.append("対象レースはありません。")
        return "\n".join(lines)
    for row in rows:
        status = row.get("status")
        if status == "reviewed":
            lines.append(
                f"{row['race_id']} / レビュー保存 / AI Brier {row.get('ai_brier_score')} / 3着内ヒット {row.get('top3_hit_count')}/3"
            )
        else:
            lines.append(f"{row['race_id']} / skip / {row.get('message')}")
    return "\n".join(lines)


def format_training_dataset_summary(dataset: dict) -> str:
    return "\n".join(
        [
            "# 学習データセット",
            f"rows {dataset.get('row_count', 0)} / races {dataset.get('race_count', 0)} / features {len(dataset.get('feature_columns') or [])}",
            f"labels {', '.join(dataset.get('label_columns') or [])}",
            dataset.get("note", ""),
        ]
    )


def format_baseline_training_result(result: dict) -> str:
    lines = [
        "# Challenger学習",
        f"status {result.get('status')} / model {result.get('model_type')} / label {result.get('label')}",
        f"rows {result.get('row_count', 0)} / races {result.get('race_count', 0)}",
    ]
    if result.get("status") != "trained_in_sample":
        lines.append(f"reason {result.get('reason', '-')}")
        if result.get("required_rows"):
            lines.append(f"required rows {result['required_rows']}")
        if result.get("required_races"):
            lines.append(f"required races {result['required_races']}")
        return "\n".join(lines)
    lines.extend(
        [
            f"positive {result.get('positive_count', 0)} / negative {result.get('negative_count', 0)} / features {result.get('feature_count', 0)}",
            f"Brier {result.get('brier_score')} / LogLoss {result.get('log_loss')}",
            _format_race_holdout(result.get("race_holdout") or {}),
            _format_temporal_evaluation(result.get("temporal_evaluation") or {}),
            "Top positive coefficients:",
        ]
    )
    lines.extend(f"- {row['feature']}: {row['coefficient']}" for row in result.get("top_positive_coefficients") or [])
    lines.append("Top negative coefficients:")
    lines.extend(f"- {row['feature']}: {row['coefficient']}" for row in result.get("top_negative_coefficients") or [])
    lines.append(result.get("note", ""))
    return "\n".join(lines)


def _format_race_holdout(holdout: dict) -> str:
    if holdout.get("status") == "evaluated":
        return (
            f"Race holdout rule Brier {holdout.get('rule_brier_score')} / "
            f"blended Brier {holdout.get('blended_brier_score')} / "
            f"blend {'approved' if holdout.get('blend_approved') else 'rejected'} / "
            f"folds {holdout.get('fold_count', 0)} / "
            f"top1 {holdout.get('rule_top1_accuracy', '-')} -> {holdout.get('blended_top1_accuracy', '-')} / "
            f"winner-in-top3 {holdout.get('rule_top3_hit_rate', '-')} -> {holdout.get('blended_top3_hit_rate', '-')}"
        )
    return f"Race holdout unavailable: {holdout.get('reason', '-')}"


def _format_temporal_evaluation(evaluation: dict) -> str:
    if evaluation.get("status") != "evaluated":
        return f"Temporal test unavailable: {evaluation.get('reason', '-')}"
    return (
        f"Temporal test races {evaluation.get('race_count')} / horses {evaluation.get('horse_count')} / "
        f"Brier baseline {evaluation.get('baseline_brier_score')} vs logistic {evaluation.get('brier_score')} / "
        f"LogLoss baseline {evaluation.get('baseline_log_loss')} vs logistic {evaluation.get('log_loss')} / "
        f"Top3 recall baseline {evaluation.get('baseline_top3_recall')} vs logistic {evaluation.get('top3_recall')} / "
        f"JEV rows {evaluation.get('jev_available_rows', 0)}"
    )


def write_training_dataset_csv(dataset: dict, path: str) -> None:
    metadata_columns = [
        "race_id",
        "race_date",
        "racecourse",
        "race_number",
        "horse_id",
        "horse_number",
        "horse_name",
        "snapshot_id",
        "prediction_timestamp",
        "review_created_at",
        "model_version",
        "feature_version",
        "ai_win_probability",
        "ai_top2_probability",
        "ai_top3_probability",
        "base_score",
        "confidence",
        "finish_position",
        "actual_win",
        "actual_top3",
        "result_status",
    ]
    feature_columns = dataset.get("feature_columns") or []
    columns = metadata_columns + [f"feature__{column}" for column in feature_columns]
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in dataset.get("rows") or []:
            flat = {column: row.get(column) for column in metadata_columns}
            features = row.get("features") or {}
            flat.update({f"feature__{column}": features.get(column) for column in feature_columns})
            writer.writerow(flat)


def _percent(value: object) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _strength_stars(win_probability: float, top_win_probability: float) -> str:
    ratio = float(win_probability) / top_win_probability if top_win_probability > 0 else 0.0
    score = max(1.0, min(5.0, round((1.0 + ratio * 4.0) * 2.0) / 2.0))
    full = int(score)
    has_half = score % 1.0 != 0.0
    empty = 5 - full - (1 if has_half else 0)
    return f"{'★' * full}{'½' if has_half else ''}{'☆' * empty} {score:.1f}"


def _market_label(entry) -> str:
    if not entry:
        return "-"
    odds = entry.win_odds.value
    popularity = entry.popularity.value
    parts = []
    if odds is not None:
        parts.append(f"単勝 {float(odds):.1f}")
    if popularity is not None:
        parts.append(f"{int(popularity)}番人気")
    return " / ".join(parts) if parts else "-"


def _result_label(row: dict) -> str:
    if row.get("finish_position"):
        return f"{row['finish_position']}着"
    return row.get("result_status") or "未勝利"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a race prediction and print a report.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Race date, e.g. 2026-08-16")
    parser.add_argument("--course", default="札幌", help="Racecourse, e.g. 札幌")
    parser.add_argument("--race", type=int, default=11, help="Race number")
    parser.add_argument("--mode", choices=("demo", "jra"), default="jra", help="Data provider mode")
    parser.add_argument("--iterations", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260816)
    parser.add_argument("--no-save", action="store_true", help="Do not save analysis snapshots")
    parser.add_argument("--review-result", action="store_true", help="Fetch race result and append review when available")
    parser.add_argument("--review-saved-race-id", help="Review latest saved AI prediction snapshots for this race_id without fetching race card")
    parser.add_argument("--list-saved", action="store_true", help="List saved prediction races")
    parser.add_argument("--review-due", action="store_true", help="Try result review for saved races without reviews")
    parser.add_argument("--needs-review-only", action="store_true", help="With --list-saved, show only races without reviews")
    parser.add_argument("--training-dataset", action="store_true", help="Print training dataset summary from saved snapshots and reviews")
    parser.add_argument("--training-dataset-csv", help="Write training dataset CSV to this path")
    parser.add_argument("--train-baseline", action="store_true", help="Train logistic-regression baseline from saved training dataset")
    parser.add_argument("--train-label", choices=("actual_win", "actual_top3"), default="actual_win", help="Label for --train-baseline")
    parser.add_argument("--min-train-rows", type=int, default=20, help="Minimum rows required for --train-baseline")
    parser.add_argument("--min-train-races", type=int, default=4, help="Minimum races required for --train-baseline")
    parser.add_argument("--limit", type=int, default=20, help="List limit for --list-saved")
    parser.add_argument("--require-review-result", action="store_true", help="Exit with an error when --review-result cannot fetch results")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of text report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.train_baseline:
        return train_baseline(args)
    if args.training_dataset or args.training_dataset_csv:
        return training_dataset(args)
    if args.list_saved:
        return list_saved(args)
    provider = provider_for_mode(args.mode)
    if args.review_due:
        return review_due(args, provider)
    if args.review_saved_race_id:
        return review_saved(args, provider)
    service = RaceAnalysisService(provider=provider, database_url=os.getenv("DATABASE_URL"))
    try:
        result = service.analyze(
            AnalysisRequest(
                race_date=args.date,
                racecourse=args.course,
                race_number=args.race,
                iterations=args.iterations,
                seed=args.seed,
                save=not args.no_save,
                enforce_pre_race=args.mode == "jra",
            )
        )
    except (ProviderUnavailableError, ValueError) as exc:
        print(f"分析できませんでした: {exc}", file=sys.stderr)
        return 2
    review = None
    review_error = None
    if args.review_result:
        race_result = None
        try:
            race_result = provider.get_race_results(result.race.race_id)
        except (ProviderUnavailableError, ValueError) as exc:
            review_error = str(exc)
            if args.require_review_result:
                print(f"結果レビューは実行できませんでした: {exc}", file=sys.stderr)
                return 3
        if not race_result and review_error is None:
            review_error = "結果を取得できませんでした。"
            if args.require_review_result:
                print(f"結果レビューは実行できませんでした: {review_error}", file=sys.stderr)
                return 3
        if review_error is None:
            review = PredictionReviewer().review(result.predictions, race_result)

    if args.json:
        payload = result.model_dump(mode="json")
        if review is not None:
            payload["review"] = review
        if review_error is not None:
            payload["review_error"] = review_error
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        report = format_prediction_report(result)
        if review is not None:
            report += "\n" + format_review_report(result, review)
        if review_error is not None:
            report += "\n" + format_review_unavailable(review_error)
        print(report)
    return 0


def training_dataset(args: argparse.Namespace) -> int:
    with make_session_factory(os.getenv("DATABASE_URL"))() as session:
        dataset = RaceRepository(session).training_dataset()
    if args.training_dataset_csv:
        write_training_dataset_csv(dataset, args.training_dataset_csv)
    if args.json:
        print(json.dumps(dataset, ensure_ascii=False, indent=2))
    else:
        print(format_training_dataset_summary(dataset))
        if args.training_dataset_csv:
            print(f"CSV: {args.training_dataset_csv}")
    return 0


def train_baseline(args: argparse.Namespace) -> int:
    session_factory = make_session_factory(os.getenv("DATABASE_URL"))
    with session_factory() as session:
        dataset = RaceRepository(session).training_dataset()
    trainer = LogisticBaselineTrainer(
        TrainingConfig(label=args.train_label, min_rows=args.min_train_rows, min_races=args.min_train_races)
    )
    result = trainer.train(dataset)
    evaluation_with_jev = evaluate_challenger_dataset(dataset, include_jev=True)
    evaluation_without_jev = evaluate_challenger_dataset(dataset, include_jev=False)
    result["temporal_evaluation"] = evaluation_with_jev
    result["jev_ablation"] = {
        "with_jev": evaluation_with_jev,
        "without_jev": evaluation_without_jev,
    }
    feature_versions = sorted(
        {
            str(row["feature_version"])
            for row in dataset.get("rows", [])
            if row.get("feature_version") not in (None, "")
        }
    )
    feature_version = feature_versions[0] if len(feature_versions) == 1 else "mixed" if feature_versions else None
    with session_factory() as session:
        repository = RaceRepository(session)
        run = repository.save_training_run(
            LOGISTIC_BASELINE_VERSION,
            feature_version,
            dataset,
            result,
        )
        repository.save_model_evaluation(
            LOGISTIC_BASELINE_VERSION,
            feature_version,
            evaluation_with_jev,
            run.id,
        )
        repository.save_model_evaluation(
            LOGISTIC_BASELINE_VERSION,
            feature_version,
            evaluation_without_jev,
            run.id,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_baseline_training_result(result))
    return 0


def list_saved(args: argparse.Namespace) -> int:
    with make_session_factory(os.getenv("DATABASE_URL"))() as session:
        rows = RaceRepository(session).race_history(
            limit=args.limit,
            has_review=False if args.needs_review_only else None,
        )
    print(format_saved_races(rows))
    return 0


def review_due(args: argparse.Namespace, provider) -> int:
    results = AutomaticReviewService(provider, os.getenv("DATABASE_URL")).run_once(
        limit=args.limit,
        save=not args.no_save,
    )

    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
    else:
        print(format_batch_review(results))
    return 0


def review_saved(args: argparse.Namespace, provider) -> int:
    race_id = args.review_saved_race_id
    with make_session_factory(os.getenv("DATABASE_URL"))() as session:
        repo = RaceRepository(session)
        predictions = repo.latest_snapshot_predictions(race_id)
    if not predictions:
        print(f"保存済みAI予測が見つかりません: {race_id}", file=sys.stderr)
        return 4
    try:
        race_result = provider.get_race_results(race_id)
    except (ProviderUnavailableError, ValueError) as exc:
        print(f"結果レビューは実行できませんでした: {exc}", file=sys.stderr)
        return 3
    if not race_result:
        print("結果レビューは実行できませんでした: 結果を取得できませんでした。", file=sys.stderr)
        return 3
    review = PredictionReviewer().review(predictions, race_result)
    if args.json:
        print(
            json.dumps(
                {
                    "race_id": race_id,
                    "prediction_source": "saved_snapshot",
                    "review": review,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        lines = [
            f"# 保存済みAI予測レビュー {race_id}",
            f"保存済みAI予測: {len(predictions)}頭",
            format_review_report_for_predictions(predictions, review),
        ]
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
