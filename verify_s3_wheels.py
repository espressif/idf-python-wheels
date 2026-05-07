#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Verify S3 wheels against exclude_list.yaml.

Checks all wheels on S3, extracting Python version from wheel filename
to evaluate python_version markers correctly.
"""

from __future__ import annotations

import json
import re
import sys

from collections import defaultdict

import boto3

from colorama import Fore

from _helper_functions import EXCLUDE_LIST_PATH
from _helper_functions import get_wheel_python_version
from _helper_functions import parse_wheel_name
from _helper_functions import print_color
from _helper_functions import should_exclude_wheel_s3
from yaml_list_adapter import YAMLListAdapter

# Temporary: regex patterns for violations to ignore (wheel name is matched)
VIOLATION_EXCLUSION_REGEXES = [
    # re.compile(r"example-.*\.whl"),
    re.compile(r"cryptography-.*-cp38-.*\.whl"),  # Can be removed after dropping Python 3.8
    re.compile(r"gevent-.*-cp39-.*-win_amd64.whl"),  # Can be removed after dropping Python 3.9
    re.compile(r"gevent-.*-cp310-.*-win_amd64.whl"),  # Can be removed after dropping Python 3.10
    re.compile(r"gdbgui-0.13.2.0-py3-none-any.whl"),  # Maybe not uploaded by the CI, so keep it for now
]


def _normalize_pkg_dir(name: str) -> str:
    """Normalize S3 package directory naming differences.

    Historically, this repo used both underscore and dash package directories on S3
    (e.g. ``flask_compress`` vs ``flask-compress``). Those can legitimately contain the
    same wheel basenames. We treat that as a warning, not a violation.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _canonical_pkg_dirs_from_wheel_filename(wheel_name: str) -> set[str]:
    """Return PEP 503-normalized candidate package dirs derived from the wheel filename.

    Mirrors the wheel-name parsing used by ``upload_wheels.py`` (first ``-`` before a digit).
    """
    parsed = parse_wheel_name(wheel_name)
    if parsed:
        return {_normalize_pkg_dir(parsed[0])}
    match = re.compile(r"^(.+?)-(\d+)").search(wheel_name)
    if not match:
        return set()
    return {_normalize_pkg_dir(match.group(1))}


def get_supported_python_versions(supported_python_json: str) -> list[str]:
    """Parse supported_python from get-supported-versions output (jq -c .supported_python)."""
    try:
        versions = json.loads(supported_python_json.strip())
        if isinstance(versions, list) and all(isinstance(v, str) for v in versions):
            return versions
    except json.JSONDecodeError:
        pass
    raise SystemExit("Invalid supported_python (3rd argument): expected JSON array from get-supported-versions output.")


def is_unsupported_python(wheel_name: str, oldest_supported: str) -> tuple[bool, str]:
    """Check if wheel is for Python 2 or older than oldest_supported_python."""
    # Check for Python 2 only wheels (not py2.py3 which supports Python 3)
    if (
        re.search(r"-cp2\d-|-py2-|-py2\.", wheel_name)
        and "-py2.py3-" not in wheel_name
        and "-py2.py3." not in wheel_name
    ):
        return True, "Python 2 wheel"

    # Get wheel's Python version
    wheel_python = get_wheel_python_version(wheel_name)
    if not wheel_python:
        return False, ""  # Universal wheel, skip

    # Parse versions for comparison
    try:
        wheel_parts = [int(x) for x in wheel_python.split(".")]
        oldest_parts = [int(x) for x in oldest_supported.split(".")]

        # Compare major.minor
        if wheel_parts < oldest_parts:
            return True, f"Python {wheel_python} < oldest supported {oldest_supported}"
    except ValueError:
        pass

    return False, ""


