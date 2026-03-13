#!/usr/bin/env python3
"""Stage 1: Start Docker stack for a given Keycloak version and enable query log."""

import os
import sys
import time
import subprocess
import httpx
from pathlib import Path
import config


def _compose_env(**extra):
    """Base environment for all docker-compose calls."""
    return {**os.environ, "DOCKER_HOST": config.DOCKER_HOST, **extra}


def _run(cmd, env=None, **kwargs):
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        env=env or _compose_env(), **kwargs,
    )
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
    result = subprocess.run(
        f"{config.COMPOSE_CMD} up -d",
        shell=True,
        capture_output=True,
        text=True,
        env=_compose_env(KC_IMAGE=image),
    )
    if result.returncode != 0:
        print(f"ERROR: {config.COMPOSE_CMD} up failed\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    _wait_for_keycloak()
    _enable_query_log()

    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"[setup] Stack ready. Results dir: {results_dir}")


def _wait_for_keycloak(timeout_s: int = 300, interval_s: int = 15):
    url = f"{config.KEYCLOAK_MANAGEMENT_URL}/health/ready"
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
        f'{config.COMPOSE_CMD} exec -T {config.DB_SERVICE} '
        f'mariadb -uroot -pkeycloak -e "{sql}"'
    )
    print("[setup] Query log enabled.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python setup.py <version_tag> <image>", file=sys.stderr)
        sys.exit(1)
    start_stack(sys.argv[1], sys.argv[2])
