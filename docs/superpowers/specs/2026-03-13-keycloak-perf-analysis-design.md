# Design: Keycloak Authentication Flow Performance Analysis

**Date:** 2026-03-13
**Scope:** Compare Keycloak 26.5.5 vs nightly build on authentication flow admin operations across 700 realms, with SQL query visibility via MariaDB general query log.

---

## 1. Goals

- Measure HTTP response times for authentication flow admin operations (create flow, add executions, update execution priority, delete flow) across 700 realms
- Capture and compare SQL queries fired against MariaDB **scoped to flow operations** for both Keycloak versions
- Produce a structured comparison report (JSON + human-readable text) in `results/`

## 2. Architecture

A staged pipeline orchestrated by `run.py`. Each stage is an independent Python module callable directly for re-runs. The coordinator runs the full pipeline sequentially: first for Keycloak 26.5.5, then for nightly.

```
keycloak-perf-test/
├── config.py                    # all tunables
├── run.py                       # coordinator
├── setup.py                     # docker-compose up + healthcheck + enable query log
├── realms.py                    # async create 700 realms
├── flows.py                     # async run flow operations per realm, record timings
├── teardown.py                  # dump query log + docker-compose down -v
├── report.py                    # parse both runs, produce comparison
├── docker-compose.yml           # image tag injected via KC_IMAGE env var
├── requirements.txt             # httpx (only external dependency)
└── results/
    ├── 26.5.5/
    │   ├── realms.json
    │   ├── flows_timings.json
    │   ├── errors.json
    │   ├── query_log_offset.txt  # byte offset recorded at start of flows.py
    │   └── query.log             # trimmed: only queries during flows.py window
    ├── nightly/
    │   ├── realms.json
    │   ├── flows_timings.json
    │   ├── errors.json
    │   ├── query_log_offset.txt
    │   └── query.log
    ├── comparison.json
    └── comparison.txt
```

## 3. Configuration (`config.py`)

```python
VERSIONS = [
    ("26.5.5",  "quay.io/keycloak/keycloak:26.5.5"),
    ("nightly", "quay.io/keycloak/keycloak:nightly"),
]
REALM_COUNT = 700
CONCURRENCY = 20          # asyncio semaphore for HTTP requests
KEYCLOAK_URL = "http://localhost:8080"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin"
RESULTS_DIR = "results"
# Use docker compose service names (not container names) for all exec/cp calls.
DB_SERVICE = "db"
```

## 4. Stage Details

### 4.1 `setup.py <version_tag> <image>`
- If `results/<version>/` already exists, abort with an error message: "Results for <version> already exist. Delete results/<version>/ before re-running."
- Sets `KC_IMAGE` env var and runs `docker compose up -d`
- The existing `docker-compose.yml` uses `start-dev` mode with `KC_HEALTH_ENABLED=true`; in `start-dev` mode the management interface shares port 8080, so the health endpoint is `GET http://localhost:8080/health/ready`
- Polls `GET /health/ready` on port 8080 until HTTP 200 is returned (up to 5 minutes, 15s interval)
- Enables MariaDB general query log via `docker compose exec` (uses service name, avoids fragile container-name assumptions):
  ```bash
  docker compose exec db mariadb -uroot -pkeycloak -e \
    "SET GLOBAL general_log = 'ON'; SET GLOBAL general_log_file = '/var/lib/mysql/general.log';"
  ```
- Creates `results/<version>/` directory

### 4.2 `realms.py <version_tag>`
- Authenticates with admin credentials via `POST /realms/master/protocol/openid-connect/token` (`grant_type=password`) to obtain an access + refresh token pair
- Uses `httpx.AsyncClient` + `asyncio.Semaphore(CONCURRENCY)` to `POST /admin/realms` for each of 700 realms named `perf-realm-{n:04d}`
- **Token refresh:** remaining validity is determined by base64-decoding the JWT payload and reading the `exp` claim (no signature verification needed — this is a local administrative tool). An `asyncio.Lock` is held during the refresh call so that only one coroutine refreshes at a time; all others wait on the lock. If the refresh token has also expired (Keycloak default lifetime: 1800s), re-authenticate from scratch with username/password.
- Saves realm names to `results/<version>/realms.json`
- Logs individual failures to `results/<version>/errors.json` without aborting

