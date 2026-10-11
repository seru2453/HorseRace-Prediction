import pytest

from apps.api.flask_app import create_app


def test_history_and_learning_available_before_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("DEPLOYMENT_MODE", "local")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/empty.sqlite3")
    app = create_app()
    client = app.test_client()

    index_response = client.get("/")
    index_html = index_response.get_data(as_text=True)
    app_javascript = client.get("/app.js").get_data(as_text=True)
    assert index_response.status_code == 200
    assert client.get("/api/config").get_json() == {"deployment_mode": "local"}
    assert 'id="mode"' not in index_html
    assert 'class="utility-menu hidden"' in index_html
    assert index_html.count("data-tab=") == 6
    assert 'selectTab("analysis")' in app_javascript
    assert 'class="probability-bar"' in app_javascript

    races_response = client.get("/api/races")
    learning_response = client.get("/api/learning")

    assert races_response.status_code == 200
    assert races_response.get_json()["races"] == []
    assert learning_response.status_code == 200
    assert learning_response.get_json()["review_count"] == 0
    assert learning_response.get_json()["data_quality_summary"]["analysis_run_count"] == 0
    export_response = client.get("/api/export")
    assert export_response.status_code == 200
    export_payload = export_response.get_json()
    assert export_payload["format_version"] == "horseprediction-export-v1"
    assert export_payload["counts"]["races"] == 0
    dataset_response = client.get("/api/training-dataset")
    assert dataset_response.status_code == 200
    assert dataset_response.get_json()["row_count"] == 0

    import_response = client.post("/api/import", json={"format_version": "unknown", "data": {}})
    assert import_response.status_code == 400

    diagnostics_response = client.post(
        "/api/provider-diagnostics",
        json={
            "race_date": "2026-08-16",
            "racecourse": "札幌",
            "race_number": 11,
            "mode": "demo",
        },
    )
    assert diagnostics_response.status_code == 200
    diagnostics = diagnostics_response.get_json()
    assert diagnostics["ok"] is True
    assert diagnostics["entry_count"] == 8
    assert diagnostics["parse_quality"]["status"] == "ok"

    failed_diagnostics_response = client.post(
        "/api/provider-diagnostics",
        json={
            "race_date": "2026-08-16",
            "racecourse": "東京",
            "race_number": 11,
            "mode": "demo",
        },
    )
    assert failed_diagnostics_response.status_code == 400
    assert "Demo fixture" in failed_diagnostics_response.get_json()["message"]

    failed_analyze_response = client.post(
        "/api/analyze",
        json={
            "race_date": "2026-08-16",
            "racecourse": "東京",
            "race_number": 11,
            "mode": "demo",
        },
    )
    assert failed_analyze_response.status_code == 400
    assert "Demo fixture" in failed_analyze_response.get_json()["message"]


def test_public_mode_blocks_admin_and_upstream_fetch_routes(tmp_path, monkeypatch):
    from services.data_provider.jra_official import JraOfficialProvider

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/public.sqlite3")
    monkeypatch.setenv("DEPLOYMENT_MODE", "public")

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("public requests must not fetch upstream data")

    monkeypatch.setattr(JraOfficialProvider, "get_race", fail_fetch)
    monkeypatch.setattr(JraOfficialProvider, "get_race_results", fail_fetch)
    client = create_app().test_client()

    assert client.get("/api/config").get_json() == {"deployment_mode": "public"}
    index_html = client.get("/").get_data(as_text=True)
    assert "本アプリはJRA公式サービスではありません" in index_html
    assert 'id="local-admin-tools" class="utility-menu hidden"' in index_html

    for response in (
        client.get("/api/export"),
        client.get("/api/training-dataset"),
        client.post("/api/import", json={"format_version": "horseprediction-export-v1", "data": {}}),
        client.post("/api/provider-diagnostics", json={"mode": "jra"}),
        client.post("/api/result-diagnostics/unknown", json={"mode": "jra"}),
    ):
        assert response.status_code == 404
        assert response.get_json()["ok"] is False

    request = {"race_date": "2026-08-16", "racecourse": "札幌", "race_number": 11, "mode": "jra"}
    assert client.post("/api/analyze", json=request).status_code == 503
    assert client.post("/api/results/not-saved", json={"mode": "jra"}).status_code == 503
    assert client.post("/api/review/not-saved", json={"mode": "jra"}).status_code == 503


