#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Official CPython image and manylinux tag for ARMv7 CI.

Bullseye images stop before Python 3.15. From 3.15 onward the current
lineage uses Trixie and the legacy lineage uses Bookworm.

The repair job downloads one artifact per Python version. The artifact
directory name is the build image; abi3 interpreter tags are the wheel's
minimum Python and are not used.
"""

from __future__ import annotations

import re
import sys

from pathlib import Path

_GLIBC_IN_PLAT = re.compile(r"manylinux_(\d+)_(\d+)")
_ARTIFACT_DIR_RE = re.compile(r"wheels-download-directory-linux-armv7(?P<legacy>legacy)?-(?P<py>\d+\.\d+)\Z")


def armv7_docker_spec(python_version: str, lineage: str) -> tuple[str, str]:
    """Return ``(image, manylinux plat)`` for an ARMv7 build or test job."""
    minor = int(python_version.split(".")[1])
    if lineage == "legacy":
        if minor >= 15:
            codename, plat = "bookworm", "manylinux_2_36_armv7l"
        else:
            codename, plat = "bullseye", "manylinux_2_31_armv7l"
    elif lineage == "current":
        if minor >= 15:
            codename, plat = "trixie", "manylinux_2_41_armv7l"
        else:
            codename, plat = "bookworm", "manylinux_2_36_armv7l"
    else:
        raise ValueError(f"lineage must be 'current' or 'legacy', got {lineage!r}")
    return f"python:{python_version}-{codename}", plat


def armv7_plat_for_artifact_dir(directory_name: str) -> str | None:
    """Manylinux tag for a per-Python ``wheels-download-directory-linux-armv7*`` directory."""
    match = _ARTIFACT_DIR_RE.fullmatch(directory_name)
    if not match:
        return None
    lineage = "legacy" if match.group("legacy") else "current"
    _image, plat = armv7_docker_spec(match.group("py"), lineage)
    return plat


def resolve_armv7_plat(wheel_path: Path, lineage_plat: str) -> str:
    """Plat for this wheel: the artifact directory's build image, else ``lineage_plat``.

    ``lineage_plat`` is the repair container tag (``manylinux_2_36`` current,
    ``manylinux_2_31`` legacy). A ``cp36-abi3`` wheel under
    ``wheels-download-directory-linux-armv7-3.15`` still resolves to Trixie's tag.
    """
    for parent in Path(wheel_path).parents:
        parsed = armv7_plat_for_artifact_dir(parent.name)
        if parsed:
            return parsed
    return lineage_plat


def _glibc_tuple(plat: str) -> tuple[int, int] | None:
    match = _GLIBC_IN_PLAT.search(plat)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def armv7_plat_newer_than(candidate: str, baseline: str) -> bool:
    """True when ``candidate`` needs a newer glibc than ``baseline``."""
    cand = _glibc_tuple(candidate)
    base = _glibc_tuple(baseline)
    if cand is None or base is None:
        return False
    return cand > base


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[3] not in ("image", "plat"):
        raise SystemExit(f"usage: {sys.argv[0]} <python-version> <current|legacy> <image|plat>")
    image, plat = armv7_docker_spec(sys.argv[1], sys.argv[2])
    print(image if sys.argv[3] == "image" else plat)


if __name__ == "__main__":
    main()
