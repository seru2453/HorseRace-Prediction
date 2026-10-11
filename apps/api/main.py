from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without optional FastAPI deps
    raise RuntimeError("FastAPI is not installed. Use apps/api/flask_app.py locally or install project dependencies.") from exc

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
from services.parser.race_parser import RaceParser


class AnalyzePayload(BaseModel):
    race_date: str
    racecourse: str
    race_number: int
    iterations: int = 10_000
    seed: int = 20260816
    mode: str = "jra"


class UserPredictionPayload(BaseModel):
    predictions: dict[int, UserPredictionInput]


class ResultPayload(BaseModel):
    race_date: str | None = None
    racecourse: str | None = None
    race_number: int | None = None
    iterations: int = 10_000
    seed: int = 20260816
    mode: str = "jra"
    predictions: dict[int, UserPredictionInput] = {}
    result: dict | None = None
    use_saved_result: bool = False


app = FastAPI(title="HorsePrediction API")


def provider_for_mode(mode: str):
    return JraOfficialProvider() if mode == "jra" else MockProvider()


def _local_only_response():
    return JSONResponse(
        status_code=404,
        content={"ok": False, "message": "この機能はローカルモードでのみ利用できます。"},
    )


def _not_prepared_response():
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "message": "このレースの保存済み分析はありません。管理された取得処理の完了後に利用できます。",
            "prepared": False,
        },
    )


def _saved_analysis_for(race_date: str, racecourse: str, race_number: int) -> dict | None:
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        repo = RaceRepository(session)
        race_id = repo.predicted_race_id(race_date, racecourse, race_number)
        return repo.race_detail(race_id) if race_id else None


def _saved_analysis_payload(detail: dict) -> dict:
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
        "message": "発走前に保存した分析を表示しています。",
        "restored_from_saved": True,
    }


def _public_snapshot(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key != "raw_features"}


@app.get("/api/config")
def public_config():
    return {"deployment_mode": deployment_mode()}


@app.get("/api/schedule")
def race_schedule(date: str):
    if is_public_deployment():
        return _local_only_response()
    try:
        race_date = validate_schedule_date(date)
        items = JraOfficialProvider().list_races(race_date)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "message": str(exc)})
    except ProviderUnavailableError as exc:
        return JSONResponse(status_code=503, content={"ok": False, "message": str(exc)})
    return schedule_response(race_date, items)


