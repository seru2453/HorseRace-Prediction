from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repository import RaceRepository
from database.session import get_session_factory
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.common.models import RaceResultData, RaceResultEntryData, UserPredictionInput
from services.common.deployment import deployment_mode, is_public_deployment
from services.common.schedule import schedule_response, validate_schedule_date
from services.data_provider.base import ProviderUnavailableError
from services.data_provider.jra_official import JraOfficialProvider
from services.data_provider.mock import MockProvider
from services.learning.review import PredictionReviewer
from services.learning.daily_pipeline import start_daily_pipeline_worker
from services.parser.race_parser import RaceParser


def create_app() -> Flask:
    static_dir = Path(__file__).resolve().parents[1] / "web" / "static"
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="")
    session_factory = get_session_factory(os.getenv("DATABASE_URL"))

    def provider_for_mode(mode: str):
        return JraOfficialProvider() if mode == "jra" else MockProvider()

    def saved_analysis_for(payload: dict) -> dict | None:
        with session_factory() as session:
            repo = RaceRepository(session)
            race_id = repo.predicted_race_id(
                str(payload.get("race_date") or ""),
                str(payload.get("racecourse") or ""),
                int(payload.get("race_number") or 0),
            )
            return repo.race_detail(race_id) if race_id else None

    def saved_analysis_payload(detail: dict) -> dict:
        analysis_run = detail.get("latest_analysis_run") or {}
        analysis_payload = analysis_run.get("payload") or {}
        return {
            "race": detail["race"],
            "entries": detail.get("entries") or [],
            "predictions": detail.get("latest_predictions") or [],
            "simulation": detail.get("latest_simulation"),
            "parse_quality": analysis_payload.get("parse_quality"),
            "provider_name": analysis_run.get("provider_name"),
            "latest_user_predictions": detail.get("latest_user_predictions") or {},
            "result": detail.get("result"),
            "result_source": detail.get("result_source"),
            "result_only": bool(detail.get("result")) and not bool(detail.get("latest_predictions")),
            "message": "発走前に保存した分析を表示しています。",
            "restored_from_saved": True,
        }

    @app.get("/")
    def index():
        return send_from_directory(static_dir, "index.html")

    @app.get("/api/config")
    def public_config():
        return jsonify({"deployment_mode": deployment_mode()})

    @app.get("/api/schedule")
    def race_schedule():
        if is_public_deployment():
            return local_only_response()
        try:
            race_date = validate_schedule_date(request.args.get("date"))
            items = JraOfficialProvider().list_races(race_date)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        except ProviderUnavailableError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 503
        return jsonify(schedule_response(race_date, items))

    def local_only_response():
        return jsonify({"ok": False, "message": "この機能はローカルモードでのみ利用できます。"}), 404

    def not_prepared_response():
        return jsonify(
            {
                "ok": False,
                "message": "このレースの保存済み分析はありません。管理された取得処理の完了後に利用できます。",
                "prepared": False,
            }
        ), 503

    def public_snapshot(snapshot: dict) -> dict:
        return {key: value for key, value in snapshot.items() if key != "raw_features"}

    @app.post("/api/analyze")
    def analyze():
        payload = request.get_json(force=True)
        mode = payload.get("mode", "jra")
        if is_public_deployment():
            saved_detail = saved_analysis_for(payload)
            if saved_detail and saved_detail.get("latest_analysis_run") and saved_detail.get("latest_predictions"):
                return jsonify(saved_analysis_payload(saved_detail))
            return not_prepared_response()
        saved_detail = saved_analysis_for(payload) if mode == "jra" else None
        if saved_detail and saved_detail.get("result"):
            return jsonify(saved_analysis_payload(saved_detail))
        service = RaceAnalysisService(provider=provider_for_mode(mode), database_url=os.getenv("DATABASE_URL"))
        try:
            result = service.analyze(
                AnalysisRequest(
                    race_date=payload["race_date"],
                    racecourse=payload["racecourse"],
                    race_number=int(payload["race_number"]),
                    iterations=int(payload.get("iterations", 10000)),
                    seed=int(payload.get("seed", 20260816)),
                    enforce_pre_race=mode == "jra",
                )
            )
        except (ProviderUnavailableError, ValueError) as exc:
            if saved_detail:
                return jsonify(saved_analysis_payload(saved_detail))
            if mode == "jra" and str(exc) == "発走後のレースは予測を作成・保存できません。":
                provider = service.provider
                try:
                    race = provider.get_race(payload["race_date"], payload["racecourse"], int(payload["race_number"]))
                    entries = provider.get_race_entries(race.race_id)
                    result_data = provider.get_race_results(race.race_id)
                    if result_data is not None:
                        with session_factory() as session:
                            RaceRepository(session).save_result(result_data)
                    return jsonify({
                        "race": race.model_dump(mode="json"),
                        "entries": [entry.model_dump(mode="json") for entry in entries],
                        "predictions": [],
                        "simulation": None,
                        "parse_quality": None,
                        "provider_name": type(provider).__name__,
                        "latest_user_predictions": {},
                        "result": result_data.model_dump(mode="json") if result_data is not None else None,
                        "result_source": "provider" if result_data is not None else None,
                        "result_only": True,
                        "message": "発走済みのため予測は作成せず、公式結果を表示しています。" if result_data is not None else "発走済みのため予測は作成していません。公式結果はまだ公開されていません。",
                    })
                except (ProviderUnavailableError, ValueError) as result_exc:
                    return jsonify({"ok": False, "message": str(result_exc), "mode": mode}), 400
            return jsonify({"ok": False, "message": str(exc), "mode": mode}), 400
        response_payload = result.model_dump(mode="json")
        with session_factory() as session:
            latest_user_predictions = RaceRepository(session).latest_user_predictions(result.race.race_id)
        response_payload["latest_user_predictions"] = {
            horse_number: prediction.model_dump(mode="json")
            for horse_number, prediction in latest_user_predictions.items()
        }
        return jsonify(response_payload)

    @app.post("/api/provider-diagnostics")
    def provider_diagnostics():
        if is_public_deployment():
            return local_only_response()
        payload = request.get_json(force=True)
        mode = payload.get("mode", "jra")
        provider = provider_for_mode(mode)
        try:
            race = provider.get_race(payload["race_date"], payload["racecourse"], int(payload["race_number"]))
            entries = provider.get_race_entries(race.race_id)
            parse_quality = RaceParser().validate(race, entries)
        except (ProviderUnavailableError, ValueError) as exc:
            return jsonify({"ok": False, "message": str(exc), "mode": mode}), 400
        return jsonify(
            {
                "ok": True,
                "mode": mode,
                "race_id": race.race_id,
                "entry_count": len(entries),
                "parse_quality": parse_quality.model_dump(mode="json"),
            }
        )

    @app.post("/api/user-predictions/<race_id>")
    def save_user_predictions(race_id: str):
        payload = request.get_json(force=True)
        predictions = {int(k): UserPredictionInput.model_validate(v) for k, v in payload.get("predictions", {}).items()}
        with session_factory() as session:
            saved = RaceRepository(session).save_user_predictions(race_id, predictions)
        return jsonify({"ok": True, "saved": saved})

    @app.post("/api/results/<race_id>")
    def update_results(race_id: str):
        payload = request.get_json(silent=True) or {}
        with session_factory() as session:
            saved_result, saved_source = RaceRepository(session).saved_result(race_id)
        if is_public_deployment():
            if saved_result is None:
                return not_prepared_response()
            return jsonify(
                {
                    "ok": True,
                    "result": saved_result.model_dump(mode="json"),
                    "result_source": f"saved_{saved_source}" if saved_source else "saved",
                }
            )
        if saved_result is not None and not payload.get("force_refresh"):
            return jsonify(
                {
                    "ok": True,
                    "result": saved_result.model_dump(mode="json"),
                    "result_source": f"saved_{saved_source}" if saved_source else "saved",
                }
            )
        provider = provider_for_mode(payload.get("mode", "jra"))
        try:
            result = provider.get_race_results(race_id)
        except (ProviderUnavailableError, ValueError) as exc:
            if saved_result is not None:
                return jsonify(
                    {
                        "ok": True,
                        "result": saved_result.model_dump(mode="json"),
                        "result_source": f"saved_{saved_source}" if saved_source else "saved",
                    }
                )
            message = str(exc)
            if "result selection did not list the requested date/course" in message:
                message = "JRA公式の結果一覧に対象日・競馬場がまだ掲載されていません。"
            return jsonify({"ok": False, "message": message, "race_id": race_id}), 404
        if not result:
            return jsonify({"ok": False, "message": "結果を取得できませんでした。"}), 404
        with session_factory() as session:
            RaceRepository(session).save_result(result)
        return jsonify({"ok": True, "result": result.model_dump(mode="json"), "result_source": "provider"})

    @app.get("/api/results/<race_id>")
    def saved_results(race_id: str):
        with session_factory() as session:
            result, source = RaceRepository(session).saved_result(race_id)
        if result is None:
            return jsonify({"ok": False, "message": "保存済み結果はありません。"}), 404
        return jsonify({"ok": True, "result": result.model_dump(mode="json"), "result_source": source})

    @app.post("/api/result-diagnostics/<race_id>")
    def result_diagnostics(race_id: str):
        if is_public_deployment():
            return local_only_response()
        payload = request.get_json(silent=True) or {}
        provider = provider_for_mode(payload.get("mode", "jra"))
        try:
            result = provider.get_race_results(race_id)
        except (ProviderUnavailableError, ValueError) as exc:
            return jsonify({"ok": False, "message": str(exc), "race_id": race_id}), 400
        if not result:
            return jsonify({"ok": False, "message": "結果を取得できませんでした。", "race_id": race_id}), 400
        return jsonify(
            {
                "ok": True,
                "race_id": race_id,
                "entry_count": len(result.entries),
                "winner_count": len([entry for entry in result.entries if entry.finish_position == 1]),
                "result_timestamp": result.result_timestamp,
            }
        )

    @app.post("/api/results/<race_id>/manual")
    def save_manual_result(race_id: str):
        payload = request.get_json(force=True)
        try:
            result = _manual_result(race_id, payload)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        with session_factory() as session:
            RaceRepository(session).save_result(result)
        return jsonify({"ok": True, "result": result.model_dump(mode="json")})

    @app.post("/api/review/<race_id>")
    def review(race_id: str):
        payload = request.get_json(force=True)
        if is_public_deployment() and not (payload.get("result") or payload.get("use_saved_result")):
            return not_prepared_response()
        provider = provider_for_mode(payload.get("mode", "jra"))
        result_source = "provider"
        if payload.get("result"):
            try:
                result = _manual_result(race_id, payload["result"])
            except ValueError as exc:
                return jsonify({"ok": False, "message": str(exc)}), 400
            result_source = "manual"
        elif payload.get("use_saved_result"):
            with session_factory() as session:
                result, saved_source = RaceRepository(session).saved_result(race_id)
            result_source = f"saved_{saved_source}" if saved_source else "saved"
        else:
            try:
                result = provider.get_race_results(race_id)
            except (ProviderUnavailableError, ValueError) as exc:
                return jsonify({"ok": False, "message": str(exc), "race_id": race_id}), 404
        if not result:
            return jsonify({"ok": False, "message": "結果を取得できませんでした。"}), 404
        request_user_predictions = _meaningful_user_predictions(
            {int(k): UserPredictionInput.model_validate(v) for k, v in payload.get("predictions", {}).items()}
        )
        with session_factory() as session:
            repo = RaceRepository(session)
            repo.save_result(result)
            predictions = repo.latest_snapshot_predictions(race_id)
            prediction_source = "snapshot"
            if not predictions:
                return jsonify({"ok": False, "message": "発走前に保存されたAI予測がありません。"}), 400
            saved_user_predictions = repo.latest_user_predictions(race_id)
            user_predictions = {**saved_user_predictions, **request_user_predictions}
            review_result = PredictionReviewer().review(predictions, result, user_predictions)
            review_result["prediction_source"] = prediction_source
            review_result["user_prediction_source"] = _user_prediction_source(
                bool(saved_user_predictions),
                bool(request_user_predictions),
            )
            review_result["result_source"] = result_source
            repo.save_review(race_id, review_result)
        return jsonify(review_result)

    @app.get("/api/learning")
    def learning():
        with session_factory() as session:
            return jsonify(RaceRepository(session).learning_summary())

    @app.get("/api/prediction-snapshots/<race_id>")
    def prediction_snapshots(race_id: str):
        limit = int(request.args.get("limit", 50))
        if is_public_deployment():
            limit = max(1, min(limit, 20))
        with session_factory() as session:
            snapshots = RaceRepository(session).prediction_snapshots(race_id, limit)
        if is_public_deployment():
            snapshots = [public_snapshot(row) for row in snapshots]
        return jsonify({"race_id": race_id, "snapshots": snapshots})

    @app.get("/api/races")
    def races():
        limit = int(request.args.get("limit", 20))
        racecourse = request.args.get("racecourse") or None
        has_result = _bool_query(request.args.get("has_result"))
        has_review = _bool_query(request.args.get("has_review"))
        if is_public_deployment():
            limit = max(1, min(limit, 50))
        with session_factory() as session:
            return jsonify({"races": RaceRepository(session).race_history(limit, racecourse, has_result, has_review)})

    @app.get("/api/races/<race_id>")
    def race_detail(race_id: str):
        with session_factory() as session:
            detail = RaceRepository(session).race_detail(race_id)
        if detail is None:
            return jsonify({"ok": False, "message": "保存済みレースが見つかりません。"}), 404
        if is_public_deployment():
            detail["latest_snapshots"] = [public_snapshot(row) for row in detail.get("latest_snapshots") or []]
        return jsonify(detail)

    @app.get("/api/export")
    def export_data():
        if is_public_deployment():
            return local_only_response()

        def stream_export():
            with session_factory() as session:
                yield from RaceRepository(session).iter_export_json()

        return Response(stream_with_context(stream_export()), mimetype="application/json")

    @app.get("/api/training-dataset")
    def training_dataset():
        if is_public_deployment():
            return local_only_response()

        def stream_dataset():
            with session_factory() as session:
                yield from RaceRepository(session).iter_training_dataset_json()

        return Response(stream_with_context(stream_dataset()), mimetype="application/json")

    @app.post("/api/import")
    def import_data():
        if is_public_deployment():
            return local_only_response()
        payload = request.get_json(force=True)
        try:
            with session_factory() as session:
                result = RaceRepository(session).import_data(payload)
        except ValueError as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        return jsonify({"ok": True, "result": result})

    return app