def main():
    if len(sys.argv) < 4:
        raise SystemExit(
            "Usage: verify_s3_wheels.py <bucket_name> <oldest_supported_python> <supported_python_json>\n"
            "  supported_python_json: output from get-supported-versions (jq -c .supported_python)"
        )

    bucket_name = sys.argv[1]
    oldest_supported_python = sys.argv[2]
    supported_python_json = sys.argv[3]

    print_color("---------- VERIFY S3 WHEELS AGAINST EXCLUDE LIST ----------")
    print(f"Oldest supported Python: {oldest_supported_python}\n")

    supported_python_versions = get_supported_python_versions(supported_python_json)
    print(f"Supported Python versions (for universal wheels): {supported_python_versions}\n")

    # Connect to S3
    s3 = boto3.resource("s3")
    bucket = s3.Bucket(bucket_name)

    # Load exclude requirements (direct logic, no inversion)
    exclude_requirements = YAMLListAdapter(EXCLUDE_LIST_PATH, exclude=False).requirements
    print(f"Loaded {len(exclude_requirements)} exclude rules\n")

    # Get all wheels from S3
    print_color("---------- SCANNING S3 WHEELS ----------")
    basename_to_keys: defaultdict[str, list[str]] = defaultdict(list)
    for obj in bucket.objects.filter(Prefix="pypi/"):
        if obj.key.endswith(".whl"):
            wheel_name = obj.key.split("/")[-1]
            basename_to_keys[wheel_name].append(obj.key)

    wheel_names = sorted(basename_to_keys.keys())
    wheels_on_s3_count = sum(len(v) for v in basename_to_keys.values())

    print(f"Found {wheels_on_s3_count} wheel objects ({len(wheel_names)} unique filenames) on S3\n")

    # Check each wheel
    print_color("---------- CHECKING WHEELS ----------")
    violations = []
    old_python_wheels = []

    for wheel in wheel_names:
        # Check for unsupported Python versions (warning only, not a violation)
        is_old, reason = is_unsupported_python(wheel, oldest_supported_python)
        if is_old:
            old_python_wheels.append((wheel, reason))
            continue

        keys_for_name = basename_to_keys[wheel]
        if len(keys_for_name) > 1:
            # Determine whether the duplicate keys are only due to directory normalization
            # differences (underscore vs dash). Those are historical and are not treated as
            # violations (we cannot infer which object is authoritative without comparing bytes).
            pkg_dirs = []
            for k in keys_for_name:
                parts = k.split("/")
                pkg_dirs.append(parts[1] if len(parts) >= 3 else "")
            normalized = {_normalize_pkg_dir(d) for d in pkg_dirs if d}
            reason_dup = "Duplicate wheel basename across multiple S3 keys: " + ", ".join(sorted(keys_for_name))
            canonical = _canonical_pkg_dirs_from_wheel_filename(wheel)
            if len(normalized) <= 1 or (canonical and canonical.issubset(normalized)):
                print_color(f"-- {wheel}", Fore.YELLOW)
                print(f"   {reason_dup}")
                if len(normalized) <= 1:
                    print(f"   Note: directories normalize to {next(iter(normalized), '')!r}; treated as warning")
                else:
                    print(
                        "   Note: at least one prefix matches the wheel's canonical project dir "
                        f"{sorted(canonical)!r}; extra keys are likely stale/wrong-path duplicates; treated as warning"
                    )
            else:
                violations.append((wheel, reason_dup))
                print_color(f"-- {wheel}", Fore.RED)
                print(f"   {reason_dup}")

        # Check against exclude_list (actual violations)
        should_exclude, reason = should_exclude_wheel_s3(
            wheel, exclude_requirements, supported_python_versions=supported_python_versions
        )
        if should_exclude:
            if any(rx.search(wheel) for rx in VIOLATION_EXCLUSION_REGEXES):
                continue
            violations.append((wheel, reason))
            print_color(f"-- {wheel}", Fore.RED)
            print(f"   {reason}")

    print_color("---------- END CHECKING ----------")

    # Statistics
    print_color("---------- STATISTICS ----------")
    print(f"Checked: {wheels_on_s3_count} wheel objects ({len(wheel_names)} unique filenames)")
    if old_python_wheels:
        print_color(f"Old Python wheels: {len(old_python_wheels)} (warning only)", Fore.YELLOW)
    if violations:
        print_color(f"Violations: {len(violations)}", Fore.RED)
        print_color("\nWheel paths that should be deleted:", Fore.RED)
        for wheel, _ in violations:
            parsed = parse_wheel_name(wheel)
            pkg = parsed[0] if parsed else wheel.replace(".whl", "")
            print(f'"/pypi/{pkg}/{wheel}"')
        print_color("---------- END STATISTICS ----------")
        return 1
    else:
        print_color("Violations: 0", Fore.GREEN)
        print_color("---------- END STATISTICS ----------")
        return 0


if __name__ == "__main__":
    sys.exit(main())
