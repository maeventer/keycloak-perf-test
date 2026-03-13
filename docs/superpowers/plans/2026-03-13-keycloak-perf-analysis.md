# Keycloak Performance Analysis Implementation Plan

**Goal:** Build a staged Python pipeline that benchmarks multiple Keycloak versions across N realms using authentication flow admin operations, and compares SQL query patterns via MariaDB general query log.

**Architecture:** Seven focused Python modules (`config`, `auth`, `setup`, `realms`, `flows`, `teardown`, `report`, `run`) each with a single responsibility, orchestrated sequentially by `run.py`.

**Tech Stack:** Python 3.11+, httpx (async HTTP), asyncio, subprocess (Docker/Podman), statistics/re/json/base64 (stdlib)

---

## Chunk 1: Foundation — config, docker-compose, requirements

### Task 1: Update `docker-compose.yml`

**Files:**
- Modify: `docker-compose.yml`

- [x] Parameterise Keycloak image via `${KC_IMAGE:-quay.io/keycloak/keycloak:latest}`
- [x] Map port 9000 to host (`"9000:9000"`) for management/health endpoint
- [x] Update healthcheck to use `http://localhost:9000/health/ready`

---

### Task 2: Create `config.py`

**Files:**
- Create: `config.py`

- [x] Define `VERSIONS`, `REALM_COUNT`, `CONCURRENCY`, `KEYCLOAK_URL`, `KEYCLOAK_MANAGEMENT_URL`
- [x] Add `ADMIN_USER`, `ADMIN_PASSWORD`, `RESULTS_DIR`, `DB_SERVICE`
- [x] Add `COMPOSE_CMD = "docker-compose"` and `DOCKER_HOST` for Podman support

Key values:
```python
KEYCLOAK_URL = "http://localhost:8080"
KEYCLOAK_MANAGEMENT_URL = "http://localhost:9000"
COMPOSE_CMD = "docker-compose"
DOCKER_HOST = "unix:///..."  # Podman socket path
```

---

## Chunk 2: Token management

### Task 3: Create `auth.py`

**Files:**
- Create: `auth.py`

- [x] `TokenManager` class with `get_token(client)`, `_do_login`, `_do_refresh`, `_store`
- [x] `asyncio.Lock` prevents concurrent refresh races (double-checked locking pattern)
- [x] `_do_refresh` falls back to full login on any non-200 response (not just >= 400)
- [x] `_jwt_exp` decodes JWT `exp` claim via base64 + json (no signature verification)

---

## Chunk 3: `setup.py`

### Task 4: Create `setup.py`

**Files:**
- Create: `setup.py`

- [x] `_compose_env(**extra)` helper injects `DOCKER_HOST` into all subprocess calls
- [x] `start_stack` aborts if `results/<version>/` already exists
- [x] Runs `{COMPOSE_CMD} up -d` with `KC_IMAGE` env var
- [x] `_wait_for_keycloak` polls `{KEYCLOAK_MANAGEMENT_URL}/health/ready` (port 9000, not 8080)
- [x] `_enable_query_log` runs `mariadb -uroot -pkeycloak -e "SET GLOBAL general_log..."`

---

## Chunk 4: `realms.py`

### Task 5: Create `realms.py`

**Files:**
- Create: `realms.py`

- [x] Async create `REALM_COUNT` realms named `perf-realm-{n:04d}` via `POST /admin/realms`
- [x] `asyncio.Semaphore(CONCURRENCY)` limits concurrent requests
- [x] Uses `TokenManager` from `auth.py`; `version_tag` parameter removed from `_create_realm`
- [x] Saves `realms.json` and `errors.json` to `results/<version>/`
- [x] Progress logged every 100 realms (and at completion)

---

## Chunk 5: `flows.py`

### Task 6: Create `flows.py`

**Files:**
- Create: `flows.py`

- [x] `PROVIDERS = ["auth-username-password-form", "auth-otp-form", "deny-access-authenticator"]`
  - Note: `deny-access-authenticator` (not `deny-access`) is the correct provider ID in Keycloak 26.x+
