from datetime import datetime, timezone

from services.common.race_eligibility import is_supported_flat_race, prediction_lead_minutes, race_start_datetime


def test_race_eligibility_parses_japanese_time_and_rejects_post_start_snapshot():
    start = race_start_datetime("2026-08-30", "15時45分")

    assert start is not None
    assert start.hour == 15
    assert prediction_lead_minutes("2026-08-30", "15時45分", datetime(2026, 8, 30, 5, 45, tzinfo=timezone.utc)) == 60
    assert prediction_lead_minutes("2026-08-30", "15時45分", datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)) == -15
    assert is_supported_flat_race("芝", "3歳以上") is True
    assert is_supported_flat_race("芝 外内", "障害3歳以上") is False
