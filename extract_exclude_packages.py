#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Extract excluded packages for a platform/python combination.

Usage: python extract_exclude_packages.py [platform] [python_version]
"""

import sys

import yaml

from packaging.specifiers import SpecifierSet

PLATFORM_MAP = {"win32": "windows", "linux": "linux", "darwin": "macos"}
ALL_PLATFORMS = ["linux", "windows", "macos"]


def get_excluded_packages(platform=None, python_version=None):
    """Get packages excluded for the given platform/python combination."""
    packages = set()

    with open("exclude_list.yaml") as f:
        data = yaml.safe_load(f)

    for entry in data:
        platforms = entry.get("platform", [])
        platforms = [platforms] if isinstance(platforms, str) else platforms
        platforms = [PLATFORM_MAP.get(p, p) for p in platforms] or ALL_PLATFORMS

        if platform and platform not in platforms:
            continue

        pythons = entry.get("python", [])
        pythons = [pythons] if isinstance(pythons, str) else pythons

        if python_version and pythons:
            if not any(python_version in SpecifierSet(c) for c in pythons):
                continue

        packages.add(entry["package_name"])

    return sorted(packages)


if __name__ == "__main__":
    platform = sys.argv[1] if len(sys.argv) > 1 else None
    python_ver = sys.argv[2] if len(sys.argv) > 2 else None
    print(" ".join(get_excluded_packages(platform, python_ver)))
