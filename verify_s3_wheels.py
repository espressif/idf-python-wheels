#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Verify S3 wheels against exclude_list.yaml.

Checks all wheels on S3, extracting Python version from wheel filename
to evaluate python_version markers correctly.
"""

import re
import sys

import boto3

from colorama import Fore

from _helper_functions import EXCLUDE_LIST_PATH
from _helper_functions import get_wheel_python_version
from _helper_functions import print_color
from _helper_functions import should_exclude_wheel_s3
from yaml_list_adapter import YAMLListAdapter


def is_unsupported_python(wheel_name: str, oldest_supported: str) -> tuple[bool, str]:
    """Check if wheel is for Python 2 or older than oldest_supported_python."""
    # Check for Python 2 only wheels (not py2.py3 which supports Python 3)
    if re.search(r"-cp2\d-|-py2-|-py2\.", wheel_name) and "-py2.py3-" not in wheel_name:
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
    if len(sys.argv) < 3:
        raise SystemExit("Usage: verify_s3_wheels.py <bucket_name> <oldest_supported_python>")

    bucket_name = sys.argv[1]
    oldest_supported_python = sys.argv[2]

    print_color("---------- VERIFY S3 WHEELS AGAINST EXCLUDE LIST ----------")
    print(f"Oldest supported Python: {oldest_supported_python}\n")

    # Connect to S3
    s3 = boto3.resource("s3")
    bucket = s3.Bucket(bucket_name)

    # Load exclude requirements (direct logic, no inversion)
    exclude_requirements = YAMLListAdapter(EXCLUDE_LIST_PATH, exclude=False).requirements
    print(f"Loaded {len(exclude_requirements)} exclude rules\n")

    # Get all wheels from S3
    print_color("---------- SCANNING S3 WHEELS ----------")
    wheels = []
    for obj in bucket.objects.filter(Prefix="pypi/"):
        if obj.key.endswith(".whl"):
            wheel_name = obj.key.split("/")[-1]
            wheels.append(wheel_name)

    print(f"Found {len(wheels)} wheels on S3\n")

    # Check each wheel
    print_color("---------- CHECKING WHEELS ----------")
    violations = []
    old_python_wheels = []

    for wheel in wheels:
        # Check for unsupported Python versions (warning only, not a violation)
        is_old, reason = is_unsupported_python(wheel, oldest_supported_python)
        if is_old:
            old_python_wheels.append((wheel, reason))
            continue

        # Check against exclude_list (actual violations)
        should_exclude, reason = should_exclude_wheel_s3(wheel, exclude_requirements)
        if should_exclude:
            violations.append((wheel, reason))
            print_color(f"-- {wheel}", Fore.RED)
            print(f"   {reason}")

    print_color("---------- END CHECKING ----------")

    # Statistics
    print_color("---------- STATISTICS ----------")
    print(f"Checked: {len(wheels)} wheels")
    if old_python_wheels:
        print_color(f"Old Python wheels: {len(old_python_wheels)} (warning only)", Fore.YELLOW)
    if violations:
        print_color(f"Violations: {len(violations)}", Fore.RED)
        print_color("\nWheels that should be deleted:", Fore.RED)
        for wheel, _ in violations:
            print(f"  - {wheel}")
        print_color("---------- END STATISTICS ----------")
        return 1
    else:
        print_color("Violations: 0", Fore.GREEN)
        print_color("---------- END STATISTICS ----------")
        return 0


if __name__ == "__main__":
    sys.exit(main())
