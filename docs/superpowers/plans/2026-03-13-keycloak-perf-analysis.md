# Keycloak Performance Analysis Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a staged Python pipeline that benchmarks Keycloak 26.5.5 vs nightly across 700 realms using authentication flow admin operations, and compares SQL query patterns via MariaDB general query log.

**Architecture:** Seven focused Python modules (`config`, `setup`, `realms`, `flows`, `teardown`, `report`, `run`) each with a single responsibility, orchestrated sequentially by `run.py`. No test framework — the pipeline is integration-only and is verified by running it against a live Docker stack.

**Tech Stack:** Python 3.8+, httpx (async HTTP), asyncio, subprocess (Docker), statistics/re/json/base64 (stdlib)

---

## Chunk 1: Foundation — config, docker-compose, requirements

### Task 1: Update `docker-compose.yml` to parameterise the Keycloak image

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Replace hardcoded Keycloak image tag**

Open `docker-compose.yml`. Change line:
```yaml
    image: quay.io/keycloak/keycloak:latest
```
to:
```yaml
    image: ${KC_IMAGE:-quay.io/keycloak/keycloak:latest}
```
Leave all other lines unchanged.

- [ ] **Step 2: Verify the compose file is valid**

```bash
docker compose config --quiet
```
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git init  # only if repo has no commits yet
git add docker-compose.yml
git commit -m "feat: parameterise keycloak image via KC_IMAGE env var"
```

---

### Task 2: Create `config.py`

**Files:**
- Create: `config.py`

- [ ] **Step 1: Write `config.py`**

```python
import os

VERSIONS = [
    ("26.5.5", "quay.io/keycloak/keycloak:26.5.5"),
    ("nightly", "quay.io/keycloak/keycloak:nightly"),
]
REALM_COUNT = 700
CONCURRENCY = 20
KEYCLOAK_URL = "http://localhost:8080"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin"
RESULTS_DIR = "results"
DB_SERVICE = "db"
```

- [ ] **Step 2: Verify it imports cleanly**

```bash
python -c "import config; print(config.REALM_COUNT)"
```
Expected output: `700`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add config.py with version, realm, and Docker settings"
```

---

## Chunk 2: `setup.py` — bring up Docker stack and enable query log

### Task 3: Create `setup.py`

**Files:**
- Create: `setup.py`

- [ ] **Step 1: Write `setup.py`**

```python
#!/usr/bin/env python3
"""Stage 1: Start Docker stack for a given Keycloak version and enable query log."""

import os
import sys
import time
import subprocess
import httpx
from pathlib import Path
import config


def _run(cmd, **kwargs):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        print(f"ERROR running: {cmd}", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def start_stack(version_tag: str, image: str):
    results_dir = Path(config.RESULTS_DIR) / version_tag
    if results_dir.exists():
        print(
            f"ERROR: Results for {version_tag} already exist. "
            f"Delete {results_dir}/ before re-running.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[setup] Starting stack for {version_tag} ({image})")
    env = {**os.environ, "KC_IMAGE": image}
    result = subprocess.run(
        "docker compose up -d",
        shell=True,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        print(f"ERROR: docker compose up failed\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    _wait_for_keycloak()
    _enable_query_log()

    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"[setup] Stack ready. Results dir: {results_dir}")


def _wait_for_keycloak(timeout_s: int = 300, interval_s: int = 15):
    url = f"{config.KEYCLOAK_URL}/health/ready"
    deadline = time.time() + timeout_s
    print(f"[setup] Waiting for Keycloak at {url} (up to {timeout_s}s)...")
    while time.time() < deadline:
        try:
            resp = httpx.get(url, timeout=5)
            if resp.status_code == 200:
                print("[setup] Keycloak is healthy.")
                return
        except Exception:
            pass
        time.sleep(interval_s)
    print(f"ERROR: Keycloak did not become healthy within {timeout_s}s", file=sys.stderr)
    sys.exit(1)


def _enable_query_log():
    print("[setup] Enabling MariaDB general query log...")
    sql = (
        "SET GLOBAL general_log = 'ON'; "
        "SET GLOBAL general_log_file = '/var/lib/mysql/general.log';"
    )
    _run(
        f'docker compose exec -T {config.DB_SERVICE} '
        f'mariadb -uroot -pkeycloak -e "{sql}"'
    )
    print("[setup] Query log enabled.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python setup.py <version_tag> <image>", file=sys.stderr)
        sys.exit(1)
    start_stack(sys.argv[1], sys.argv[2])
```

