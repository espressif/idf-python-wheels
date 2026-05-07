#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Detect duplicate *.whl basenames with different file contents under a tree.

Used after downloading per-arch ``wheels-repaired-*`` artifacts into separate
subdirectories (``merge-multiple: false``) so a filesystem flatten step cannot
hide ARMv7 vs ARMv7 Legacy collisions before upload to S3.
"""

from __future__ import annotations

import hashlib
import sys

from collections import defaultdict
from pathlib import Path


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def collect_collision_errors(root: Path) -> list[str]:
    """Return human-readable error lines; empty if OK."""
    wheels: list[Path] = []
    for p in sorted(root.rglob("*.whl")):
        if p.is_file():
            wheels.append(p)

    by_name: defaultdict[str, list[Path]] = defaultdict(list)
    for p in wheels:
        by_name[p.name].append(p)

    errors: list[str] = []
    for name, paths in sorted(by_name.items()):
        if len(paths) < 2:
            continue
        by_digest: defaultdict[str, list[Path]] = defaultdict(list)
        for p in paths:
            by_digest[_sha256_file(p)].append(p)
        if len(by_digest) == 1:
            # Identical content in multiple artifact trees — unusual but safe.
            continue
        lines = [f"Duplicate wheel basename with different contents: {name}"]
        for p in paths:
            lines.append(f"  - {p}  sha256={_sha256_file(p)}")
        errors.append("\n".join(lines))
    return errors


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 2

    errors = collect_collision_errors(root)
    if errors:
        print("Wheel basename collision check failed:\n", file=sys.stderr)
        for block in errors:
            print(block, file=sys.stderr)
            print(file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