def test_public_mode_returns_saved_analysis_without_fetching(tmp_path, monkeypatch):
    from services.data_provider.jra_official import JraOfficialProvider

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/public-saved.sqlite3")
    monkeypatch.setenv("DEPLOYMENT_MODE", "local")
    client = create_app().test_client()
    response = client.post(
        "/api/analyze",
        json={"race_date": "2026-08-16", "racecourse": "札幌", "race_number": 11, "mode": "demo", "iterations": 1000},
    )
    assert response.status_code == 200

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("saved analysis should be returned without fetching")

    monkeypatch.setattr(JraOfficialProvider, "get_race", fail_fetch)
    monkeypatch.setenv("DEPLOYMENT_MODE", "public")
    saved = client.post(
        "/api/analyze",
        json={"race_date": "2026-08-16", "racecourse": "札幌", "race_number": 11, "mode": "jra"},
    )

    assert saved.status_code == 200
    assert saved.get_json()["restored_from_saved"] is True
    race_id = saved.get_json()["race"]["race_id"]
    snapshots = client.get(f"/api/prediction-snapshots/{race_id}?limit=100").get_json()["snapshots"]
    assert 0 < len(snapshots) <= 20
    assert all("raw_features" not in row for row in snapshots)
    detail = client.get(f"/api/races/{race_id}").get_json()
    assert all("raw_features" not in row for row in detail["latest_snapshots"])


