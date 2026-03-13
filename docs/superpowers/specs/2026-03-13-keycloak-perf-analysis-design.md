# Design: Keycloak Authentication Flow Performance Analysis

**Date:** 2026-03-13
**Scope:** Compare multiple Keycloak versions (e.g. 26.5.3, 26.5.4, 26.5.5, nightly) on authentication flow admin operations across a configurable number of realms, with SQL query visibility via MariaDB general query log.

---

## 1. Goals

- Measure HTTP response times for authentication flow admin operations (create flow, add executions, update execution priority, delete flow) across N realms
- Capture and compare SQL queries fired against MariaDB **scoped to flow operations** for each Keycloak version
- Produce a structured comparison report (JSON + human-readable text) in `results/`

## 2. Architecture

A staged pipeline orchestrated by `run.py`. Each stage is an independent Python module callable directly for re-runs. The coordinator runs the full pipeline sequentially for each configured version.

```
keycloak-perf-test/
├── config.py                    # all tunables
├── run.py                       # coordinator
├── setup.py                     # docker-compose up + healthcheck + enable query log
├── realms.py                    # async create N realms
├── flows.py                     # async run flow operations per realm, record timings
├── teardown.py                  # dump query log + docker-compose down -v
├── report.py                    # parse all version results, produce comparison
├── auth.py                      # shared async token manager
├── docker-compose.yml           # image tag injected via KC_IMAGE env var
├── requirements.txt             # httpx (only external dependency)
└── results/
    ├── <version>/
    │   ├── realms.json
    │   ├── flows_timings.json
    │   ├── errors.json
    │   ├── query_log_offset.txt  # byte offset recorded at start of flows.py
    │   └── query.log             # trimmed: only queries during flows.py window
    ├── comparison.json
    └── comparison.txt
```

## 3. Configuration (`config.py`)

```python
VERSIONS = [
    ("26.5.3",  "quay.io/keycloak/keycloak:26.5.3"),
    ("26.5.4",  "quay.io/keycloak/keycloak:26.5.4"),
    ("26.5.5",  "quay.io/keycloak/keycloak:26.5.5"),
    ("nightly", "quay.io/keycloak/keycloak:nightly"),
]
REALM_COUNT = 30
CONCURRENCY = 20          # asyncio semaphore for HTTP requests
KEYCLOAK_URL = "http://localhost:8080"
KEYCLOAK_MANAGEMENT_URL = "http://localhost:9000"   # management/health port
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin"
RESULTS_DIR = "results"
DB_SERVICE = "db"
COMPOSE_CMD = "docker-compose"   # use "docker compose" for Docker Desktop
DOCKER_HOST = "unix:///..."      # Podman socket path (omit for standard Docker)
```

`KEYCLOAK_MANAGEMENT_URL` is used for the health check. In `start-dev` mode Keycloak exposes the management interface on port 9000 (separate from the main HTTP port 8080). Port 9000 must be mapped in `docker-compose.yml`.

`COMPOSE_CMD` and `DOCKER_HOST` support Podman environments where only `docker-compose` (v1 standalone) is available and the daemon socket differs from the Docker default.

## 4. Stage Details

### 4.1 `setup.py <version_tag> <image>`
- If `results/<version>/` already exists, abort with error: "Results for <version> already exist. Delete results/<version>/ before re-running."
- Sets `KC_IMAGE` env var and runs `{COMPOSE_CMD} up -d` with `DOCKER_HOST` injected
- Polls `GET {KEYCLOAK_MANAGEMENT_URL}/health/ready` until HTTP 200 (up to 5 minutes, 15s interval)
- Enables MariaDB general query log:
  ```bash
  {COMPOSE_CMD} exec -T db mariadb -uroot -pkeycloak -e \
    "SET GLOBAL general_log = 'ON'; SET GLOBAL general_log_file = '/var/lib/mysql/general.log';"
  ```
- Creates `results/<version>/` directory

### 4.2 `realms.py <version_tag>`
- Authenticates via `POST /realms/master/protocol/openid-connect/token` (`grant_type=password`)
- Uses `httpx.AsyncClient` + `asyncio.Semaphore(CONCURRENCY)` to `POST /admin/realms` for each realm named `perf-realm-{n:04d}`
- **Token refresh:** decodes JWT `exp` claim (base64 + json, no signature verification). An `asyncio.Lock` prevents concurrent refresh races. Falls back to full login if refresh token has expired or returns non-200.
- Saves realm names to `results/<version>/realms.json`
- Logs individual failures to `results/<version>/errors.json` without aborting
- Prints progress every 100 realms

### 4.3 `flows.py <version_tag>`
- Loads `results/<version>/realms.json`; uses same token refresh/lock logic as `realms.py`
- **Query log window:** records current MariaDB log file size via:
  ```bash
  {COMPOSE_CMD} exec -T db stat -c%s /var/lib/mysql/general.log
  ```
  Saves raw byte count to `results/<version>/query_log_offset.txt`.
