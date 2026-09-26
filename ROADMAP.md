# ROADMAP

1. Add more JRA official fixture tests across racecourses, scratches, weight changes, and expired/not-yet-published pages.
2. Extend result parser coverage for non-finishers, disqualifications, cancellations, and unusual margin notation.
3. Add richer condition-level learning breakdowns for combinations of popularity, frame, race condition, and user rating dimensions once enough reviewed races exist.
4. Add richer `PastPerformance` quality checks and source/confidence metadata per field.
5. Add Alembic migrations.
6. Replace the local Flask adapter with the FastAPI runtime after dependencies are installed.
7. Add Playwright browser tests.
8. Add time-series model training once enough local snapshots exist.
9. Implement JRA-VAN bridge through `JraVanProvider`.
10. Add user tendency reports after enough reviewed races accumulate.
