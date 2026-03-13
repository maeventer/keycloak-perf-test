#!/usr/bin/env python3
"""Coordinator: run the full pipeline for each Keycloak version, then generate report."""

import asyncio
import sys
import config
from setup import start_stack
from realms import create_realms
from flows import run_flows
from teardown import teardown
from report import generate_report

_failed_versions: list[str] = []


def run_version(version_tag: str, image: str):
    print(f"\n{'='*60}")
    print(f"  Running pipeline for Keycloak {version_tag}")
    print(f"{'='*60}\n")

    try:
        print("--- Stage 1: setup ---")
        start_stack(version_tag, image)

        print("\n--- Stage 2: create realms ---")
        asyncio.run(create_realms(version_tag))

        print("\n--- Stage 3: flow operations ---")
        asyncio.run(run_flows(version_tag))

        print("\n--- Stage 4: teardown ---")
        teardown(version_tag)
    except SystemExit as exc:
        print(f"\nERROR: Pipeline for {version_tag} aborted (exit code {exc.code}).", file=sys.stderr)
        _failed_versions.append(version_tag)


def main():
    for version_tag, image in config.VERSIONS:
        run_version(version_tag, image)

    if _failed_versions:
        print(f"\nWARNING: The following versions failed and are excluded from the report: {_failed_versions}", file=sys.stderr)

    print(f"\n{'='*60}")
    print("  Generating comparison report")
    print(f"{'='*60}\n")
    generate_report()


if __name__ == "__main__":
    main()
