# DATABASE

The MVP uses SQLite through SQLAlchemy 2.x. Tables are designed to remain portable to PostgreSQL.

Main tables:

- `races`
- `horses`
- `race_entries`
- `past_performances`
- `predictions`
- `simulations`
- `user_predictions`
- `race_results`
- `prediction_reviews`
- `data_fetch_logs`

User predictions are stored as snapshots with timestamps. Past prediction values should not be overwritten after results are known.

Prediction reviews are appended to `prediction_reviews` and are used by the learning summary API. The MVP reports review count, average Brier scores, race-level Brier breakdowns by racecourse, surface, distance band, and track condition, horse-level Brier breakdowns by popularity band, frame number, sex, and age band, plus user-rating Brier breakdowns. Small samples are flagged as statistically weak.

Parsed recent-run rows are stored in `past_performances` with `observed_in_race_id`, so the app can distinguish facts seen before prediction from later result updates. If a provider has no history for a horse, the analysis service falls back to stored local rows.