def test_fastapi_public_mode_uses_the_same_public_boundary(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from apps.api.main import app as fastapi_app
    from services.data_provider.jra_official import JraOfficialProvider

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/fastapi-public.sqlite3")
    monkeypatch.setenv("DEPLOYMENT_MODE", "public")

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("public requests must not fetch upstream data")

    monkeypatch.setattr(JraOfficialProvider, "get_race", fail_fetch)
    monkeypatch.setattr(JraOfficialProvider, "get_race_results", fail_fetch)
    client = TestClient(fastapi_app)

    assert client.get("/api/config").json() == {"deployment_mode": "public"}
    for response in (
        client.get("/api/export"),
        client.get("/api/training-dataset"),
        client.post("/api/import", json={"format_version": "horseprediction-export-v1", "data": {}}),
        client.post("/api/provider-diagnostics", json={"race_date": "2026-08-16", "racecourse": "札幌", "race_number": 11}),
        client.post("/api/result-diagnostics/not-saved", json={}),
    ):
        assert response.status_code == 404
        assert response.json()["ok"] is False

    analyze_payload = {"race_date": "2026-08-16", "racecourse": "札幌", "race_number": 11, "mode": "jra"}
    assert client.post("/api/analyze", json=analyze_payload).status_code == 503
    assert client.post("/api/results/not-saved", json={}).status_code == 503
    assert client.post("/api/review/not-saved", json={}).status_code == 503

    monkeypatch.setenv("DEPLOYMENT_MODE", "local")
    saved_demo = client.post("/api/analyze", json={**analyze_payload, "mode": "demo", "iterations": 1000})
    assert saved_demo.status_code == 200
    monkeypatch.setenv("DEPLOYMENT_MODE", "public")
    saved_public = client.post("/api/analyze", json=analyze_payload)
    assert saved_public.status_code == 200
    assert saved_public.json()["restored_from_saved"] is True
    race_id = saved_public.json()["race"]["race_id"]
    snapshots = client.get(f"/api/prediction-snapshots/{race_id}?limit=100").json()["snapshots"]
    assert 0 < len(snapshots) <= 20
    assert all("raw_features" not in row for row in snapshots)


def test_review_does_not_recompute_prediction_after_result(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/no-snapshot-review.sqlite3")
    client = create_app().test_client()

    response = client.post(
        "/api/review/20260816-sapporo-11",
        json={"mode": "demo", "predictions": {}},
    )

    assert response.status_code == 400
    assert "発走前に保存されたAI予測" in response.get_json()["message"]


def test_prediction_snapshots_endpoint_returns_latest_saved_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/api.sqlite3")
    app = create_app()
    client = app.test_client()

    analyze_response = client.post(
        "/api/analyze",
        json={
            "race_date": "2026-08-16",
            "racecourse": "札幌",
            "race_number": 11,
            "iterations": 1000,
            "seed": 321,
            "mode": "demo",
        },
    )
    assert analyze_response.status_code == 200
    analysis = analyze_response.get_json()

    snapshot_response = client.get(f"/api/prediction-snapshots/{analysis['race']['race_id']}?limit=8")
    assert snapshot_response.status_code == 200
    payload = snapshot_response.get_json()

    assert payload["race_id"] == analysis["race"]["race_id"]
    assert len(payload["snapshots"]) == len(analysis["predictions"])
    assert payload["snapshots"][0]["simulation_seed"] == 321
    assert payload["snapshots"][0]["raw_features"]["surface"] == "芝"

    save_user_response = client.post(
        f"/api/user-predictions/{analysis['race']['race_id']}",
        json={
            "predictions": {
                "5": {
                    "mark": "◎",
                },
                "1": {
                    "mark": None,
                },
            }
        },
    )
    assert save_user_response.status_code == 200
    assert save_user_response.get_json()["saved"] == 1

    review_response = client.post(
        f"/api/review/{analysis['race']['race_id']}",
        json={
            "race_date": "2026-08-16",
            "racecourse": "札幌",
            "race_number": 11,
            "iterations": 1000,
            "seed": 999,
            "mode": "demo",
            "predictions": {},
        },
    )
    assert review_response.status_code == 200
    review = review_response.get_json()

    assert review["prediction_source"] == "snapshot"
    assert review["user_prediction_source"] == "snapshot"
    assert review["user_brier_score"] is not None
    assert review["user_probability_summary"]["filled_count"] == 1
    assert review["user_probability_summary"]["horse_count"] == len(analysis["predictions"])
    assert review["user_probability_summary"]["probability_total"] == 0.3
    assert review["comparison"]["winner"] in {"ai", "user", "tie"}
    assert review["insights"]["biggest_ai_misses"]
    assert review["insights"]["largest_disagreements"]
    assert review["factor_attribution"]["overvalued_positive_factors"]
    assert "score_factors" in review["rows"][0]
    assert review["rows"][0]["ai_probability"] in {item["win_probability"] for item in analysis["predictions"]}

    saved_result_response = client.get(f"/api/results/{analysis['race']['race_id']}")
    assert saved_result_response.status_code == 200
    saved_result = saved_result_response.get_json()
    assert saved_result["ok"] is True
    assert any(entry["finish_position"] == 1 for entry in saved_result["result"]["entries"])

    saved_result_post_response = client.post(
        f"/api/results/{analysis['race']['race_id']}",
        json={"mode": "jra"},
    )
    assert saved_result_post_response.status_code == 200
    assert saved_result_post_response.get_json()["result_source"] == "saved_provider"

    saved_analysis_response = client.post(
        "/api/analyze",
        json={
            "race_date": "2026-08-16",
            "racecourse": "札幌",
            "race_number": 11,
            "mode": "jra",
        },
    )
    assert saved_analysis_response.status_code == 200
    saved_analysis = saved_analysis_response.get_json()
    assert saved_analysis["restored_from_saved"] is True
    assert saved_analysis["result"] is not None
    assert len(saved_analysis["predictions"]) == len(analysis["predictions"])

    result_diagnostics_response = client.post(
        f"/api/result-diagnostics/{analysis['race']['race_id']}",
        json={
            "race_date": "2026-08-16",
            "racecourse": "札幌",
            "race_number": 11,
            "mode": "demo",
        },
    )
    assert result_diagnostics_response.status_code == 200
    result_diagnostics = result_diagnostics_response.get_json()
    assert result_diagnostics["entry_count"] == 3
    assert result_diagnostics["winner_count"] == 1

    races_response = client.get("/api/races?limit=10")
    assert races_response.status_code == 200
    races = races_response.get_json()["races"]

    learning_response = client.get("/api/learning")
    assert learning_response.status_code == 200
    learning = learning_response.get_json()
    dataset_response = client.get("/api/training-dataset")
    assert dataset_response.status_code == 200
    dataset = dataset_response.get_json()

    assert races[0]["race_id"] == analysis["race"]["race_id"]
    assert races[0]["entry_count"] == len(analysis["entries"])
    assert races[0]["snapshot_count"] == len(analysis["predictions"])
    assert races[0]["review_count"] == 1
    assert races[0]["latest_ai_brier_score"] is not None
    assert races[0]["parse_status"] == "ok"
    assert races[0]["provider_name"] == "MockProvider"
    assert races[0]["available_feature_ratio"] is not None

    filtered_response = client.get("/api/races?racecourse=札幌&has_result=true&has_review=true")
    assert filtered_response.status_code == 200
    assert [item["race_id"] for item in filtered_response.get_json()["races"]] == [analysis["race"]["race_id"]]

    assert client.get("/api/races?racecourse=東京").get_json()["races"] == []
    assert client.get("/api/races?has_result=false").get_json()["races"] == []
    assert client.get("/api/races?has_review=false").get_json()["races"] == []

    assert learning["comparison_summary"]["ai_wins"] + learning["comparison_summary"]["user_wins"] + learning["comparison_summary"]["ties"] == 1
    assert learning["comparison_summary"]["average_brier_delta"] is not None
    assert learning["trend_summary"]["recent_count"] == 1
    assert learning["trend_summary"]["ai_recent_brier_score"] == learning["ai_brier_score"]
    assert learning["trend_summary"]["ai_direction"] == "insufficient"
    assert learning["user_probability_quality_summary"]["review_count"] == 1
    assert learning["user_probability_quality_summary"]["average_probability_total"] == 0.3
    assert learning["data_quality_summary"]["analysis_run_count"] == 1
    assert learning["data_quality_summary"]["parse_status_counts"]["ok"] == 1
    assert learning["data_quality_summary"]["provider_counts"]["MockProvider"] == 1
    assert learning["data_quality_summary"]["average_available_feature_ratio"] is not None
    assert isinstance(learning["data_quality_summary"]["top_unavailable_features"], list)
    assert learning["data_quality_summary"]["attention_runs"][0]["race_id"] == analysis["race"]["race_id"]
    assert learning["data_quality_summary"]["attention_runs"][0]["available_feature_ratio"] is not None
    assert learning["recent_reviews"][0]["race_id"] == analysis["race"]["race_id"]
    assert learning["recent_reviews"][0]["racecourse"] == "札幌"
    assert learning["recent_reviews"][0]["race_number"] == 11
    assert learning["recent_reviews"][0]["race_name"] == "デモサマーステークス"
    assert learning["recent_reviews"][0]["comparison"]["winner"] in {"ai", "user", "tie"}
    assert learning["recent_reviews"][0]["user_probability_summary"]["probability_total"] == 0.3
    assert learning["calibration_summary"]["ai"]
    assert learning["calibration_summary"]["user"]
    assert learning["calibration_summary"]["ai"][0]["average_probability"] is not None
    assert learning["factor_attribution_summary"]["overvalued_positive_factors"]
    assert learning["learning_insights"]
    assert learning["learning_insights"][0]["message"]
    assert dataset["row_count"] == 0
    assert dataset["excluded_counts"]["invalid_prediction_time"] == len(analysis["predictions"])
    assert dataset["race_count"] == 0
    assert dataset["feature_columns"] == []
    assert "actual_win" in dataset["label_columns"]

    detail_response = client.get(f"/api/races/{analysis['race']['race_id']}")
    assert detail_response.status_code == 200
    detail = detail_response.get_json()

    assert detail["race"]["race_id"] == analysis["race"]["race_id"]
    assert detail["latest_analysis_run"]["parse_status"] == "ok"
    assert detail["latest_analysis_run"]["provider_name"] == "MockProvider"
    assert detail["latest_analysis_run"]["payload"]["parse_quality"]["recognized_entries"] == len(analysis["entries"])
    assert detail["latest_analysis_run"]["payload"]["feature_quality"]["horse_count"] == len(analysis["entries"])
    assert detail["latest_analysis_run"]["payload"]["feature_quality"]["available_feature_ratio"] is not None
    assert len(detail["entries"]) == len(analysis["entries"])
    assert len(detail["latest_predictions"]) == len(analysis["predictions"])
    assert detail["latest_simulation"]["seed"] == 321
    assert detail["latest_simulation"]["selection_notes"][0]["label"] == "本命候補"
    assert detail["latest_simulation"]["selection_notes"][0]["horse_number"] == detail["latest_simulation"]["ranking"][0]["horse_number"]
    assert detail["latest_predictions"][0]["horse_number"] in {item["horse_number"] for item in analysis["predictions"]}
    assert detail["result"]["race_id"] == analysis["race"]["race_id"]
    assert detail["latest_review"]["prediction_source"] == "snapshot"
    assert detail["latest_review"]["user_prediction_source"] == "snapshot"
    assert detail["latest_review"]["comparison"]["winner"] in {"ai", "user", "tie"}
    assert detail["latest_user_predictions"]["5"]["mark"] == "◎"

    export_response = client.get("/api/export")
    assert export_response.status_code == 200
    export_payload = export_response.get_json()
    assert export_payload["counts"]["races"] == 1
    assert export_payload["counts"]["prediction_snapshots"] == len(analysis["predictions"])
    assert export_payload["counts"]["prediction_reviews"] == 1
    assert export_payload["data"]["races"][0]["race_id"] == analysis["race"]["race_id"]

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/import.sqlite3")
    import_app = create_app()
    import_client = import_app.test_client()

    import_response = import_client.post("/api/import", json=export_payload)
    assert import_response.status_code == 200
    import_result = import_response.get_json()["result"]
    assert import_result["imported"]["races"] == 1
    assert import_result["imported"]["prediction_snapshots"] == len(analysis["predictions"])
    assert import_result["imported"]["prediction_reviews"] == 1

    imported_detail_response = import_client.get(f"/api/races/{analysis['race']['race_id']}")
    assert imported_detail_response.status_code == 200
    imported_detail = imported_detail_response.get_json()
    assert imported_detail["latest_analysis_run"]["parse_status"] == "ok"
    assert len(imported_detail["latest_predictions"]) == len(analysis["predictions"])
    assert imported_detail["latest_simulation"]["selection_notes"][0]["label"] == "本命候補"
    assert imported_detail["latest_user_predictions"]["5"]["mark"] == "◎"

    duplicate_response = import_client.post("/api/import", json=export_payload)
    assert duplicate_response.status_code == 200
    duplicate_result = duplicate_response.get_json()["result"]
    assert duplicate_result["skipped"]["prediction_snapshots"] == len(analysis["predictions"])
    assert duplicate_result["skipped"]["prediction_reviews"] == 1

    imported_export_response = import_client.get("/api/export")
    assert imported_export_response.status_code == 200
    imported_export = imported_export_response.get_json()
    assert imported_export["counts"]["races"] == 1
    assert imported_export["counts"]["prediction_snapshots"] == len(analysis["predictions"])
    assert imported_export["counts"]["prediction_reviews"] == 1

    reanalyze_response = client.post(
        "/api/analyze",
        json={
            "race_date": "2026-08-16",
            "racecourse": "札幌",
            "race_number": 11,
            "iterations": 1000,
            "seed": 321,
            "mode": "demo",
        },
    )
    assert reanalyze_response.status_code == 200
    reanalysis = reanalyze_response.get_json()
    assert reanalysis["latest_user_predictions"]["5"]["mark"] == "◎"


def test_manual_result_review_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/manual.sqlite3")
    app = create_app()
    client = app.test_client()

    analyze_response = client.post(
        "/api/analyze",
        json={
            "race_date": "2026-08-16",
            "racecourse": "札幌",
            "race_number": 11,
            "iterations": 1000,
            "seed": 777,
            "mode": "demo",
        },
    )
    assert analyze_response.status_code == 200
    analysis = analyze_response.get_json()
    manual_result = {
        "entries": [
            {
                "horse_number": entry["horse_number"]["value"],
                "horse_name": entry["horse"]["horse_name"]["value"],
                "finish_position": 1 if index == 0 else None,
                "result_status": "manual",
            }
            for index, entry in enumerate(analysis["entries"])
        ]
    }

    missing_winner_response = client.post(
        f"/api/review/{analysis['race']['race_id']}",
        json={**analysis_request_payload(), "result": {"entries": manual_result["entries"][1:]}},
    )
    assert missing_winner_response.status_code == 400

    duplicate_entries = [dict(item) for item in manual_result["entries"]]
    duplicate_entries[0]["finish_position"] = 1
    duplicate_entries[1]["finish_position"] = 1
    duplicate_response = client.post(
        f"/api/review/{analysis['race']['race_id']}",
        json={**analysis_request_payload(), "result": {"entries": duplicate_entries}},
    )
    assert duplicate_response.status_code == 400

    save_response = client.post(f"/api/results/{analysis['race']['race_id']}/manual", json=manual_result)
    assert save_response.status_code == 200
    assert save_response.get_json()["result"]["entries"][0]["result_status"] == "manual"

    saved_races_response = client.get("/api/races?has_result=true&has_review=false")
    assert saved_races_response.status_code == 200
    saved_race = saved_races_response.get_json()["races"][0]
    assert saved_race["result_source"] == "manual"
    assert saved_race["review_count"] == 0

    saved_review_response = client.post(
        f"/api/review/{analysis['race']['race_id']}",
        json={**analysis_request_payload(), "use_saved_result": True, "predictions": {}},
    )
    assert saved_review_response.status_code == 200
    saved_review = saved_review_response.get_json()
    assert saved_review["result_source"] == "saved_manual"
    assert saved_review["prediction_source"] == "snapshot"

    review_response = client.post(
        f"/api/review/{analysis['race']['race_id']}",
        json={**analysis_request_payload(), "result": manual_result, "predictions": {}},
    )
    assert review_response.status_code == 200
    review = review_response.get_json()

    assert review["result_source"] == "manual"
    assert review["prediction_source"] == "snapshot"
    assert any(row["finish_position"] == 1 for row in review["rows"])

    detail_response = client.get(f"/api/races/{analysis['race']['race_id']}")
    assert detail_response.status_code == 200
    detail = detail_response.get_json()
    assert detail["result_source"] == "manual"
    assert detail["result"]["entries"][0]["result_status"] == "manual"
    assert detail["latest_review"]["result_source"] == "manual"

    races_response = client.get("/api/races?has_result=true")
    assert races_response.status_code == 200
    assert races_response.get_json()["races"][0]["result_source"] == "manual"


def analysis_request_payload():
    return {
        "race_date": "2026-08-16",
        "racecourse": "札幌",
        "race_number": 11,
        "iterations": 1000,
        "seed": 777,
        "mode": "demo",
    }
