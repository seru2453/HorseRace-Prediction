# ARCHITECTURE

The application keeps the data source behind a provider boundary:

`Data Provider -> Common Data Models -> Features -> Prediction -> Simulation -> User Prediction -> Result -> Review`

`Prediction`, `Simulation`, UI, and review code never read provider-specific HTML. This allows `JraOfficialProvider` to be replaced later by `JraVanProvider` without rewriting the analysis pipeline.

Runtime adapters live at the edge:

- `apps/api/main.py`: intended FastAPI adapter.
- `apps/api/flask_app.py`: local runnable adapter for this environment.
- `apps/web/static`: PWA UI.

Core logic lives in `services/` and persistence lives in `database/`.
