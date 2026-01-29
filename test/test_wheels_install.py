#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2024 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0

"""
Test wheel installation script for CI workflows.

This script finds and installs wheels compatible with the current Python version,
verifying that wheel files are valid and platform-compatible.
"""

from __future__ import annotations

import re
import subprocess
import sys

from pathlib import Path

WHEELS_DIR = Path("./downloaded_wheels")


def get_python_version_tag() -> str:
    """Get the Python version tag (e.g., '311' for Python 3.11)."""
    return f"{sys.version_info.major}{sys.version_info.minor}"


def is_wheel_compatible(wheel_name: str, python_version: str) -> bool:
    """
    Check if a wheel is compatible with the given Python version.

    Compatible wheels are:
    - cpXY: exact Python version match (e.g., cp311 for Python 3.11)
    - py3: universal Python 3 wheels
    - py2.py3: universal Python 2/3 wheels
    - abi3: stable ABI wheels (compatible with Python >= base version)
    """
    patterns = [
        rf"-cp{python_version}-",  # Exact version match
        r"-py3-",  # Universal Python 3
        r"-py2\.py3-",  # Universal Python 2/3
        r"-abi3-",  # Stable ABI
    ]
    return any(re.search(pattern, wheel_name) for pattern in patterns)


def find_compatible_wheels(python_version: str) -> list[Path]:
    """Find all wheel files compatible with the given Python version."""
    if not WHEELS_DIR.exists():
        return []

    wheels = []
    for wheel_path in WHEELS_DIR.glob("*.whl"):
        if is_wheel_compatible(wheel_path.name, python_version):
            wheels.append(wheel_path)

    return sorted(wheels)


def install_wheel(wheel_path: Path) -> tuple[bool, str]:
    """
    Install a wheel with --no-deps to verify wheel validity.

    Returns:
        tuple: (success: bool, error_message: str)
    """
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--no-index",
        "--find-links",
        str(WHEELS_DIR),
        str(wheel_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0:
        return True, ""

    return False, (result.stderr or result.stdout).strip()


def main() -> int:
    python_version = get_python_version_tag()

    # Find compatible wheels
    wheels = find_compatible_wheels(python_version)
    print(f"Found {len(wheels)} compatible wheels to test\n")

    if not wheels:
        print("No compatible wheels found!")
        return 1

    # Install each wheel
    installed = 0
    failed = 0
    failed_wheels = []

    print("Installing wheels (--no-deps to test wheel validity only)...")
    print("-" * 60)

    for wheel_path in wheels:
        success, error_message = install_wheel(wheel_path)

        if success:
            installed += 1
        else:
            failed += 1
            failed_wheels.append((wheel_path.name, error_message))
            print()
            print(f"ERROR: Failed to install {wheel_path.name}")
            if error_message:
                for line in error_message.split("\n"):
                    print(f"  {line}")
            print()

    print("-" * 60)
    print(f"Results: {installed} installed successfully, {failed} failed\n")

    # Print summary of failures
    if failed_wheels:
        print("Failed wheels:")
        for wheel_name, _ in failed_wheels:
            print(f"  - {wheel_name}")
        print()
        return 1

    print("All wheels installed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
