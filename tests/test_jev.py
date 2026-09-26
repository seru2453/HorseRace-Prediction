from database.repository import RaceRepository
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.mock import MockProvider
from services.evidence.collector import QualitativeEvidenceCollector
from services.prediction.jev import JEV_FEATURE_NAMES, JevConfig, JevQualitativeEvaluator


def _evidence_for_first_horse(service: RaceAnalysisService):
    race = service.provider.get_race("2026-08-16", "札幌", 11)
    first = service.provider.get_race_entries(race.race_id)[0]
    evidence = QualitativeEvidenceCollector().normalize(
        source_type="trainer_comment",
        source_name="fixture",
        race_id=race.race_id,
        horse_id=first.horse.horse_id,
        raw_text="調教状態は前走時より改善。",
        confidence=0.8,
    )
    with service.session_factory() as session:
        RaceRepository(session).save_qualitative_evidence(evidence)


def test_jev_structures_evidence_without_changing_champion_probability(tmp_path):
    captured = {}

    def transport(payload):
        captured.update(payload)
        return {
            "model": "jev-test",
            "answers": {
                key: {"type": "score", "score": 4.0, "confidence": 0.8}
                for key in payload["questions"]
            },
        }

    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/jev.sqlite3")
    baseline = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=True))
    _evidence_for_first_horse(service)
    service.qualitative_evaluator = JevQualitativeEvaluator(
        JevConfig(api_key="test", enabled=True), transport=transport
    )

    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))
    first = next(item for item in result.predictions if item.horse_number == 1)
    baseline_first = next(item for item in baseline.predictions if item.horse_number == 1)

    assert first.qualitative_adjustment == 0.0
    assert first.win_probability == baseline_first.win_probability
    assert first.qualitative_status == "structured"
    assert first.qualitative_model_version == "jev-test"
    assert first.jev_features["jev_training_improvement"] == 1.0
    assert len(captured["state"]["horses"]) == 1
    assert captured["state"]["horses"][0]["evidence"][0]["text"]


def test_jev_does_not_call_api_or_invent_features_without_evidence(tmp_path):
    called = False

    def transport(payload):
        nonlocal called
        called = True
        return {"answers": {}}

    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/jev-none.sqlite3")
    service.qualitative_evaluator = JevQualitativeEvaluator(
        JevConfig(api_key="test", enabled=True), transport=transport
    )
    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))

    assert called is False
    assert all(item.qualitative_status == "no_evidence" for item in result.predictions)
    assert all(item.qualitative_confidence is None for item in result.predictions)
    assert all(all(item.jev_features.get(name) is None for name in JEV_FEATURE_NAMES) for item in result.predictions)