- [ ] **Step 2: Check syntax**

```bash
python -m py_compile setup.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add setup.py
git commit -m "feat: add setup.py — docker-compose up, health wait, enable query log"
```

---

## Chunk 3: Token management utility

### Task 4: Create `auth.py` — shared token management

Both `realms.py` and `flows.py` need token management with the same logic. Extract it into a single module.

**Files:**
- Create: `auth.py`

- [ ] **Step 1: Write `auth.py`**

```python
"""Shared Keycloak admin token management with asyncio-safe refresh."""

import asyncio
import base64
import json
import time
import httpx
import config


class TokenManager:
    """Obtains and refreshes a Keycloak admin access token.

    Safe for concurrent async use: an asyncio.Lock prevents multiple
    coroutines from refreshing simultaneously.
    """

    def __init__(self):
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._exp: float = 0.0
        self._refresh_exp: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid access token, refreshing if needed."""
        if time.time() < self._exp - 30:
            return self._access_token
        async with self._lock:
            # Re-check after acquiring lock (another coroutine may have refreshed)
            if time.time() < self._exp - 30:
                return self._access_token
            if self._refresh_token and time.time() < self._refresh_exp - 30:
                await self._do_refresh(client)
            else:
                await self._do_login(client)
        return self._access_token

    async def _do_login(self, client: httpx.AsyncClient):
        url = f"{config.KEYCLOAK_URL}/realms/master/protocol/openid-connect/token"
        resp = await client.post(url, data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": config.ADMIN_USER,
            "password": config.ADMIN_PASSWORD,
        })
        resp.raise_for_status()
        self._store(resp.json())

    async def _do_refresh(self, client: httpx.AsyncClient):
        url = f"{config.KEYCLOAK_URL}/realms/master/protocol/openid-connect/token"
        resp = await client.post(url, data={
            "grant_type": "refresh_token",
            "client_id": "admin-cli",
            "refresh_token": self._refresh_token,
        })
        if resp.status_code >= 400:
            # Refresh token expired; fall back to full login
            await self._do_login(client)
            return
        self._store(resp.json())

    def _store(self, data: dict):
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", "")
        self._exp = _jwt_exp(self._access_token)
        self._refresh_exp = _jwt_exp(self._refresh_token) if self._refresh_token else 0.0


def _jwt_exp(token: str) -> float:
    """Decode the exp claim from a JWT without verifying the signature."""
    try:
        payload_b64 = token.split(".")[1]
        # Add padding so base64 decodes correctly
        padding = 4 - len(payload_b64) % 4
        payload_b64 += "=" * (padding % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload["exp"])
    except Exception:
        # If decoding fails, treat as already expired
        return 0.0
```

- [ ] **Step 2: Check syntax**

```bash
python -m py_compile auth.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add auth.py
git commit -m "feat: add auth.py — async token manager with JWT exp decode and refresh lock"
```

---

## Chunk 4: `realms.py` — create 700 realms

### Task 5: Create `realms.py`

**Files:**
- Create: `realms.py`

- [ ] **Step 1: Write `realms.py`**

