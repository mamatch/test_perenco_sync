# 3. The CMMS connector API (DIMO Maint MX, mocked)

Base URL in the sandbox: `http://localhost:8080`. Interactive documentation: `http://localhost:8080/docs`.
The machine-readable spec is in `docs/openapi.json`. The real API is much larger (work orders,
parts, purchase orders…); the mock keeps only what this exercise needs, with the real field names.

## Authentication

Every connector call carries the header `X-API-Key`. The `{tenant}` path segment identifies the
customer instance on the vendor's platform:

| Tenant (path) | API key |
|---|---|
| `PERENCO` | `demo-key-global` |

In production the key is a secret, and you should treat it as such in your design (the sandbox key
is fake).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/{tenant}/connector/Asset/Filter` | Paginated list of assets. Body: `currentPage` (1-based), `pageSize` (max 1000), optional filters `archived`, `assetCode`, `parentCode`, `assetFamilyCode`, `bodyNames[]`, `startLastModificationDateTime`, `endLastModificationDateTime`. |
| GET | `/{tenant}/connector/Asset/Get?code=…` | One asset with full details, including `archived` and `meters`. |
| POST | `/{tenant}/connector/Asset/Post` | Create an asset: `assetCode`, `assetName`, `assetFamilyCode`, `assetParentCode`, `bodyNames[]`. |
| PATCH | `/{tenant}/connector/Asset/Patch` | Update an asset by `assetCode`: `archived`, `assetName`, `parentCode`, `bodyNames[]`, `assetFamilyCode`. |
| POST | `/{tenant}/connector/Asset/MeterUpdate` | Record a meter reading: `assetCode`, `dateTime`, `value` (integer), `name`, `unitCode`. |
| POST | `/{tenant}/connector/ImportExport/Export` | Bulk exports: `{"action": "BodyExport"}` (sites) or `{"action": "AssetMeterExport"}` (current meter values), paginated. |

Responses of write endpoints are a JSON **array of strings** (messages). Errors follow the same shape.

## Status codes

| Code | Meaning |
|---|---|
| 200 | OK |
| 401 | Missing or wrong API key |
| 404 | Asset not found (Get, Patch) |
| 406 | Business validation error. The body lists the reasons. |
| 429 | Rate limit exceeded (50 requests / minute). `Retry-After` header in seconds. |
| 500 / 503 | Transient server error. Happens on ~3 % of the calls. |

## Notes from the vendor documentation

* `pageSize` maximum is 1000; a page beyond the last one returns an empty list.
* Write endpoints answer `200` with a list of messages; validation problems answer `406` with the
  list of reasons. Read the reasons, they are meant for integrators.
* Bodies are managed in the CMMS back office, not through the connector.
* Meters are cumulative counters. `value` is an integer.
* Names may contain quotes and non-ASCII characters.

Anything else about the API's behaviour is for you to discover by using it. The mock reproduces the
real system's behaviour, quirks included.

## Mock-only administration (not part of the real API, no key needed)

| Method | Path | Purpose |
|---|---|---|
| GET | `/_admin/health` | Liveness + known tenants |
| GET | `/_admin/{tenant}/state` | Full dump of the tenant. Handy to check your results after a run, **forbidden inside your pipeline** (production has no such thing). |
| GET | `/_admin/{tenant}/calls` | Counters since the last reset: `total`, `writes`, per endpoint, `429`, `5xx`. Use it to prove idempotency. |
| POST | `/_admin/{tenant}/reset` | Back to the seed. |
| POST | `/_admin/{tenant}/scenario/{name}` | Simulate an event: `technician_adds_system`, `technician_archives_system`, `key_user_renames_section`, `api_degraded`, `rate_limit_strict`. |

Behaviour can be tuned with environment variables in `docker-compose.yml` (`MOCK_RATE_LIMIT_PER_MINUTE`,
`MOCK_CHAOS_RATE`, `MOCK_LATENCY_MS`). Your pipeline must work with the defaults; you may relax them
while developing.