def _meaningful_user_predictions(predictions: dict[int, UserPredictionInput]) -> dict[int, UserPredictionInput]:
    return {
        horse_number: prediction
        for horse_number, prediction in predictions.items()
        if any(value not in (None, "") for value in prediction.model_dump(mode="json").values())
    }


def _user_prediction_source(has_saved: bool, has_request: bool) -> str:
    if has_saved and has_request:
        return "request+snapshot"
    if has_request:
        return "request"
    if has_saved:
        return "snapshot"
    return "none"


def _bool_query(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.lower() in {"1", "true", "yes", "on"}


def _manual_result(race_id: str, payload: dict) -> RaceResultData:
    entries = payload.get("entries") or []
    if not entries:
        raise ValueError("手入力結果にはentriesが必要です。")
    finish_positions = [
        int(item["finish_position"])
        for item in entries
        if item.get("finish_position") not in (None, "")
    ]
    if any(position < 1 or position > len(entries) for position in finish_positions):
        raise ValueError("着順は1以上、出走頭数以内で入力してください。")
    if len(finish_positions) != len(set(finish_positions)):
        raise ValueError("同じ着順が複数入力されています。")
    result_entries = [
        RaceResultEntryData(
            horse_number=int(item["horse_number"]),
            horse_name=str(item.get("horse_name") or ""),
            finish_position=int(item["finish_position"]) if item.get("finish_position") not in (None, "") else None,
            result_status=item.get("result_status"),
        )
        for item in entries
    ]
    if not any(item.finish_position == 1 for item in result_entries):
        raise ValueError("1着馬を入力してください。")
    return RaceResultData(
        race_id=race_id,
        result_timestamp=payload.get("result_timestamp") or datetime.now(timezone.utc).isoformat(),
        entries=result_entries,
    )


app = create_app()


if __name__ == "__main__":
    if os.getenv("AUTO_PIPELINE_ENABLED", os.getenv("AUTO_REVIEW_ENABLED", "1")) != "0":
        start_daily_pipeline_worker(JraOfficialProvider(), os.getenv("DATABASE_URL"))
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
        use_reloader=False,
    )