```python
#!/usr/bin/env python3
"""Stage 2: Create REALM_COUNT realms asynchronously."""

import asyncio
import json
import sys
from pathlib import Path
import httpx
import config
from auth import TokenManager


async def create_realms(version_tag: str):
    results_dir = Path(config.RESULTS_DIR) / version_tag
    errors = []
    realm_names = []

    sem = asyncio.Semaphore(config.CONCURRENCY)
    token_mgr = TokenManager()

    async with httpx.AsyncClient(timeout=30) as client:
        # Warm up token before spawning tasks
        await token_mgr.get_token(client)

        tasks = [
            _create_realm(client, token_mgr, sem, version_tag, n, realm_names, errors)
            for n in range(config.REALM_COUNT)
        ]
        await asyncio.gather(*tasks)

    realm_names.sort()
    (results_dir / "realms.json").write_text(json.dumps(realm_names, indent=2))
    (results_dir / "errors.json").write_text(json.dumps(errors, indent=2))

    print(
        f"[realms] Created {len(realm_names)}/{config.REALM_COUNT} realms. "
        f"Errors: {len(errors)}"
    )


async def _create_realm(
    client: httpx.AsyncClient,
    token_mgr: TokenManager,
    sem: asyncio.Semaphore,
    version_tag: str,
    n: int,
    realm_names: list,
    errors: list,
):
    realm = f"perf-realm-{n:04d}"
    async with sem:
        try:
            token = await token_mgr.get_token(client)
            resp = await client.post(
                f"{config.KEYCLOAK_URL}/admin/realms",
                json={"realm": realm, "enabled": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code in (201, 409):
                # 409 = already exists (idempotent re-run)
                realm_names.append(realm)
            else:
                errors.append({"realm": realm, "status": resp.status_code, "body": resp.text})
        except Exception as exc:
            errors.append({"realm": realm, "error": str(exc)})

    if (n + 1) % 100 == 0 or n + 1 == config.REALM_COUNT:
        print(f"[realms] Progress: {n + 1}/{config.REALM_COUNT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python realms.py <version_tag>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(create_realms(sys.argv[1]))
```

- [ ] **Step 2: Check syntax**

```bash
python -m py_compile realms.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add realms.py
git commit -m "feat: add realms.py — async create 700 realms with token refresh and error logging"
```

---

## Chunk 5: `flows.py` — run auth flow operations and record timings

### Task 6: Create `flows.py`

**Files:**
- Create: `flows.py`

- [ ] **Step 1: Write `flows.py`**

