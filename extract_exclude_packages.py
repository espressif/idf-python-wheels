#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Extract excluded packages for a platform/python combination.

Usage: python extract_exclude_packages.py [platform] [python_version]

Platform can be: windows, macos, linux, linux_x86_64, linux_arm64, linux_armv7, macos_x86_64, macos_arm64
"""

import sys

import yaml

from packaging.specifiers import SpecifierSet

from _helper_functions import exclude_entry_applies_to_platform


def get_excluded_packages(platform=None, python_version=None):
    """Get packages excluded for the given platform/python combination."""
    packages = set()

    with open("exclude_list.yaml") as f:
        data = yaml.safe_load(f)

    for entry in data:
        # Skip packages excluded everywhere (no platform/python) - don't test build those
        platforms = entry.get("platform", [])
        pythons = entry.get("python", [])
        if not platforms and not pythons:
            continue

        if platform and not exclude_entry_applies_to_platform(entry, platform):
            continue

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
