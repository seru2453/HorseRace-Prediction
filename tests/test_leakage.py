import pytest

from services.leakage import assert_no_future_features


def test_leakage_guard_rejects_result_fields():
    with pytest.raises(ValueError):
        assert_no_future_features([{"horse_number": 1, "finish_position": 1}])


def test_leakage_guard_accepts_pre_race_features():
    assert_no_future_features([{"horse_number": 1, "carried_weight": 57.0, "distance": 2000}])
