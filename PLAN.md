# PLAN

## Phase 1-4: Foundation

- Common provider-neutral models: `RaceData`, `RaceEntryData`, `HorseData`, `RaceResultData`.
- Provider interface: `RacingDataProvider`.
- Implemented providers: `MockProvider`, conservative `JraOfficialProvider`, placeholder `JraVanProvider`.
- `JraOfficialProvider` resolves current race-card URLs from public official selection pages and caches all fetches.
- HTML parser uses structured markup first, supports the observed JRA出馬表 and レース結果 table shapes, and reports quality issues.

## Phase 5-14: MVP Flow

- Mobile-first PWA served from `apps/web/static`.
- API adapters under `apps/api`.
- End-to-end flow: race input -> provider -> parser/quality -> features -> rule-based prediction -> Monte Carlo simulation -> user prediction snapshot -> result update -> review.
- CLI prediction report can run the same analysis pipeline for demo or JRA mode without using the web UI.
- CLI prediction report can append result review for races whose results are available.
- CLI can review saved AI prediction snapshots by race_id after race-card pages are no longer available.
- CLI can list saved prediction races and filter races that still need review.
- CLI can batch-review saved unreviewed races and persist reviews for races whose results are available.
- Local dev mode automatically checks analyzed unreviewed races and saves reviews after JRA results become available.
- AI prediction snapshots are append-only and retain raw prediction-time features, model/feature versions, probabilities, and simulation seed.
- Reviews prefer saved AI prediction snapshots and only recompute when no snapshot exists.
- Review saving is idempotent per race, and existing duplicate reviews are canonicalized to the latest row for learning and summaries.
- Review snapshots are persisted and exposed through the learning status tab.
- Saved race history is exposed through the UI and `/api/races`.
- Saved race history can be filtered by racecourse, result availability, and review availability.
- History and learning status are accessible before running a new analysis.
- Provider diagnostics can check race-card availability and parse quality before saving an analysis.
- Result diagnostics can check result-page availability, result row count, and winner count before saving results.
- Analyze failures return structured provider/parser error messages.
- Local saved data can be exported as JSON through `/api/export` and the UI.
- Exported JSON can be imported non-destructively through `/api/import` and the UI, with duplicate snapshots/reviews skipped.
- Saved race details can be reopened from history using persisted entries, latest AI snapshots, result, and latest review.
- Saved analyses can be restored into the main race tabs from history.
- Reload restores the current screen only within the same local calendar day; a new day defaults to today's date instead of reopening yesterday's race.
- Restored saved analyses prefill the user prediction form from latest saved user predictions.
- Analysis runs persist parse quality, provider metadata, and issue counts for later audit.
- Analysis runs persist feature quality, including missing history count and available feature ratio.
- Future/result field leakage guard runs before prediction.
- Simulation generates selection notes for main contender, supporting contenders, longshot candidate, and risk cautions without stake calculation.
- Simulation generates a race decision summary for favorite-leaning, wide-open, top-3 contested, and low-confidence races.
- Simulation groups candidates into core, opposition, and reserve sets for review without stake calculation.
- Simulation rankings include leader gap, top-3 cutline gap, and most likely finishing position.
- Simulation summarizes scenario consensus by counting top-3 appearances across pace/frame/upset scenarios.
- Saved user predictions are reused automatically during review, with current request inputs taking precedence.
- User prediction forms only collect marks, keeping win probabilities, confidence, comments, and per-factor user ratings out of the UI.
- User prediction forms can draft empty marks from AI candidate groups without saving automatically.
- Mark-only user predictions are converted to provisional probabilities for backend review comparison.
- Manual result entry can save race results and run reviews when provider result fetches are unavailable.
- Manual results can also be saved without immediately creating a review.
- Saved results can be reused for later reviews without provider refetch or manual re-entry.
- Restored saved analyses prefill manual finish-position inputs from saved results.
- Manual result entry validates duplicate and out-of-range finish positions, and saved history shows the result source.
- Reviews include AI/User comparison, Brier delta, largest AI misses, and largest AI/User disagreements.
- Largest AI misses are ranked by absolute predicted-rank versus finish-rank error across all finishers, rather than winner-only Brier error.
- Reviews aggregate positive factors on horses overvalued by at least three ranks and negative factors on horses undervalued by at least three ranks as challenger evidence.
- Reviews display largest user prediction misses when user win probabilities are available.
- Reviews persist score-factor attribution for overvalued non-winners and winner penalties for later model tuning.
- Reviews normalize score-factor keys so amount variants are grouped as the same learning signal.
- Learning status summarizes AI wins, user wins, ties, and average Brier delta.
- Learning status includes recent Brier trend summaries for AI and user predictions.
- Learning status includes analysis data-quality summaries from persisted analysis runs.
- Learning status aggregates unavailable feature counts across persisted analysis runs.
- Learning status highlights analysis runs that need attention because of parser issues or low feature availability.
- Learning status includes probability calibration by predicted win-probability bucket for AI and user predictions.
- Learning status generates short learning insights from comparison, calibration, and breakdown summaries.
- Learning status aggregates persisted factor attribution as backend data for future weight adjustment.
- Learning status aggregates factor-level calibration gaps between AI probability and actual win rate.
- Learning status generates factor weight adjustment candidates and applies only `candidate` adjustments to later predictions.
- Factor adjustment candidates include readiness guardrails based on sample count and calibration gap.
- Training dataset export joins prediction snapshot raw features with review labels as one row per horse.
- Training excludes prediction snapshots stored after the corresponding race result was stored.
- Training dataset is exposed through `/api/training-dataset` and `make dataset`.
- Logistic-regression baseline training can run from the training dataset with explicit insufficient-data reporting.
- Prediction now blends the logistic baseline only when enough prior reviewed races exist, excluding the target race to avoid result leakage.
- Logistic blending requires at least 0.001 walk-forward Brier improvement without LogLoss regression.
- Logistic blending is applied only when walk-forward validation using earlier races beats the rule-based probabilities on Brier score without worsening LogLoss.
- Recent learning reviews link back to saved race detail and restored saved analyses.
- Learning status includes racecourse, surface, distance-band, and track-condition breakdowns.
- Horse-level review breakdowns include popularity band, frame number, sex, and age band.
- JRA race-card past-performance columns are parsed into rule-based model feature inputs.
- JRA race-card single-win odds and popularity are parsed when available and displayed as market context.
- JRA race-card career record and total prize money are parsed when available.
- JRA baba pages are parsed when available to fill weather and current turf/dirt track condition.
- Rule-based predictions use recent win rate, bad-finish rate, recent trend, same-condition top-3 rates, same-track-condition top-3 rates, same-racecourse top-3 rates, same-jockey signals, and light market-support adjustments.
- Rule-based predictions normalize single-win odds into field-relative market rank and support share.
- Rule-based predictions use recent corner positions to estimate running style and position-gain tendencies.
- Rule-based predictions use recent popularity versus finish to detect market overperformance and longshot top-3 tendencies.
- Rule-based predictions use rotation signals for short intervals, standard spacing, moderate freshening, long layoffs, and 90-day run count.
- Rule-based predictions use field early-pressure to adjust front-runner and closer expectations.
- Rule-based predictions use course-specific frame bias for known course-distance patterns.
- Rule-based predictions use combined condition fit for surface-distance and racecourse-distance records.
- Rule-based predictions use distance-band fit so sparse exact-distance histories still contribute.
- Rule-based predictions normalize recent finish positions by field size and top-half finish rate.
- Rule-based predictions apply recency-weighted finish and top-3 signals so newer races matter more.
- Rule-based predictions evaluate previous-run top-3 and field-size-adjusted previous-run finish separately from broader recent averages.
- Rule-based predictions use recent finish-range consistency to separate stable runners from volatile ones.
- Rule-based predictions scale distance-change risk by the meter difference from the previous run.
- Rule-based predictions evaluate carried-weight changes from the previous run separately from current carried weight.
- Rule-based predictions use recent horse-weight trend when current race-day horse weight is not available yet.
- Rule-based predictions use age-band adjustments for prime, mature, veteran, and senior runners.
- Rule-based predictions use recent graded-race experience and grade-step signals as an early opponent-strength proxy.
- Rule-based predictions compare total prize money and career win/top-3 rates within the current field.
- Rule-based predictions convert recent finish times and distances into recency-weighted speed indicators adjusted by surface, distance band, track condition, and racecourse.
- Base ability and condition adjustments are persisted separately, with configurable feature-group and total caps.
- Base ability blends recent finish quality, winner-time gap, track-adjusted speed, and opponent grade with recency weighting and sparse-sample shrinkage.
- Saved results enrich future horse histories with exact time gaps, while as-of date filtering prevents later races from entering earlier predictions.
- Sparse course, distance, surface, jockey, pedigree, and track-bias evidence is shrunk toward a neutral prior and labeled `UNKNOWN`, `LOW_SAMPLE`, or `SUFFICIENT`.
- Sire and damsire condition trends use only saved results from races before the target date with the same surface and distance band.
- Track bias uses earlier compatible races with time decay and a small maximum adjustment.
- Inner/outer simulation scenarios combine ability with actual frame, inferred running position, field pressure, and observed track bias instead of sorting by horse number.
- Predictions persist raw/calibrated probabilities, rank changes, extreme adjustments, confidence, and horse-specific uncertainty for later review.
- Large unsupported rank changes are guarded, and review-derived factor changes remain challengers until explicitly promoted.
- AI prediction cards show score-factor breakdowns alongside 0.5-step strength stars and confidence.
- AI prediction cards cap displayed positive and negative reason lists to keep the UI compact.
- Parsed past performances are saved locally and reused when provider history is unavailable.

## Phase 15-16: Verification And Docs

- Pytest coverage for parser, prediction, simulation, leakage, and fixture E2E.
- Documentation added for architecture, provider rules, database, model, security, roadmap, and decisions.

## Known Scope Limits

- Live JRA official flow is smoke-tested for one race, but broader race/page coverage still needs fixtures.
- FastAPI is declared and an entry point exists, but local runnable mode uses Flask because FastAPI is not installed locally.
- Demo fixture data is fictional.