```python
#!/usr/bin/env python3
"""Stage 3: Run auth flow operations per realm and record timings + query log offset."""

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
import httpx
import config
from auth import TokenManager

PROVIDERS = ["auth-username-password-form", "auth-otp-form", "deny-access"]


def _record_query_log_offset(version_tag: str):
    """Record current MariaDB general log size before flow operations start."""
    result = subprocess.run(
        f"docker compose exec -T {config.DB_SERVICE} stat -c%s /var/lib/mysql/general.log",
        shell=True,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Log may not exist yet if no queries fired; treat as offset 0
        offset = 0
    else:
        offset = int(result.stdout.strip())
    offset_file = Path(config.RESULTS_DIR) / version_tag / "query_log_offset.txt"
    offset_file.write_text(str(offset))
    print(f"[flows] Query log offset recorded: {offset} bytes")


async def run_flows(version_tag: str):
    results_dir = Path(config.RESULTS_DIR) / version_tag
    realms: list[str] = json.loads((results_dir / "realms.json").read_text())

    _record_query_log_offset(version_tag)

    timings = []
    errors = []
    sem = asyncio.Semaphore(config.CONCURRENCY)
    token_mgr = TokenManager()

    async with httpx.AsyncClient(timeout=30) as client:
        await token_mgr.get_token(client)
        tasks = [
            _flow_sequence(client, token_mgr, sem, realm, timings, errors)
            for realm in realms
        ]
        await asyncio.gather(*tasks)

    (results_dir / "flows_timings.json").write_text(json.dumps(timings, indent=2))
    # Merge with existing errors.json if present
    errors_file = results_dir / "errors.json"
    existing_errors = json.loads(errors_file.read_text()) if errors_file.exists() else []
    errors_file.write_text(json.dumps(existing_errors + errors, indent=2))

    print(
        f"[flows] Completed {len(realms)} realms. "
        f"Timing records: {len(timings)}. Flow errors: {len(errors)}"
    )


async def _timed(client, method, url, token, **kwargs):
    """Execute an HTTP call and return (response, duration_ms)."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    t0 = time.perf_counter()
    resp = await getattr(client, method)(url, headers=headers, **kwargs)
    duration_ms = (time.perf_counter() - t0) * 1000
    return resp, round(duration_ms, 2)


async def _flow_sequence(
    client: httpx.AsyncClient,
    token_mgr: TokenManager,
    sem: asyncio.Semaphore,
    realm: str,
    timings: list,
    errors: list,
):
    base = f"{config.KEYCLOAK_URL}/admin/realms/{realm}/authentication"

    async with sem:
        try:
            token = await token_mgr.get_token(client)

            # Step 1: create flow
            resp, dur = await _timed(
                client, "post", f"{base}/flows",
                token,
                json={
                    "alias": "perf-test-flow",
                    "description": "",
                    "providerId": "basic-flow",
                    "topLevel": True,
                    "builtIn": False,
                },
            )
            timings.append({"realm": realm, "operation": "create_flow", "duration_ms": dur})
            if resp.status_code != 201 or "location" not in resp.headers:
                errors.append({"realm": realm, "operation": "create_flow",
                               "status": resp.status_code, "body": resp.text})
                return
            flow_id = resp.headers["location"].rstrip("/").split("/")[-1]

            # Step 2: add 3 executions
            for i, provider in enumerate(PROVIDERS, start=1):
                token = await token_mgr.get_token(client)
                resp, dur = await _timed(
                    client, "post",
                    f"{base}/flows/perf-test-flow/executions/execution",
                    token,
                    json={"provider": provider},
                )
                timings.append({"realm": realm, "operation": f"add_execution_{i}",
                                "duration_ms": dur})
                if resp.status_code != 201:
                    errors.append({"realm": realm, "operation": f"add_execution_{i}",
                                   "status": resp.status_code, "body": resp.text})

            # Step 3: get executions
            token = await token_mgr.get_token(client)
            resp, dur = await _timed(
                client, "get",
                f"{base}/flows/perf-test-flow/executions",
                token,
            )
            timings.append({"realm": realm, "operation": "get_executions", "duration_ms": dur})
            if resp.status_code != 200:
                errors.append({"realm": realm, "operation": "get_executions",
                               "status": resp.status_code, "body": resp.text})
                # Can't raise priority without an execution ID; skip remaining steps
                await _delete_flow(client, token_mgr, base, realm, flow_id, timings, errors)
                return
            executions = resp.json()
            execution_id = executions[-1]["id"]  # last = lowest priority = meaningful raise

            # Step 4: raise priority
            token = await token_mgr.get_token(client)
            resp, dur = await _timed(
                client, "post",
                f"{base}/executions/{execution_id}/raise-priority",
                token,
            )
            timings.append({"realm": realm, "operation": "raise_priority", "duration_ms": dur})
            if resp.status_code != 204:
                errors.append({"realm": realm, "operation": "raise_priority",
                               "status": resp.status_code, "body": resp.text})

            # Step 5: delete flow
            token = await token_mgr.get_token(client)
            resp, dur = await _timed(
                client, "delete",
                f"{base}/flows/{flow_id}",
                token,
            )
            timings.append({"realm": realm, "operation": "delete_flow", "duration_ms": dur})
            if resp.status_code != 204:
                errors.append({"realm": realm, "operation": "delete_flow",
                               "status": resp.status_code, "body": resp.text})

        except Exception as exc:
            errors.append({"realm": realm, "operation": "exception", "error": str(exc)})


async def _delete_flow(client, token_mgr, base, realm, flow_id, timings, errors):
    """Best-effort cleanup when an earlier step failed."""
    try:
        token = await token_mgr.get_token(client)
        resp, dur = await _timed(client, "delete", f"{base}/flows/{flow_id}", token)
        timings.append({"realm": realm, "operation": "delete_flow", "duration_ms": dur})
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python flows.py <version_tag>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(run_flows(sys.argv[1]))
```

- [ ] **Step 2: Check syntax**

```bash
python -m py_compile flows.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add flows.py
git commit -m "feat: add flows.py — async auth flow operations with timing and query log offset"
```

---

## Chunk 6: `teardown.py` — extract query log and stop stack

### Task 7: Create `teardown.py`

**Files:**
- Create: `teardown.py`

- [ ] **Step 1: Write `teardown.py`**

