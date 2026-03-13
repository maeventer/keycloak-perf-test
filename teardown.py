#!/usr/bin/env python3
"""Stage 4: Extract trimmed query log and stop Docker stack."""

import os
import subprocess
import sys
from pathlib import Path
import config


def _compose_env():
    return {**os.environ, "DOCKER_HOST": config.DOCKER_HOST}


def _run(cmd, check=True):
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=_compose_env())
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
            f"{config.COMPOSE_CMD} exec -T {config.DB_SERVICE} "
            f"tail -c +{tail_start} /var/lib/mysql/general.log",
            shell=True,
            capture_output=True,
            env=_compose_env(),
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
    _run(f"{config.COMPOSE_CMD} down -v")
    print("[teardown] Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python teardown.py <version_tag>", file=sys.stderr)
        sys.exit(1)
    teardown(sys.argv[1])
