# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Performance/load testing scripts for Keycloak using Python and `httpx`. The project is in early stages — Python test scripts are expected to be added to the root or a `tests/` directory.

## Infrastructure

Start the local Keycloak environment (Keycloak + MariaDB via Docker):

```bash
docker compose up -d
```

Keycloak runs on `http://localhost:8080`. Admin credentials: `admin` / `admin`. The `organization` feature is enabled.

Wait for Keycloak to be healthy before running tests (the healthcheck retries up to 20 times with 15s intervals, so startup can take ~2 minutes).

## Dependencies

Python dependencies are in `requirements.txt`. Install with:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Current dependency: `httpx` (async-capable HTTP client used for Keycloak API calls).

## Output

Test results should be written to the `results/` directory (already in `.gitignore`).

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
