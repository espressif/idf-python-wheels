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

from _helper_functions import print_color
from _helper_functions import requirement_satisfied_by_filenames


def _normalize_pkg_dir(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _load_requirement_lines(path: Path) -> list[Requirement]:
    requirements: list[Requirement] = []
    invalid: list[str] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirements.append(Requirement(line))
        except InvalidRequirement:
            invalid.append(f"line {line_no}: {line}")
    if invalid:
        print_color("Invalid requirement line(s) in sdist requirements file:", Fore.RED)
        for entry in invalid:
            print_color(f"  {entry}", Fore.RED)
        raise SystemExit(1)
    return requirements


def _sdist_basenames_for_package(bucket, pkg_dir: str) -> list[str]:
    prefix = f"pypi/{pkg_dir}/"
    names: list[str] = []
    for obj in bucket.objects.filter(Prefix=prefix):
        basename = obj.key.rsplit("/", maxsplit=1)[-1]
        if basename and basename != "index.html":
            names.append(basename)
    return names


def verify_sdists_on_s3(bucket_name: str, requirements_path: Path, *, strict: bool = False) -> int:
    if not requirements_path.is_file():
        msg = f"Missing {requirements_path}"
        if strict:
            print_color(f"{msg}; sdist verification failed.", Fore.RED)
            return 1
        print_color(f"{msg}; skipping sdist verification.", Fore.YELLOW)
        return 0

    requirements = _load_requirement_lines(requirements_path)
    if not requirements:
        print_color("No packages in sdist requirements file.", Fore.YELLOW)
        return 0

    s3 = boto3.resource("s3")
    bucket = s3.Bucket(bucket_name)

    print_color("---------- VERIFY S3 SDISTS ----------")
    print(f"Bucket: {bucket_name}")
    print(f"Requirements to check: {len(requirements)}\n")

    sdist_names_by_pkg: dict[str, list[str]] = {}
    violations: list[str] = []
    for req in requirements:
        pkg_dir = _normalize_pkg_dir(req.name)
        if pkg_dir not in sdist_names_by_pkg:
            sdist_names_by_pkg[pkg_dir] = _sdist_basenames_for_package(bucket, pkg_dir)
        line = str(req)
        if requirement_satisfied_by_filenames(sdist_names_by_pkg[pkg_dir], req, sdists_only=True):
            print_color(f"-- {line}: OK (sdist on S3)", Fore.GREEN)
        else:
            violations.append(line)
            print_color(f"-- {line}: MISSING sdist under pypi/{pkg_dir}/", Fore.RED)

    print_color("---------- END VERIFY S3 SDISTS ----------")
    if violations:
        print_color(f"Missing sdists: {len(violations)}", Fore.RED)
        return 1
    print_color("All required sdists present on S3.", Fore.GREEN)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    strict = "--strict" in argv
    args = [arg for arg in argv if arg != "--strict"]
    if len(args) < 2:
        raise SystemExit("Usage: verify_s3_sdists.py <bucket_name> <sdist_requirements.txt> [--strict]")
    return verify_sdists_on_s3(args[0], Path(args[1]), strict=strict)


if __name__ == "__main__":
    sys.exit(main())
