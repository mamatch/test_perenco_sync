# mock_gmao — DIMO Maint MX connector API (mock)

```bash
uv sync
uv run uvicorn mock_gmao.main:app --port 8080 --reload
# Swagger UI: http://localhost:8080/docs   OpenAPI: http://localhost:8080/openapi.json
uv run pytest -q
```

Tenant and key: `PERENCO` → `demo-key-global`.

State is persisted under `state/` after every write. `POST /_admin/<tenant>/reset` reloads the seed
from `data/<tenant>.json` (regenerate the seed with `uv run --no-project ../tools/generate_seeds.py`).

Behaviour knobs (environment variables): `MOCK_RATE_LIMIT_PER_MINUTE` (default 50, 0 disables),
`MOCK_CHAOS_RATE` (default 0.03, 0 disables), `MOCK_CHAOS_SEED`, `MOCK_LATENCY_MS`, `MOCK_STATE_DIR`.
