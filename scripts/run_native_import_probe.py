#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Run native_import_guard.yaml import probes (used by cryptography spike / CI)."""

from __future__ import annotations

import argparse
import subprocess
import sys

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_GUARD_PATH = _ROOT / "native_import_guard.yaml"


def _guard_import_blocks(package_names: list[str]) -> dict[str, str]:
    """Return canonical name → import probe source for requested packages."""
    import yaml

    from packaging.utils import canonicalize_name

    data = yaml.safe_load(_GUARD_PATH.read_text(encoding="utf-8")) or {}
    wanted = {canonicalize_name(name) for name in package_names}
    blocks: dict[str, str] = {}
    for entry in data.get("packages") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        imports = entry.get("imports")
        if not name or not imports:
            continue
        canon = canonicalize_name(str(name))
        if canon not in wanted:
            continue
        stmts = [str(stmt).strip() for stmt in imports if str(stmt).strip()]
        if stmts:
            blocks[canon] = "\n".join(stmts)
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

    for name in args.packages:
        code = blocks.get(canonicalize_name(name))
        if code is None:
            print(f"ERROR: no guard entry for {name!r}", file=sys.stderr)
            failed += 1
            continue
        print(f"--- probe {name} ---")
        result = subprocess.run([sys.executable, "-c", code], check=False)
        if result.returncode != 0:
            print(f"FAILED: {name}", file=sys.stderr)
            failed += 1
        else:
            print(f"OK: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
