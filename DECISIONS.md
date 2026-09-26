# DECISIONS

- The existing `HorseRace/HorsePrediction` path was a zero-byte file, so it was preserved as `HorsePrediction.placeholder` and the project directory was created at the expected path.
- Demo mode uses completely fictional race data.
- Live JRA mode follows public POST navigation from the official race-card selection page. It was smoke-tested on `2026-08-16 札幌 11R`.
- JRA result mode follows public POST navigation from the official result selection page. It was smoke-tested on `2026-08-08 札幌 11R`.
- FastAPI is the intended backend stack, but a Flask adapter is included because FastAPI/uvicorn are not installed in the current environment.
- The first model is rule-based. It does not claim high prediction accuracy.
- Missing source fields remain `NULL`/`unknown`; they are not inferred.
- JRA race-card URLs are not generated from an undocumented checksum. The provider discovers them from official public selection pages or an explicit local URL index.
- 2026-08-30: Reviews are canonicalized to the latest row per race. Existing duplicate rows remain preserved but are excluded from learning and summary calculations.
- 2026-08-30: Training excludes prediction snapshots stored after the corresponding result was stored, and ML blending requires at least 0.001 Brier improvement without LogLoss regression.
