from services.prediction.expected_value import estimate_expected_value


def test_expected_value_uses_raw_probability_below_high_odds_threshold():
    estimate = estimate_expected_value(0.03, 20.0, confidence=0.8, market_probability=0.04)

    assert estimate is not None
    assert estimate["expected_value_index"] == 0.6
    assert estimate["raw_expected_value_index"] == 0.6
    assert estimate["expected_value_is_adjusted"] is False
    assert estimate["expected_value_method"] == "raw-v1"


def test_high_odds_expected_value_is_shrunk_toward_market_probability():
    estimate = estimate_expected_value(0.03, 100.0, confidence=0.8, market_probability=0.01)

    assert estimate is not None
    assert estimate["raw_expected_value_index"] == 3.0
    assert estimate["expected_value_index"] == 1.6
    assert estimate["expected_value_probability"] == 0.016
    assert estimate["fair_odds"] == 62.5
    assert estimate["expected_value_is_adjusted"] is True
    assert estimate["expected_value_model_weight"] == 0.3


def test_expected_value_adjustment_starts_at_thirty_odds():
    estimate = estimate_expected_value(0.05, 30.0, confidence=0.8, market_probability=0.03)

    assert estimate is not None
    assert estimate["raw_expected_value_index"] == 1.5
    assert estimate["expected_value_index"] == 1.08
    assert estimate["expected_value_is_adjusted"] is True


def test_high_odds_adjustment_respects_lower_prediction_confidence():
    estimate = estimate_expected_value(0.03, 100.0, confidence=0.1, market_probability=0.01)

    assert estimate is not None
    assert estimate["expected_value_index"] == 1.2
    assert estimate["expected_value_model_weight"] == 0.1


def test_expected_value_is_unavailable_without_valid_odds_or_confidence():
    assert estimate_expected_value(0.03, None, confidence=0.8) is None
    assert estimate_expected_value(0.03, 100.0, confidence=float("nan")) is None
