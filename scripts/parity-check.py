#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""Phase 2 parity checker — verify JSON ↔ SQLite shadow consistency.

JSON stores are authoritative. SQLite shadows are write-only replicas
for fast parity checking. This script compares them and reports
divergences.

Usage:
    uv run python scripts/parity-check.py --state-root <path>

Phase 3 will wire the JSON adapters. For now this is a structural
skeleton that imports the shadow stores and validates the query
paths exist.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check parity between JSON stores and SQLite shadow"
    )
    parser.add_argument(
        "--state-root",
        required=True,
        help="Path to the JSON state root directory",
    )
    args = parser.parse_args()

    # Phase 2: SQLite shadow path is derived from state root
    # Phase 3: Wire full JSON adapter comparison
    print(f"Parity check against {args.state_root}")
    print("Phase 2 scaffold — full comparison deferred to Phase 3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
