import pytest

from services.ml.targets import relative_finish, relative_finish_category


def test_relative_finish_accounts_for_field_size():
    assert relative_finish(1, 12) == 1.0
    assert relative_finish(12, 12) == 0.0
    assert relative_finish(5, 6) == pytest.approx(0.2)
    assert relative_finish(5, 18) == pytest.approx(13 / 17)
    assert relative_finish(5, 18) > relative_finish(5, 6)


def test_relative_finish_preserves_unknown_and_rejects_invalid_values():
    assert relative_finish(None, 12) is None
    assert relative_finish(3, None) is None
    assert relative_finish(1, 1) is None
    assert relative_finish(13, 12) is None


def test_relative_finish_category_uses_quartiles():
    assert relative_finish_category(0.75) == "GOOD"
    assert relative_finish_category(0.5) == "FAIR"
    assert relative_finish_category(0.49) == "POOR"
    assert relative_finish_category(None) is None
