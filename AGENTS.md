# AGENTS.md

- Work in an Observe -> Diagnose -> Define Success -> Implement -> Test -> Review loop until the success conditions pass.
- Inspect only files related to the current change and prefer the smallest implementation that fixes the root cause.
- Do not add unrelated features, refactors, dependencies, or documentation.
- Bug fixes require a regression test that reproduces the failure.
- Run focused tests first; fix failures and retest. Use `make check` for important milestones.
- Self-review changes for null/unknown handling, edge cases, leakage, database integrity, state restoration, and reproducibility.
- Provider and prediction logic must remain separated by the common data models.
- Do not use future information, final results, final odds, payouts, or post-race corrections as prediction features.
- Missing data is unknown, not zero. Never generate evidence or race facts that were not present in prediction-time data.
- Do not implement high-frequency access, bulk crawling, CAPTCHA bypass, access-limit bypass, login automation, or private API probing against JRA.
- Do not commit secrets or personal credentials.
- Parser changes require fixture-based tests.
- Unknown or unavailable source data must be stored as `NULL` / `unknown`, not invented.
- Keep JRA-VAN migration viable by implementing providers behind `RacingDataProvider`.
- Tests are required for parser validation, prediction bounds, simulation reproducibility, and leakage prevention.
- Do not rewrite saved prediction-time snapshots after results become available.
- Keep progress updates and final reports concise; report only verified behavior as confirmed.
