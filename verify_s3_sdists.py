#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Verify PEP 503 sdists exist on S3 for packages in sdist_requirements.txt."""

from __future__ import annotations

import re
import sys

from pathlib import Path

import boto3

from colorama import Fore
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from _helper_functions import print_color

SDIST_EXTENSIONS = (".tar.gz", ".zip", ".tar.bz2", ".tar.xz", ".tar.zst")


def _normalize_pkg_dir(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_requirement_names(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            names.append(canonicalize_name(Requirement(line).name))
        except InvalidRequirement:
            continue
    return names


def _sdist_keys_for_package(bucket, pkg_dir: str) -> list[str]:
    prefix = f"pypi/{pkg_dir}/"
    keys: list[str] = []
    for obj in bucket.objects.filter(Prefix=prefix):
        if obj.key.endswith(SDIST_EXTENSIONS):
            keys.append(obj.key)
    return keys


def verify_sdists_on_s3(bucket_name: str, requirements_path: Path) -> int:
    if not requirements_path.is_file():
        print_color(f"Missing {requirements_path}; skipping sdist verification.", Fore.YELLOW)
        return 0

    names = _load_requirement_names(requirements_path)
    if not names:
        print_color("No packages in sdist requirements file.", Fore.YELLOW)
        return 0

    s3 = boto3.resource("s3")
    bucket = s3.Bucket(bucket_name)

    print_color("---------- VERIFY S3 SDISTS ----------")
    print(f"Bucket: {bucket_name}")
    print(f"Packages to check: {len(names)}\n")

    violations: list[str] = []
    for name in sorted(set(names)):
        pkg_dir = _normalize_pkg_dir(name)
        keys = _sdist_keys_for_package(bucket, pkg_dir)
        if keys:
            print_color(f"-- {name}: OK ({len(keys)} sdist object(s))", Fore.GREEN)
        else:
            violations.append(name)
            print_color(f"-- {name}: MISSING sdist under pypi/{pkg_dir}/", Fore.RED)

    print_color("---------- END VERIFY S3 SDISTS ----------")
    if violations:
        print_color(f"Missing sdists: {len(violations)}", Fore.RED)
        return 1
    print_color("All required sdists present.", Fore.GREEN)
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("Usage: verify_s3_sdists.py <bucket_name> <sdist_requirements.txt>")
    return verify_sdists_on_s3(sys.argv[1], Path(sys.argv[2]))


if __name__ == "__main__":
    sys.exit(main())
