from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DataField(BaseModel):
    value: Any = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    source: str = "unknown"


def field(value: Any, confidence: float = 1.0, source: str = "manual") -> DataField:
    return DataField(value=value, confidence=confidence, source=source)


class HorseData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    horse_id: str
    horse_name: DataField
    sire: DataField = Field(default_factory=DataField)
    dam: DataField = Field(default_factory=DataField)
    damsire: DataField = Field(default_factory=DataField)


class RaceData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    race_id: str
    race_date: DataField
    racecourse: DataField
    race_number: DataField
    race_name: DataField = Field(default_factory=DataField)
    grade: DataField = Field(default_factory=DataField)
    race_class: DataField = Field(default_factory=DataField)
    surface: DataField = Field(default_factory=DataField)
    distance: DataField = Field(default_factory=DataField)
    direction: DataField = Field(default_factory=DataField)
    start_time: DataField = Field(default_factory=DataField)
    weather: DataField = Field(default_factory=DataField)
    track_condition: DataField = Field(default_factory=DataField)
    field_size: DataField = Field(default_factory=DataField)


class RaceScheduleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    race_date: str
    racecourse: str
    race_number: int
    start_time: str
    race_type: str = "flat"
    source_url: str | None = None


class RaceEntryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    race_id: str
    horse: HorseData
    frame_number: DataField
    horse_number: DataField
    sex: DataField = Field(default_factory=DataField)
    age: DataField = Field(default_factory=DataField)
    jockey_name: DataField = Field(default_factory=DataField)
    trainer_name: DataField = Field(default_factory=DataField)
    carried_weight: DataField = Field(default_factory=DataField)
    horse_weight: DataField = Field(default_factory=DataField)
    horse_weight_change: DataField = Field(default_factory=DataField)
    win_odds: DataField = Field(default_factory=DataField)
    popularity: DataField = Field(default_factory=DataField)
    career_starts: DataField = Field(default_factory=DataField)
    career_wins: DataField = Field(default_factory=DataField)
    career_seconds: DataField = Field(default_factory=DataField)
    career_thirds: DataField = Field(default_factory=DataField)
    career_win_rate: DataField = Field(default_factory=DataField)
    career_top3_rate: DataField = Field(default_factory=DataField)
    total_prize_money: DataField = Field(default_factory=DataField)
    scratched: DataField = Field(default_factory=lambda: field(False, 1.0, "default"))


class PastPerformanceData(BaseModel):
    horse_id: str
    race_date: str
    racecourse: str | None = None
    surface: str | None = None
    distance: int | None = None
    finish_position: int | None = None
    margin: float | None = None
    final_three_furlongs: float | None = None


class RaceResultEntryData(BaseModel):
    horse_number: int
    horse_name: str
    finish_position: int | None = None
    result_status: str | None = None
    finish_time: str | None = None
    margin: str | None = None
    final_three_furlongs: float | None = None
    corner_positions: str | None = None
    horse_weight: int | None = None
    horse_weight_change: int | None = None
    jockey_name: str | None = None
    popularity: int | None = None


class RaceResultData(BaseModel):
    race_id: str
    result_timestamp: str
    entries: list[RaceResultEntryData]


class QualityIssue(BaseModel):
    code: str
    message: str
    field: str | None = None
    horse_number: int | None = None


class ParseQuality(BaseModel):
    status: str
    recognized_entries: int
    expected_entries: int | None = None
    issues: list[QualityIssue] = Field(default_factory=list)


class HorseFeature(BaseModel):
    race_id: str
    horse_id: str
    horse_number: int
    horse_name: str
    features: dict[str, Any]
    unavailable_features: list[str] = Field(default_factory=list)
    as_of: str


class PredictionData(BaseModel):
    race_id: str
    horse_id: str
    horse_number: int
    horse_name: str
    base_score: float
    win_probability: float
    top2_probability: float
    top3_probability: float
    uncertainty: float
    confidence: float
    explanation: dict[str, list[str]]
    model_version: str
    feature_version: str
    champion_version: str = "baseline-v1"
    base_ability_score: float | None = None
    ability_index: float | None = None
    ability_components: dict[str, float | None] = Field(default_factory=dict)
    ability_sample_status: str = "UNKNOWN"
    raw_condition_adjustment: float = 0.0
    condition_adjustment: float = 0.0
    feature_group_adjustments: dict[str, float] = Field(default_factory=dict)
    raw_probability: float | None = None
    calibrated_probability: float | None = None
    calibration_version: str = "none"
    base_rank: int | None = None
    final_rank: int | None = None
    rank_delta: int = 0
    largest_positive_adjustment: float = 0.0
    largest_negative_adjustment: float = 0.0
    adjustment_source: str | None = None
    confidence_label: str = "LOW"
    condition_sample_statuses: dict[str, str] = Field(default_factory=dict)
    new_horse_flags: dict[str, bool] = Field(default_factory=dict)
    frame_number: int | None = None
    running_style: str | None = None
    average_first_corner_ratio: float | None = None
    field_early_pressure: float | None = None
    track_bias_adjustment: float = 0.0
    track_bias_sample_status: str = "UNKNOWN"
    parameter_version: str = "stability-v1"
    simulation_version: str = "monte-carlo-heteroskedastic-v3"
    win_odds: float | None = None
    raw_expected_value_index: float | None = None
    expected_value_index: float | None = None
    expected_value_probability: float | None = None
    fair_odds: float | None = None
    expected_value_is_adjusted: bool = False
    expected_value_model_weight: float | None = None
    expected_value_method: str = "unavailable"
    qualitative_adjustment: float = 0.0
    qualitative_confidence: float | None = None
    qualitative_status: str = "disabled"
    qualitative_model_version: str | None = None
    qualitative_input_hash: str | None = None
    current_form: float | None = None
    condition_fit: float | None = None
    previous_race_residual: float | None = None
    rebound_score: float | None = None
    field_size: int | None = None
    jev_features: dict[str, float | None] = Field(default_factory=dict)


class ScenarioResult(BaseModel):
    name: str
    top_horse_numbers: list[int]
    note: str


class SelectionNote(BaseModel):
    label: str
    horse_number: int | None = None
    horse_name: str | None = None
    priority: int
    confidence: float | None = None
    win_rate: float | None = None
    top3_rate: float | None = None
    risk_level: str
    note: str


class SimulationResult(BaseModel):
    race_id: str
    iterations: int
    seed: int
    ranking: list[dict[str, Any]]
    position_distribution: dict[int, dict[int, float]]
    scenarios: list[ScenarioResult]
    selection_notes: list[SelectionNote] = Field(default_factory=list)
    decision_summary: dict[str, Any] = Field(default_factory=dict)
    candidate_groups: dict[str, Any] = Field(default_factory=dict)
    scenario_consensus: list[dict[str, Any]] = Field(default_factory=list)


class UserPredictionInput(BaseModel):
    mark: str | None = None
    user_win_probability: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: int | None = Field(default=None, ge=1, le=5)
    track_rating: int | None = Field(default=None, ge=-2, le=2)
    pedigree_rating: int | None = Field(default=None, ge=-2, le=2)
    jockey_rating: int | None = Field(default=None, ge=-2, le=2)
    rotation_rating: int | None = Field(default=None, ge=-2, le=2)
    pace_rating: int | None = Field(default=None, ge=-2, le=2)
    comment: str | None = None


class AnalysisResult(BaseModel):
    race: RaceData
    entries: list[RaceEntryData]
    parse_quality: ParseQuality
    predictions: list[PredictionData]
    simulation: SimulationResult
    message: str