```python
#!/usr/bin/env python3
"""Stage 4: Extract trimmed query log and stop Docker stack."""

import subprocess
import sys
from pathlib import Path
import config


def _run(cmd, check=True):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"ERROR running: {cmd}\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def teardown(version_tag: str):
    results_dir = Path(config.RESULTS_DIR) / version_tag
    offset_file = results_dir / "query_log_offset.txt"

    if offset_file.exists():
        offset = int(offset_file.read_text().strip())
        tail_start = offset + 1  # tail -c +N is 1-based; +1 skips the first `offset` bytes
        query_log_dest = results_dir / "query.log"
        print(f"[teardown] Extracting query log from byte offset {offset}...")
        result = subprocess.run(
            f"docker compose exec -T {config.DB_SERVICE} "
            f"tail -c +{tail_start} /var/lib/mysql/general.log",
            shell=True,
            capture_output=True,
        )
        if result.returncode == 0:
            query_log_dest.write_bytes(result.stdout)
            print(f"[teardown] Query log saved to {query_log_dest} "
                  f"({len(result.stdout):,} bytes)")
        else:
            print(
                f"[teardown] WARNING: Could not extract query log: "
                f"{result.stderr.decode()}",
                file=sys.stderr,
            )
    else:
        print("[teardown] WARNING: No query_log_offset.txt found; skipping log extraction.",
              file=sys.stderr)

    print("[teardown] Stopping Docker stack and removing volumes...")
    _run("docker compose down -v")
    print("[teardown] Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python teardown.py <version_tag>", file=sys.stderr)
        sys.exit(1)
    teardown(sys.argv[1])
```

- [ ] **Step 2: Check syntax**

```bash
python -m py_compile teardown.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add teardown.py
git commit -m "feat: add teardown.py — extract trimmed query log and docker-compose down -v"
```

---

## Chunk 7: `report.py` — parse results and produce comparison

### Task 8: Create `report.py`

**Files:**
- Create: `report.py`

- [ ] **Step 1: Write `report.py`**