@app.post("/api/analyze")
def analyze(payload: AnalyzePayload):
    if is_public_deployment():
        detail = _saved_analysis_for(payload.race_date, payload.racecourse, payload.race_number)
        if detail and detail.get("latest_analysis_run") and detail.get("latest_predictions"):
            return _saved_analysis_payload(detail)
        return _not_prepared_response()
    service = RaceAnalysisService(provider=provider_for_mode(payload.mode), database_url=os.getenv("DATABASE_URL"))
    try:
        result = service.analyze(
            AnalysisRequest(
                payload.race_date,
                payload.racecourse,
                payload.race_number,
                payload.iterations,
                payload.seed,
                enforce_pre_race=payload.mode == "jra",
            )
        )
    except (ProviderUnavailableError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response_payload = result.model_dump(mode="json")
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        latest_user_predictions = RaceRepository(session).latest_user_predictions(result.race.race_id)
    response_payload["latest_user_predictions"] = {
        horse_number: prediction.model_dump(mode="json")
        for horse_number, prediction in latest_user_predictions.items()
    }
    return response_payload


@app.post("/api/provider-diagnostics")
def provider_diagnostics(payload: AnalyzePayload):
    if is_public_deployment():
        return _local_only_response()
    provider = provider_for_mode(payload.mode)
    try:
        race = provider.get_race(payload.race_date, payload.racecourse, payload.race_number)
        entries = provider.get_race_entries(race.race_id)
        parse_quality = RaceParser().validate(race, entries)
    except (ProviderUnavailableError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "mode": payload.mode,
        "race_id": race.race_id,
        "entry_count": len(entries),
        "parse_quality": parse_quality,
    }


@app.post("/api/user-predictions/{race_id}")
def save_user_predictions(race_id: str, payload: UserPredictionPayload):
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        saved = RaceRepository(session).save_user_predictions(race_id, payload.predictions)
    return {"ok": True, "saved": saved}


@app.post("/api/results/{race_id}")
def update_results(race_id: str, payload: ResultPayload):
    if is_public_deployment():
        with get_session_factory(os.getenv("DATABASE_URL"))() as session:
            result, source = RaceRepository(session).saved_result(race_id)
        if result is None:
            return _not_prepared_response()
        return {"ok": True, "result": result, "result_source": f"saved_{source}" if source else "saved"}
    try:
        result = provider_for_mode(payload.mode).get_race_results(race_id)
    except (ProviderUnavailableError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="結果を取得できませんでした。")
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        RaceRepository(session).save_result(result)
    return {"ok": True, "result": result}


@app.post("/api/result-diagnostics/{race_id}")
def result_diagnostics(race_id: str, payload: ResultPayload):
    if is_public_deployment():
        return _local_only_response()
    try:
        result = provider_for_mode(payload.mode).get_race_results(race_id)
    except (ProviderUnavailableError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=400, detail="結果を取得できませんでした。")
    return {
        "ok": True,
        "race_id": race_id,
        "entry_count": len(result.entries),
        "winner_count": len([entry for entry in result.entries if entry.finish_position == 1]),
        "result_timestamp": result.result_timestamp,
    }


@app.post("/api/results/{race_id}/manual")
def save_manual_result(race_id: str, payload: dict):
    try:
        result = _manual_result(race_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        RaceRepository(session).save_result(result)
    return {"ok": True, "result": result}


@app.post("/api/review/{race_id}")
def review(race_id: str, payload: ResultPayload):
    if is_public_deployment() and not (payload.result or payload.use_saved_result):
        return _not_prepared_response()
    provider = provider_for_mode(payload.mode)
    result_source = "provider"
    if payload.result:
        try:
            result = _manual_result(race_id, payload.result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result_source = "manual"
    elif payload.use_saved_result:
        with get_session_factory(os.getenv("DATABASE_URL"))() as session:
            result, saved_source = RaceRepository(session).saved_result(race_id)
        result_source = f"saved_{saved_source}" if saved_source else "saved"
    else:
        try:
            result = provider.get_race_results(race_id)
        except (ProviderUnavailableError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="結果を取得できませんでした。")
    request_user_predictions = _meaningful_user_predictions(payload.predictions)
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        repo = RaceRepository(session)
        repo.save_result(result)
        predictions = repo.latest_snapshot_predictions(race_id)
        prediction_source = "snapshot"
        if not predictions:
            raise HTTPException(status_code=400, detail="発走前に保存されたAI予測がありません。")
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
    return review_result


@app.get("/api/learning")
def learning():
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        return RaceRepository(session).learning_summary()


@app.get("/api/prediction-snapshots/{race_id}")
def prediction_snapshots(race_id: str, limit: int = 50):
    if is_public_deployment():
        limit = max(1, min(limit, 20))
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        snapshots = RaceRepository(session).prediction_snapshots(race_id, limit)
    if is_public_deployment():
        snapshots = [_public_snapshot(row) for row in snapshots]
    return {"race_id": race_id, "snapshots": snapshots}


@app.get("/api/races")
def races(limit: int = 20, racecourse: str | None = None, has_result: bool | None = None, has_review: bool | None = None):
    if is_public_deployment():
        limit = max(1, min(limit, 50))
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        return {"races": RaceRepository(session).race_history(limit, racecourse, has_result, has_review)}


@app.get("/api/races/{race_id}")
def race_detail(race_id: str):
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        detail = RaceRepository(session).race_detail(race_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="保存済みレースが見つかりません。")
    if is_public_deployment():
        detail["latest_snapshots"] = [_public_snapshot(row) for row in detail.get("latest_snapshots") or []]
    return detail


@app.get("/api/export")
def export_data():
    if is_public_deployment():
        return _local_only_response()
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        return RaceRepository(session).export_data()


@app.get("/api/training-dataset")
def training_dataset():
    if is_public_deployment():
        return _local_only_response()
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        return RaceRepository(session).training_dataset()


@app.post("/api/import")
def import_data(payload: dict):
    if is_public_deployment():
        return _local_only_response()
    try:
        with get_session_factory(os.getenv("DATABASE_URL"))() as session:
            result = RaceRepository(session).import_data(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "result": result}


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


app.mount("/", StaticFiles(directory="apps/web/static", html=True), name="web")
