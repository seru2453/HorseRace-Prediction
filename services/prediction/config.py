from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StabilityConfig:
    condition_adjustment_cap: float = 0.30
    feature_group_caps: dict[str, float] = field(
        default_factory=lambda: {
            "FORM": 0.30,
            "SURFACE": 0.08,
            "DISTANCE": 0.10,
            "COURSE": 0.08,
            "PEDIGREE": 0.06,
            "JOCKEY": 0.06,
            "ROTATION": 0.08,
            "PACE": 0.08,
            "TRACK_BIAS": 0.03,
            "MARKET": 0.14,
            "PHYSICAL": 0.10,
            "LEARNING": 0.05,
            "JEV": 0.04,
            "REBOUND": 0.08,
        }
    )
    shrinkage_strength: float = 6.0
    pedigree_shrinkage_strength: float = 12.0
    prior_top3_rate: float = 0.30
    minimum_sample_size: int = 3
    sufficient_sample_size: int = 5
    track_bias_decay: tuple[float, ...] = (1.0, 0.8, 0.6, 0.3, 0.1)
    rank_delta_warning: int = 3
    rank_delta_high_risk: int = 5
    uncertainty_min: float = 0.22
    uncertainty_max: float = 0.82
    probability_score_exponent: float = 1.25
    probability_shrinkage_max: float = 0.35
    extreme_single_adjustment: float = 0.12
    ability_prior: float = 0.50
    ability_shrinkage_strength: float = 3.0
    ability_recency_weights: tuple[float, ...] = (1.0, 0.8, 0.6, 0.4, 0.25)
    ability_finish_weight: float = 0.50
    ability_margin_weight: float = 0.15
    ability_speed_weight: float = 0.20
    ability_opponent_weight: float = 0.15
    ability_score_scale: float = 0.70
    ability_index_blend_weight: float = 0.40
    scenario_frame_weight: float = 0.08
    scenario_style_weight: float = 0.05
    scenario_pace_weight: float = 0.04
    scenario_track_bias_weight: float = 1.0


DEFAULT_STABILITY_CONFIG = StabilityConfig()