```python
#!/usr/bin/env python3
"""Stage 5: Parse both version results and produce a comparison report."""

import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
import config

TOP_N_QUERIES = 20

# Regex to normalise SQL literals to ?
_LITERAL_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'"          # single-quoted strings
    r"|\"(?:[^\"\\]|\\.)*\""      # double-quoted strings
    r"|(?<!['\w])\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # UUIDs
    r"|(?<!['\w.-])\b\d+(?:\.\d+)?\b(?!['\w])"  # bare numbers
)

# Operations to show in the timing table (add_execution_* are merged)
_OPS_ORDER = [
    "create_flow",
    "add_execution",
    "get_executions",
    "raise_priority",
    "delete_flow",
]


def _percentiles(data: list[float]) -> dict:
    if len(data) < 10:
        med = statistics.median(data) if data else None
        return {"mean": round(statistics.mean(data), 1) if data else None,
                "p50": round(med, 1) if med is not None else None,
                "p95": "n/a", "p99": "n/a"}
    qs = statistics.quantiles(data, n=100)
    return {
        "mean": round(statistics.mean(data), 1),
        "p50":  round(qs[49], 1),
        "p95":  round(qs[94], 1),
        "p99":  round(qs[98], 1),
    }


def _load_timings(version_tag: str) -> dict[str, list[float]]:
    """Load timings and merge add_execution_1/2/3 into add_execution."""
    path = Path(config.RESULTS_DIR) / version_tag / "flows_timings.json"
    raw = json.loads(path.read_text())
    grouped: dict[str, list[float]] = defaultdict(list)
    for entry in raw:
        op = entry["operation"]
        if op.startswith("add_execution_"):
            op = "add_execution"
        grouped[op].append(entry["duration_ms"])
    return grouped


def _parse_query_log(version_tag: str) -> dict:
    """Parse MariaDB general log, normalise queries, count by type and pattern."""
    path = Path(config.RESULTS_DIR) / version_tag / "query.log"
    if not path.exists():
        return {"total": 0, "by_type": {}, "top_patterns": []}

    type_counts: Counter = Counter()
    pattern_counts: Counter = Counter()

    for line in path.read_text(errors="replace").splitlines():
        # General log format: <timestamp>\t<thread_id>\tQuery\t<sql>
        # or: <timestamp>\t<thread_id>\t<command>\t<argument>
        parts = line.split("\t", 3)
        if len(parts) < 4 or parts[2].strip() != "Query":
            continue
        sql = parts[3].strip()
        stmt_type = sql.split()[0].upper() if sql else ""
        if stmt_type in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            type_counts[stmt_type] += 1
        normalised = _LITERAL_RE.sub("?", sql)
        pattern_counts[normalised] += 1

    total = sum(type_counts.values())
    top_patterns = [
        {"count": cnt, "query": pat}
        for pat, cnt in pattern_counts.most_common(TOP_N_QUERIES)
    ]
    return {"total": total, "by_type": dict(type_counts), "top_patterns": top_patterns}


def generate_report():
    versions = [v[0] for v in config.VERSIONS]
    results_dir = Path(config.RESULTS_DIR)

    timing_stats = {}
    sql_stats = {}
    for v in versions:
        timings = _load_timings(v)
        timing_stats[v] = {op: _percentiles(timings.get(op, [])) for op in _OPS_ORDER}
        sql_stats[v] = _parse_query_log(v)

    # --- Build comparison.json ---
    comparison = {"timing": timing_stats, "sql": sql_stats}
    (results_dir / "comparison.json").write_text(json.dumps(comparison, indent=2))

    # --- Build comparison.txt ---
    lines = []

    lines.append("=== HTTP Timing Comparison (ms) ===")
    v0, v1 = versions[0], versions[1]
    header = f"{'Operation':<20} | {v0+' p50':>12} | {v0+' p99':>12} | {v1+' p50':>13} | {v1+' p99':>13}"
    lines.append(header)
    lines.append("-" * len(header))
    for op in _OPS_ORDER:
        s0 = timing_stats[v0][op]
        s1 = timing_stats[v1][op]
        lines.append(
            f"{op:<20} | {str(s0['p50']):>12} | {str(s0['p99']):>12} "
            f"| {str(s1['p50']):>13} | {str(s1['p99']):>13}"
        )

    lines.append("")
    lines.append("=== SQL Query Count Comparison (flow operations window only) ===")
    lines.append(f"{'Statement':<10} | {v0:>10} | {v1:>10}")
    lines.append("-" * 36)
    for stmt in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        c0 = sql_stats[v0]["by_type"].get(stmt, 0)
        c1 = sql_stats[v1]["by_type"].get(stmt, 0)
        lines.append(f"{stmt:<10} | {c0:>10,} | {c1:>10,}")
    lines.append(f"{'TOTAL':<10} | {sql_stats[v0]['total']:>10,} | {sql_stats[v1]['total']:>10,}")

    for v in versions:
        lines.append("")
        lines.append(f"=== Top {TOP_N_QUERIES} Most Frequent Queries ({v}) ===")
        for i, entry in enumerate(sql_stats[v]["top_patterns"], start=1):
            lines.append(f"{i:>3}. [{entry['count']:>7,}x] {entry['query'][:120]}")

    txt = "\n".join(lines) + "\n"
    (results_dir / "comparison.txt").write_text(txt)
    print(txt)
    print(f"[report] Saved comparison.json and comparison.txt to {results_dir}/")


if __name__ == "__main__":
    generate_report()
```

- [ ] **Step 2: Check syntax**

```bash
python -m py_compile report.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add report.py
git commit -m "feat: add report.py — timing percentiles, SQL query analysis, comparison output"
```

---

## Chunk 8: `run.py` — coordinator

### Task 9: Create `run.py`

**Files:**
- Create: `run.py`

- [ ] **Step 1: Write `run.py`**

