#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Official CPython image and manylinux tag for ARMv7 CI.

Legacy is Bullseye only. Python 3.14 and newer have no Bullseye image and are
not built on that line. The current lineage stays on Bookworm.
"""

from __future__ import annotations

import sys


def armv7_docker_spec(python_version: str, lineage: str) -> tuple[str, str]:
    """Return ``(image, manylinux plat)`` for an ARMv7 build or test job."""
    minor = int(python_version.split(".")[1])
    if lineage == "legacy":
        if minor >= 14:
            raise ValueError(f"ARMv7 legacy is Bullseye only; Python {python_version} has no bullseye image")
        codename, plat = "bullseye", "manylinux_2_31_armv7l"
    elif lineage == "current":
        codename, plat = "bookworm", "manylinux_2_36_armv7l"
    else:
        raise ValueError(f"lineage must be 'current' or 'legacy', got {lineage!r}")
    return f"python:{python_version}-{codename}", plat


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[3] not in ("image", "plat"):
        raise SystemExit(f"usage: {sys.argv[0]} <python-version> <current|legacy> <image|plat>")
    image, plat = armv7_docker_spec(sys.argv[1], sys.argv[2])
    print(image if sys.argv[3] == "image" else plat)


if __name__ == "__main__":
    main()
