#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Phase 3 parity checker — verify JSON authoritative state vs SQLite shadow.

JSON stores are authoritative. SQLite shadows are replicas for fast parity
checking. This script compares the active execution-worker registry against the
shadow lease table and reports divergence as evidence for the operator -- it
never mutates either side.

Usage:
    uv run python scripts/parity-check.py --state-root <path>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check parity between JSON stores and SQLite shadow"
    )
    parser.add_argument(
        "--state-root",
        required=True,
        help="Path to the JSON state root directory",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to the shadow SQLite database (default: <state-root>/runtime-leases-shadow.db)",
    )
    args = parser.parse_args()

    from repoforge.adapters.persistence.json_process_lease_adapter import (
        JsonProcessLeaseAdapter,
    )
    from repoforge.adapters.persistence.parity import compare_lease_parity
    from repoforge.adapters.persistence.sqlite_lease_store import SqliteLeaseStore
    from repoforge.testing import InMemoryLockManager

    state_root = Path(args.state_root)
    leases = JsonProcessLeaseAdapter(state_root, InMemoryLockManager())
    shadow = SqliteLeaseStore(
        Path(args.db_path) if args.db_path else state_root / "runtime-leases-shadow.db"
    )
    try:
        report = compare_lease_parity(leases, shadow)
    finally:
        shadow.close()

    payload = report.as_dict()
    if report.in_sync:
        print(f"parity: in_sync (json={payload['json_count']}, shadow={payload['shadow_count']})")
        return 0
    print(
        f"parity: DRIFT (json={payload['json_count']}, "
        f"shadow={payload['shadow_count']}, only_in_json="
        f"{len(payload['only_in_json'])}, only_in_shadow="
        f"{len(payload['only_in_shadow'])}, json_scan_complete="
        f"{payload['json_scan_complete']})"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
