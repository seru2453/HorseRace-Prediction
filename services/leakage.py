from __future__ import annotations

FORBIDDEN_FEATURE_KEYS = {
    "finish_position",
    "finish_time",
    "margin",
    "final_odds",
    "payout",
    "result_timestamp",
}


def assert_no_future_features(features: list[dict]) -> None:
    for row in features:
        leaked = FORBIDDEN_FEATURE_KEYS.intersection(row.keys())
        if leaked:
            raise ValueError(f"Future/result fields cannot be used as prediction features: {sorted(leaked)}")
