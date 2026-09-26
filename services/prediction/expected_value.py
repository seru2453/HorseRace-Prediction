from __future__ import annotations

import math


HIGH_ODDS_THRESHOLD = 30.0
MAX_MODEL_WEIGHT = 0.30
EXPECTED_VALUE_METHOD = "market-shrinkage-v1"


def estimate_expected_value(
    win_probability: float,
    win_odds: float | None,
    confidence: float,
    market_probability: float | None = None,
) -> dict[str, float | bool | str] | None:
    probability = _finite_float(win_probability)
    odds = _finite_float(win_odds)
    reliability = _finite_float(confidence)
    if probability is None or not 0.0 <= probability <= 1.0 or odds is None or odds <= 0.0:
        return None
    if reliability is None or not 0.0 <= reliability <= 1.0:
        return None

    raw_index = probability * odds
    adjusted = odds >= HIGH_ODDS_THRESHOLD
    value_probability = probability
    model_weight = 1.0
    method = "raw-v1"

    if adjusted:
        market = _finite_float(market_probability)
        if market is None or not 0.0 < market <= 1.0:
            market = min(1.0, 1.0 / odds)
        model_weight = min(MAX_MODEL_WEIGHT, reliability)
        value_probability = market + model_weight * (probability - market)
        method = EXPECTED_VALUE_METHOD

    return {
        "win_odds": round(odds, 3),
        "raw_expected_value_index": round(raw_index, 6),
        "expected_value_index": round(value_probability * odds, 6),
        "expected_value_probability": round(value_probability, 6),
        "fair_odds": round(1.0 / value_probability, 3) if value_probability > 0.0 else 0.0,
        "expected_value_is_adjusted": adjusted,
        "expected_value_model_weight": round(model_weight, 3),
        "expected_value_method": method,
    }


def _finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