- [x] `_record_query_log_offset` uses `stat -c%s` via `{COMPOSE_CMD} exec -T` with `DOCKER_HOST`
- [x] `flow_id = None` sentinel set before `try` block; cleanup called in `except` if not None
- [x] Flow sequence: create → add 3 executions → GET executions → raise-priority → DELETE
  - `executionId` = `response[-1]["id"]` (last = lowest priority = meaningful raise)
  - raise-priority URL: `{base}/executions/{executionId}/raise-priority`
- [x] Timings merged into `flows_timings.json`; errors merged into existing `errors.json`

---

## Chunk 6: `teardown.py`

### Task 7: Create `teardown.py`

**Files:**
- Create: `teardown.py`

- [x] `_compose_env()` helper injects `DOCKER_HOST`
- [x] Reads `query_log_offset.txt`; computes `tail_start = offset + 1` (1-based)
- [x] Extracts log via `{COMPOSE_CMD} exec -T db tail -c +{tail_start} /var/lib/mysql/general.log`
- [x] Writes binary output to `results/<version>/query.log`
- [x] Runs `{COMPOSE_CMD} down -v`

---

## Chunk 7: `report.py`

### Task 8: Create `report.py`

**Files:**
- Create: `report.py`

- [x] Requires at least 2 versions in config (raises `SystemExit` otherwise)
- [x] `_load_timings` warns and returns `{}` if file missing; merges `add_execution_1/2/3` → `add_execution`
- [x] `_percentiles` uses `statistics.quantiles(data, n=100)`; falls back for < 10 data points
- [x] **Query log parser:** MariaDB general log format is `\t\t<thread_id> Query\t<sql>` (no timestamp).
  Thread ID and command are space-separated in the same tab field. Parser finds the field whose
  last whitespace-token is `"Query"` and takes the next tab-field as SQL.
- [x] Table columns scale dynamically to number of versions (not hardcoded to 2)
- [x] Full query text output (no truncation)
- [x] Writes `results/comparison.json` and `results/comparison.txt`

---

## Chunk 8: `run.py`

### Task 9: Create `run.py`

**Files:**
- Create: `run.py`

- [x] `_failed_versions: list[str]` tracks per-version failures
- [x] `run_version` wraps pipeline in `try/except SystemExit` — failed version is logged and skipped, remaining versions continue
- [x] Prints warning listing failed versions before report generation
- [x] Calls `generate_report()` after all versions complete

---

## Chunk 9: Supporting files

### Task 10: Finalise project files

**Files:**
- Modify: `requirements.txt`, `CLAUDE.md`, `docker-compose.yml`
- Create: `README.md`, `LICENSE`

- [x] `requirements.txt` contains only `httpx`
- [x] `CLAUDE.md` updated with benchmark run instructions
- [x] `README.md` describes usage, intent, configuration, example findings table, output files
- [x] `LICENSE` — Apache 2.0, copyright 2026 Markus Eberl

---

## Chunk 10: Smoke test and full run

### Task 11: Verify pipeline end-to-end

- [x] Smoke test with `REALM_COUNT = 2`, single version — confirmed 14 timing records, 0 errors
- [x] Fixed health endpoint: `KEYCLOAK_MANAGEMENT_URL` pointing to port 9000
- [x] Fixed provider ID: `deny-access-authenticator` (was `deny-access`)
- [x] Full benchmark runs: 10, 20, 30 realms across 26.5.3, 26.5.4, 26.5.5, nightly
- [x] Combined SQL count results documented in `results/combined-sql-counts.md`

**Key findings from benchmark:**
- nightly shows O(n²) SQL growth driven by `SELECT ... FROM KEYCLOAK_ROLE JOIN COMPOSITE_ROLE WHERE COMPOSITE=? LIMIT ?` — 84,000 hits at 30 realms vs. 0 in any stable release
- HTTP latency for add_execution/get_executions/delete_flow is 14–32x slower in nightly at 30 realms
- Regression introduced between 26.5.5 and nightly; all stable versions (26.5.3–26.5.5) behave comparably