```python
#!/usr/bin/env python3
"""Coordinator: run the full pipeline for each Keycloak version, then generate report."""

import sys
import config
from setup import start_stack
from realms import create_realms
from flows import run_flows
from teardown import teardown
from report import generate_report
import asyncio


def run_version(version_tag: str, image: str):
    print(f"\n{'='*60}")
    print(f"  Running pipeline for Keycloak {version_tag}")
    print(f"{'='*60}\n")

    print(f"--- Stage 1: setup ---")
    start_stack(version_tag, image)

    print(f"\n--- Stage 2: create realms ---")
    asyncio.run(create_realms(version_tag))

    print(f"\n--- Stage 3: flow operations ---")
    asyncio.run(run_flows(version_tag))

    print(f"\n--- Stage 4: teardown ---")
    teardown(version_tag)


def main():
    for version_tag, image in config.VERSIONS:
        run_version(version_tag, image)

    print(f"\n{'='*60}")
    print("  Generating comparison report")
    print(f"{'='*60}\n")
    generate_report()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Check syntax**

```bash
python -m py_compile run.py && echo "OK"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add run.py
git commit -m "feat: add run.py — pipeline coordinator for sequential version comparison"
```

---

## Chunk 9: Update CLAUDE.md and requirements.txt

### Task 10: Finalise project files

**Files:**
- Modify: `requirements.txt`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Confirm requirements.txt is correct**

`requirements.txt` should contain only:
```
httpx
```
No changes needed if it already reads `httpx`.

- [ ] **Step 2: Update CLAUDE.md with run instructions**

Add the following section to `CLAUDE.md` under a `## Running the benchmark` heading:

```markdown
## Running the benchmark

```bash
# Install dependencies
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Full two-version comparison (takes ~30–60 minutes)
python run.py

# Re-run a single stage (delete results/<version>/ first if it exists)
python setup.py 26.5.5 quay.io/keycloak/keycloak:26.5.5
python realms.py 26.5.5
python flows.py 26.5.5
python teardown.py 26.5.5

# Regenerate report from existing results
python report.py
```

Results are written to `results/comparison.txt` (human-readable) and `results/comparison.json`.
```

- [ ] **Step 3: Commit**

```bash
git add requirements.txt CLAUDE.md
git commit -m "docs: update CLAUDE.md with benchmark run instructions"
```

---

## Chunk 10: Smoke test — verify the pipeline works end-to-end with 2 realms

### Task 11: Smoke test against live Docker stack

This task verifies the entire pipeline works before running the full 700-realm benchmark.

**Prerequisites:** Docker must be running.

- [ ] **Step 1: Temporarily override REALM_COUNT in config.py for smoke test**

Edit `config.py`: change `REALM_COUNT = 700` to `REALM_COUNT = 2` and `VERSIONS` to only the first version:

```python
VERSIONS = [
    ("26.5.5", "quay.io/keycloak/keycloak:26.5.5"),
]
REALM_COUNT = 2
```

- [ ] **Step 2: Run the smoke test**

```bash
python run.py
```

Expected output (abbreviated):
```
=== Running pipeline for Keycloak 26.5.5 ===
[setup] Starting stack for 26.5.5 ...
[setup] Keycloak is healthy.
[setup] Query log enabled.
[realms] Created 2/2 realms. Errors: 0
[flows] Query log offset recorded: ...
[flows] Completed 2 realms. Timing records: 14. Flow errors: 0
[teardown] Extracting query log...
[teardown] Stopping Docker stack...
```

- [ ] **Step 3: Verify output files exist**

```bash
ls results/26.5.5/
```
Expected: `errors.json  flows_timings.json  query.log  query_log_offset.txt  realms.json`

```bash
python -c "import json; d=json.load(open('results/26.5.5/flows_timings.json')); print(len(d), 'timing records')"
```
Expected: `14 timing records` (7 operations × 2 realms)

- [ ] **Step 4: Revert config.py to full settings**

```python
VERSIONS = [
    ("26.5.5", "quay.io/keycloak/keycloak:26.5.5"),
    ("nightly", "quay.io/keycloak/keycloak:nightly"),
]
REALM_COUNT = 700
```

Also delete the smoke test results so the full run can proceed:
```bash
rm -rf results/26.5.5/
```

- [ ] **Step 5: Commit**

```bash
git add config.py
git commit -m "feat: restore config.py to full 700-realm two-version settings"
```

- [ ] **Step 6: Run the full benchmark**

```bash
python run.py
```

When complete, check:
```bash
cat results/comparison.txt
```
