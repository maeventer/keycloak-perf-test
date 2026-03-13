# keycloak-perf-test

Automated performance benchmark comparing Keycloak versions across authentication flow admin API operations — measures HTTP latency and SQL query patterns against MariaDB to detect regressions.

## What it does

The benchmark runs a full pipeline for each configured Keycloak version:

1. **Setup** — starts a Keycloak + MariaDB stack via Docker, enables the MariaDB general query log
2. **Realm creation** — creates N realms concurrently via the Keycloak admin API
3. **Flow operations** — for each realm: creates an authentication flow, adds 3 executions, fetches the execution list, raises the priority of one execution, then deletes the flow
4. **Teardown** — extracts the query log window covering only the flow operations, stops the stack
5. **Report** — compares HTTP latency (p50/p99 per operation) and SQL query counts/patterns across all versions

Results are written to `results/comparison.txt` (human-readable table) and `results/comparison.json`.

## Requirements

- Python 3.11+
- Docker or Podman with `docker-compose` (v1 standalone)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Edit `config.py` to control which versions are compared and how many realms to use:

```python
VERSIONS = [
    ("26.5.5", "quay.io/keycloak/keycloak:26.5.5"),
    ("nightly", "quay.io/keycloak/keycloak:nightly"),
]
REALM_COUNT = 30
CONCURRENCY = 20
```

For Podman, set the `DOCKER_HOST` socket path to match your machine:

```python
DOCKER_HOST = "unix:///var/folders/.../podman-machine-default-api.sock"
```

## Running

```bash
# Full benchmark — runs all versions sequentially, then generates report
python run.py
```

Individual stages can be run separately (useful for debugging):

```bash
python setup.py 26.5.5 quay.io/keycloak/keycloak:26.5.5
python realms.py 26.5.5
python flows.py 26.5.5
python teardown.py 26.5.5

# Regenerate report from existing results without re-running
python report.py
```

Each version's results are stored under `results/<version>/`. Delete a version's directory to re-run it.

## Example findings

Running 30 realms across 26.5.3, 26.5.4, 26.5.5, and nightly revealed a severe O(n²) SQL regression in nightly:

| Version | Realms | Total SELECTs | add_execution p50 |
|---|---:|---:|---:|
| 26.5.3 | 30 | 6,193 | 39 ms |
| 26.5.4 | 30 | 9,357 | 46 ms |
| 26.5.5 | 30 | 9,434 | 42 ms |
| nightly | 30 | 98,547 | 2,097 ms |

## Output

- `results/comparison.txt` — formatted table of HTTP timings and SQL counts
- `results/comparison.json` — full structured data including top query patterns per version
- `results/<version>/query.log` — raw MariaDB query log trimmed to the flow operations window
- `results/<version>/flows_timings.json` — per-realm per-operation timing records
- `results/<version>/errors.json` — any API errors encountered during realm creation or flow operations

## License

Apache 2.0 — see [LICENSE](LICENSE).
