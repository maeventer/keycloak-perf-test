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
            _create_realm(client, token_mgr, sem, n, realm_names, errors)
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
