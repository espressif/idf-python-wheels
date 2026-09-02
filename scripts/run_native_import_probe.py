#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Run native_import_guard.yaml import probes (used by cryptography spike / CI)."""

from __future__ import annotations

import argparse
import sys

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _guard_import_blocks(package_names: list[str]) -> dict[str, tuple[str, ...]]:
    """Return canonical name → import probe statements for requested packages."""
    from packaging.utils import canonicalize_name

    from native_import_guard import package_import_statements

    blocks: dict[str, tuple[str, ...]] = {}
    for name in package_names:
        stmts = package_import_statements(name, repo_root=_ROOT)
        if stmts:
            blocks[canonicalize_name(name)] = stmts
    return blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "packages",
        nargs="*",
        default=["cryptography"],
        help="Canonical distribution names from native_import_guard.yaml",
    )
    args = parser.parse_args()

    blocks = _guard_import_blocks(args.packages)
    failed = 0
    from packaging.utils import canonicalize_name

    from native_import_probe import run_import_probes

    for name in args.packages:
        stmts = blocks.get(canonicalize_name(name))
        if stmts is None:
            print(f"ERROR: no guard entry for {name!r}", file=sys.stderr)
            failed += 1
            continue
        print(f"--- probe {name} ---")
        ok, msg = run_import_probes(stmts)
        if msg:
            print(msg)
        if not ok:
            print(f"FAILED: {name}", file=sys.stderr)
            failed += 1
        else:
            print(f"OK: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