- For each realm, runs this fixed sequence. Each HTTP call's wall-clock duration is recorded independently.
  1. `POST /admin/realms/{realm}/authentication/flows`
     - Body: `{"alias": "perf-test-flow", "description": "", "providerId": "basic-flow", "topLevel": true, "builtIn": false}`
     - Assert HTTP 201. Capture `flowId` UUID from `Location` response header (last path segment).
  2. `POST /admin/realms/{realm}/authentication/flows/perf-test-flow/executions/execution` × 3
     - Bodies: `{"provider": "auth-username-password-form"}`, `{"provider": "auth-otp-form"}`, `{"provider": "deny-access-authenticator"}`
     - Assert HTTP 201 for each.
  3. `GET /admin/realms/{realm}/authentication/flows/perf-test-flow/executions`
     - Use `response[-1]["id"]` (last element = lowest priority) as `executionId`.
  4. `POST /admin/realms/{realm}/authentication/executions/{executionId}/raise-priority`
     - No request body. Assert HTTP 204.
  5. `DELETE /admin/realms/{realm}/authentication/flows/{flowId}`
     - Assert HTTP 204.
- On exception, best-effort cleanup: delete the flow if `flowId` was already captured.
- Saves per-operation timings to `results/<version>/flows_timings.json`; merges with existing `errors.json`.

### 4.4 `teardown.py <version_tag>`
- Reads `results/<version>/query_log_offset.txt` for byte count `N`
- Extracts log from byte `N+1` onward (`tail -c +N` is 1-based, so `tail_start = offset + 1`):
  ```bash
  {COMPOSE_CMD} exec -T db tail -c +{tail_start} /var/lib/mysql/general.log
  ```
- Writes binary output to `results/<version>/query.log`
- Runs `{COMPOSE_CMD} down -v` to remove containers and volumes

### 4.5 `report.py`
- Reads `flows_timings.json` for all versions; requires at least 2 versions in config
- Computes per-operation statistics: mean, p50, p95, p99 (ms)
  - Groups `add_execution_1/2/3` into a single `add_execution` distribution
  - Uses `statistics.quantiles(data, n=100)` — returns 99 cut points; index 49 = p50, 94 = p95, 98 = p99
  - If fewer than 10 data points, omits p95/p99 (marked `"n/a"`); p50 falls back to `statistics.median`
  - `_load_timings` warns and returns empty dict if file is missing
- Reads `query.log` for all versions
- **Query log parsing:** MariaDB general log format (no timestamp in log-only mode) is `\t\t<thread_id> Query\t<sql>`. The thread ID and command are space-separated in the same tab field. Parser finds the field ending in `Query` and takes everything after the next tab as the SQL.
- Normalises SQL by stripping literals (quoted strings, bare numbers, UUIDs) → `?`
- Aggregates: total count, breakdown by statement type (SELECT/INSERT/UPDATE/DELETE), top-20 normalised patterns
- Report columns scale dynamically to the number of configured versions
- Writes `results/comparison.json` and `results/comparison.txt`; prints full query text (no truncation)

## 5. Data Flow

```
run.py
  │
  ├─ [version: 26.5.3]
  │    setup.py → realms.py → flows.py → teardown.py
  │
  ├─ [version: 26.5.4]
  │    setup.py → realms.py → flows.py → teardown.py
  │
  ├─ [version: 26.5.5]
  │    setup.py → realms.py → flows.py → teardown.py
  │
  ├─ [version: nightly]
  │    setup.py → realms.py → flows.py → teardown.py
  │
  └─ report.py  →  results/comparison.json + comparison.txt
```

**Error handling:** Per-realm/flow failures are caught, logged to `errors.json`, and do not abort the run. Stage-level failures exit with non-zero code; `run.py` catches `SystemExit` per version, appends to `_failed_versions`, and continues with remaining versions. Failed versions are excluded from the report warning.

## 6. Docker Compose Changes

- Keycloak image parameterised via `${KC_IMAGE:-quay.io/keycloak/keycloak:latest}`
- Port 9000 mapped to host (`"9000:9000"`) so the management health endpoint is reachable
- Healthcheck updated to use `http://localhost:9000/health/ready`

## 7. Dependencies

`requirements.txt` contains only `httpx`. No additional packages needed:
- Docker/Podman lifecycle via `subprocess` calling `{COMPOSE_CMD}` with `DOCKER_HOST` env injection
- Percentile math via `statistics.quantiles()` (stdlib, Python ≥ 3.11)
- JWT `exp` decoding via `base64` + `json` (stdlib)
- Query log parsing via `re` and `json` (stdlib)

## 8. Running

```bash
# Full comparison run (all configured versions, sequential)
python run.py

# Re-run a single stage (delete results/<version>/ first if it exists)
python setup.py 26.5.5 quay.io/keycloak/keycloak:26.5.5
python realms.py 26.5.5
python flows.py 26.5.5
python teardown.py 26.5.5

# Generate report from existing result files without re-running tests
python report.py
```
