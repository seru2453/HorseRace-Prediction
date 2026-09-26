# MODEL

The MVP uses `RuleBasedPredictor`.

Model version: `rule-based-stable-v31`.

It calculates:

- `base_score`
- `base_ability_score`
- `raw_condition_adjustment` and capped `condition_adjustment`
- `win_probability`
- `raw_probability` and `calibrated_probability`
- `top2_probability`
- `top3_probability`
- `uncertainty`
- `confidence`
- `base_rank`, `final_rank`, and `rank_delta`
- positive and negative explanation text
- score-factor breakdown text
- per-feature-group adjustment totals and extreme-adjustment attribution

The UI states that the current model is a simple model while training data is still limited. Factor-level corrections from saved reviews remain challengers and are not applied automatically. The logistic-regression baseline is used only when enough reviewed races exist and walk-forward validation passes. Future stages can add LightGBM once enough historical snapshots and results exist.

Condition features use sample-size shrinkage toward a neutral prior and distinguish `UNKNOWN`, `LOW_SAMPLE`, and `SUFFICIENT`. Feature-group and total adjustment caps prevent correlated condition signals from overwhelming base ability. Probabilities are conservatively shrunk toward the field average according to each horse's uncertainty before simulation.

Base ability now blends the legacy record score with a recency-weighted composite index. The composite uses field-relative finish quality, time behind the winner when available, track-condition/course-adjusted speed, and opponent grade. Sparse histories are shrunk toward a neutral prior, and missing components are excluded rather than scored as zero. Saved official results enrich future local histories with exact time gaps; history lookup is restricted to races before the prediction date.

Training data for future models is exported as one row per horse. Each row joins the prediction-time `raw_features` from `prediction_snapshots` with review labels:

- `actual_win`
- `actual_top3`
- `finish_position`

This dataset is available from `/api/training-dataset` and `make dataset`.

`make train` trains the first baseline model from that dataset:

- model: logistic regression
- default label: `actual_win`
- alternate label: `actual_top3`
- default minimum: 20 horse rows, 4 races, 2 positive labels, and 2 negative labels
- output: in-sample Brier, LogLoss, and top coefficients

When rows, races, or class balance are insufficient, the command reports `insufficient_data` instead of fitting a misleading model.
Prediction-time blending is enabled only when walk-forward validation using earlier races improves Brier score by at least 0.001 without worsening LogLoss versus the rule-based probabilities.

During prediction, the app excludes the target race from the training dataset, fits the logistic baseline from prior reviewed races, and blends the learned win probabilities into the rule-based probabilities with a capped default weight of 0.25. If the baseline cannot train, prediction stays rule-based plus guarded factor adjustments.

Current feature inputs include basic race/entry facts plus available recent-run facts parsed from JRA race cards:

- past average finish
- past win rate
- past top-3 rate
- past bad-finish rate
- recent finish range and consistency label
- recent-3 average finish
- previous-run finish position, finish ratio, top-3 flag, and top-half flag
- field-size-adjusted recent finish ratio and top-half rate
- recency-weighted recent finish ratio and top-3 rate
- average final three furlongs
- average, best, and recency-weighted recent speed derived from finish time and distance, plus adjusted speed deltas by surface, distance band, track condition, and racecourse
- same-distance and same-distance-band top-3 rates
- same-surface top-3 rate
- same-track-condition top-3 rate
- same-racecourse top-3 rate
- same-surface-distance, same-surface-distance-band, and same-racecourse-distance top-3 rates
- same-jockey top-3 rate
- same-jockey-as-last-run flag
- sire and damsire top-3 trends for the same surface and distance band, aggregated only from saved prior race results
- decayed same-course track bias from available earlier races, with a deliberately small cap
- course-specific frame bias
- running style from recent corner positions
- average first-corner position ratio and position gain
- field early-pressure ratio from all runners with known running style
- market overperformance and longshot top-3 rates from recent popularity
- recent graded-race experience, graded top-3 rate, and grade step from the previous run
- age band
- carried-weight change from the previous run
- field-relative total prize money, career win-rate rank, and career top-3-rate rank
- latest recent horse weight, average recent horse weight, and latest recent horse-weight change
- days since last run
- recent run count within 90 days
- distance change direction and meter magnitude
- surface change
- single-win odds and popularity when available on the JRA race card
- implied win probability derived from single-win odds
- field-relative implied win probability rank and normalized market-support share
- career starts, career win/top-3 rates, and total prize money when available

Monte Carlo simulation runs 1,000 to 100,000 iterations, defaults to 10,000, and accepts a seed for reproducibility.
It samples finishing order from normalized AI win probabilities with uncertainty-driven race-day variance.
Each simulation ranking row includes rank, win gap to the leader, top-3 cutline gap, and most likely finishing position.

Simulation output also includes selection notes:

- race decision summary
- candidate groups for core, opposition, and reserve horses
- scenario consensus across average, inner-favoring, outer-favoring, and upset scenarios; inner/outer scenarios retain ability as the main signal and apply small adjustments from actual frame, inferred running position, field pressure, and observed track bias
- main contender
- supporting contenders
- longshot candidate
- risk cautions

These notes organize prediction and simulation probabilities for review. They do not calculate stakes, connect to betting services, or promise profit.

Post-race review keeps probability metrics such as Brier and LogLoss, but its largest-miss display uses absolute finish-rank error across every finisher. Factor learning separately records three-or-more-rank overvaluation and undervaluation so major misses outside the top three contribute challenger evidence.
