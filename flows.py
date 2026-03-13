#!/usr/bin/env python3
"""Stage 3: Run auth flow operations per realm and record timings + query log offset."""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import httpx
import config
from auth import TokenManager

PROVIDERS = ["auth-username-password-form", "auth-otp-form", "deny-access-authenticator"]


def _record_query_log_offset(version_tag: str):
    """Record current MariaDB general log size before flow operations start."""
    result = subprocess.run(
        f"{config.COMPOSE_CMD} exec -T {config.DB_SERVICE} stat -c%s /var/lib/mysql/general.log",
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, "DOCKER_HOST": config.DOCKER_HOST},
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
        flow_id = None
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
            # Best-effort cleanup: delete the flow if it was created before the exception
            if flow_id is not None:
                await _delete_flow(client, token_mgr, base, realm, flow_id, timings, errors)


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