### 4.3 `flows.py <version_tag>`
- Loads `results/<version>/realms.json`; obtains and manages an admin token with the same refresh/lock logic as `realms.py`
- **Query log window:** immediately before starting flow operations, records the current MariaDB general log file size:
  ```bash
  docker compose exec db stat -c%s /var/lib/mysql/general.log
  ```
  Saves the raw integer byte count to `results/<version>/query_log_offset.txt`. `teardown.py` will use `offset + 1` as the `tail -c +N` argument to skip all pre-flow log content.
- For each realm, runs this fixed sequence. Each HTTP call's wall-clock duration is recorded independently.
  1. `POST /admin/realms/{realm}/authentication/flows`
     - Body: `{"alias": "perf-test-flow", "description": "", "providerId": "basic-flow", "topLevel": true, "builtIn": false}`
     - Assert HTTP 201. Capture `flowId` UUID from the `Location` response header (last path segment). If the header is absent or the response is not 201, log as a per-realm error and skip remaining steps for this realm.
  2. `POST /admin/realms/{realm}/authentication/flows/perf-test-flow/executions/execution` × 3
     - Bodies (sent in order): `{"provider": "auth-username-password-form"}`, `{"provider": "auth-otp-form"}`, `{"provider": "deny-access"}`
     - Assert HTTP 201 for each.
  3. `GET /admin/realms/{realm}/authentication/flows/perf-test-flow/executions`
     - Fetch execution list. Use `response[-1]["id"]` (last element = lowest priority = most recently added) as `executionId` for the next step. This ensures `raise-priority` performs a meaningful priority change rather than a no-op.
  4. `POST /admin/realms/{realm}/authentication/executions/{executionId}/raise-priority`
     - No request body. `executionId` is the `"id"` field (string UUID) from `response[-1]` in step 3.
     - Assert HTTP 204.
  5. `DELETE /admin/realms/{realm}/authentication/flows/{flowId}`
     - Uses the `flowId` UUID captured in step 1. Assert HTTP 204.
- Saves per-operation timings to `results/<version>/flows_timings.json`:
  ```json
  [
    {"realm": "perf-realm-0001", "operation": "create_flow",       "duration_ms": 42.3},
    {"realm": "perf-realm-0001", "operation": "add_execution_1",   "duration_ms": 18.1},
    {"realm": "perf-realm-0001", "operation": "add_execution_2",   "duration_ms": 17.4},
    {"realm": "perf-realm-0001", "operation": "add_execution_3",   "duration_ms": 16.9},
    {"realm": "perf-realm-0001", "operation": "get_executions",    "duration_ms": 12.0},
    {"realm": "perf-realm-0001", "operation": "raise_priority",    "duration_ms": 21.5},
    {"realm": "perf-realm-0001", "operation": "delete_flow",       "duration_ms": 30.1}
  ]
  ```

### 4.4 `teardown.py <version_tag>`
- Reads `results/<version>/query_log_offset.txt` to get the byte count `N` recorded at the start of `flows.py`
- Extracts only the portion of the log from byte `N+1` onward (skipping all pre-flow log content):
  ```bash
  docker compose exec -T db tail -c +$(( N + 1 )) /var/lib/mysql/general.log \
    > results/<version>/query.log
  ```
  (`tail -c +K` is 1-based: `+1` = start of file, so `+N+1` skips exactly the first `N` bytes.)
- Runs `docker compose down -v` to remove containers and volumes (ensures clean state for next version)

### 4.5 `report.py`
- Reads `flows_timings.json` for both versions
- Computes per-operation-type statistics: mean, p50, p95, p99 (in ms)
  - Groups `add_execution_1`, `add_execution_2`, `add_execution_3` into a single `add_execution` distribution (all individual timings pooled) for the summary table
  - Uses `statistics.quantiles(data, n=100)` which returns 99 cut points (indices 0–98). Index 49 = p50, index 94 = p95, index 98 = p99.
  - If fewer than 10 data points exist for an operation, omit p95/p99 (mark as `"n/a"`) since high-percentile estimates would be unreliable; p50 falls back to `statistics.median`.
- Reads `query.log` for both versions (already trimmed to flow-operation window)
- Normalises SQL by stripping literals (quoted strings, bare numbers, UUIDs) with regex → `?`
- Aggregates: total query count, breakdown by statement type (SELECT/INSERT/UPDATE/DELETE), top-20 most frequent normalised query patterns
- Writes `results/comparison.json` and `results/comparison.txt`

**Example `comparison.txt` layout:**
```
=== HTTP Timing Comparison (ms) ===
Operation          | 26.5.5 p50 | 26.5.5 p99 | nightly p50 | nightly p99
create_flow        |         38 |        120 |          35 |         110
add_execution      |         22 |         80 |          19 |          70
get_executions     |         10 |         40 |           9 |          35
raise_priority     |         25 |         90 |          23 |          82
delete_flow        |         30 |        105 |          28 |          98

=== SQL Query Count Comparison (flow operations window only) ===
Statement | 26.5.5  | nightly
SELECT    |  42,100 |  38,900
INSERT    |   7,700 |   7,200
UPDATE    |   3,500 |   3,100
DELETE    |   2,800 |   2,600

=== Top 20 Most Frequent Queries (26.5.5) ===
1. [12450x] SELECT * FROM AUTHENTICATION_FLOW WHERE REALM_ID = ?
...

=== Top 20 Most Frequent Queries (nightly) ===
1. [11200x] SELECT * FROM AUTHENTICATION_FLOW WHERE REALM_ID = ?
...
```

## 5. Data Flow

```
run.py
  │
  ├─ [version: 26.5.5]
  │    setup.py → realms.py → flows.py → teardown.py
  │
  ├─ [version: nightly]
  │    setup.py → realms.py → flows.py → teardown.py
  │
  └─ report.py  →  results/comparison.json + comparison.txt
```

**Token management:** `realms.py` and `flows.py` each obtain a token at startup. Remaining validity is determined by decoding the JWT `exp` claim (base64 + json, stdlib only). An `asyncio.Lock` prevents concurrent refresh races. If the refresh token expires, re-authentication with username/password is performed automatically.

**Error handling:** Per-realm/flow failures are caught, logged to `errors.json`, and do not abort the run. Stage-level failures (e.g. Keycloak never becomes healthy, `results/<version>/` already exists) exit with a non-zero code and `run.py` halts the pipeline.

## 6. Docker Compose Changes

The existing `docker-compose.yml` is updated to:
- Parameterise the Keycloak image via `${KC_IMAGE:-quay.io/keycloak/keycloak:latest}` (replaces the hardcoded `image:` value)

All other values remain unchanged — `KC_HEALTH_ENABLED: "true"` is already present, `start-dev` mode keeps health on port 8080, and all Docker interactions use `docker compose exec <service>` (not hardcoded container names).

## 7. Dependencies

`requirements.txt` contains only `httpx`. No additional packages are needed:
- Docker lifecycle managed via `subprocess` calling `docker compose` / `docker compose exec`
- Percentile math via `statistics.quantiles()` (stdlib, Python ≥ 3.8)
- JWT `exp` decoding via `base64` + `json` (stdlib)
- Query log parsing via `re` and `json` (stdlib)

## 8. Running

```bash
# Full comparison run (both versions, sequential)
python run.py

# Re-run a single stage independently (e.g. if flows.py failed mid-run)
# Note: setup.py aborts if results/<version>/ already exists; delete it first
python flows.py 26.5.5

# Generate report from existing result files without re-running tests
python report.py
```
