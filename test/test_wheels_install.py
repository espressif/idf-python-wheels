#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2024 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0

"""
Test wheel installation script for CI workflows.

This script finds and installs wheels compatible with the current Python version,
verifying that wheel files are valid and platform-compatible.
It also checks wheels against exclude_list.yaml and removes incompatible ones.
"""

from __future__ import annotations

import re
import subprocess
import sys

from pathlib import Path

from colorama import Fore
from packaging.utils import canonicalize_name
from packaging.version import Version

from _helper_functions import print_color
from yaml_list_adapter import YAMLListAdapter

WHEELS_DIR = Path("./downloaded_wheels")
EXCLUDE_LIST_PATH = Path("exclude_list.yaml")


def get_python_version_tag() -> str:
    """Get the Python version tag (e.g., '311' for Python 3.11)."""
    return f"{sys.version_info.major}{sys.version_info.minor}"


def parse_wheel_name(wheel_name: str) -> tuple[str, str] | None:
    """
    Parse wheel filename to extract package name and version.

    Wheel format: {distribution}-{version}(-{build tag})?-{python tag}-{abi tag}-{platform tag}.whl
    """
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)-(\d+(?:\.\d+)*(?:[a-zA-Z0-9.]+)?)-")
    match = pattern.match(wheel_name)
    if match:
        pkg_name = match.group(1)
        version = match.group(2)
        return pkg_name, version
    return None


def load_exclude_requirements() -> set:
    """Load exclude_list.yaml using YAMLListAdapter and return requirements set."""
    adapter = YAMLListAdapter(str(EXCLUDE_LIST_PATH), exclude=True)
    return adapter.requirements


def should_exclude_wheel(wheel_name: str, exclude_requirements: set) -> tuple[bool, str]:
    """
    Check if a wheel should be excluded based on exclude_list.yaml rules.

    Uses YAMLListAdapter with exclude=True, so the logic is inverted:
    - If marker evaluates to True -> wheel satisfies "keep" condition, skip
    - If version is in the (inverted) specifier -> wheel satisfies "keep" condition, skip
    - Otherwise -> wheel should be excluded

    Returns:
        tuple: (should_exclude: bool, reason: str)
    """
    parsed = parse_wheel_name(wheel_name)
    if not parsed:
        return False, ""

    pkg_name, wheel_version = parsed
    canonical_name = canonicalize_name(pkg_name)

    for req in exclude_requirements:
        # Check if package name matches (using canonical names)
        if canonicalize_name(req.name) != canonical_name:
            continue

        # With exclude=True, if marker evaluates to True -> KEEP the wheel
        if req.marker and req.marker.evaluate():
            continue

        # With exclude=True, if version is in the (inverted) specifier -> KEEP the wheel
        if req.specifier:
            try:
                if Version(wheel_version) in req.specifier:
                    continue
            except Exception:
                pass

        # Name matches, and marker is False (or absent), and version not in specifier (or absent)
        # -> EXCLUDE the wheel
        return True, f"matches exclude rule: {req}"

    return False, ""


def get_platform_patterns() -> list[str]:
    """Get regex patterns for wheels compatible with current platform."""
    platform = sys.platform
    if platform == "win32":
        return [r"-win_amd64\.whl$", r"-win32\.whl$", r"-any\.whl$"]
    elif platform == "darwin":
        return [r"-macosx_.*\.whl$", r"-any\.whl$"]
    elif platform == "linux":
        return [r"-manylinux.*\.whl$", r"-linux.*\.whl$", r"-any\.whl$"]
    else:
        # Unknown platform, only match universal wheels
        return [r"-any\.whl$"]


def is_wheel_compatible(wheel_name: str, python_version: str) -> bool:
    """
    Check if a wheel is compatible with the given Python version AND current platform.

    Python version compatibility:
    - cpXY: exact Python version match (e.g., cp311 for Python 3.11)
    - py3: universal Python 3 wheels
    - py2.py3: universal Python 2/3 wheels
    - abi3: stable ABI wheels (compatible with Python >= base version)

    Platform compatibility:
    - Windows: win32, win_amd64, any
    - macOS: macosx_*, any
    - Linux: manylinux*, linux*, any
    """
    # Check Python version compatibility
    python_patterns = [
        rf"-cp{python_version}-",  # Exact version match
        r"-py3-",  # Universal Python 3
        r"-py2\.py3-",  # Universal Python 2/3
        r"-abi3-",  # Stable ABI
    ]
    if not any(re.search(pattern, wheel_name) for pattern in python_patterns):
        return False

    # Check platform compatibility
    platform_patterns = get_platform_patterns()
    return any(re.search(pattern, wheel_name) for pattern in platform_patterns)


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


def is_python_version_error(error_message: str) -> bool:
    """Check if the error is due to Python version constraints in package metadata."""
    return "requires a different Python" in error_message


def main() -> int:
    python_version_tag = get_python_version_tag()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    print_color(f"---------- TEST WHEELS INSTALL (Python {python_version}) ----------")
    print(f"Platform: {sys.platform}\n")

    # Load exclude list using YAMLListAdapter
    exclude_requirements = load_exclude_requirements()
    print(f"Loaded {len(exclude_requirements)} exclude requirements from {EXCLUDE_LIST_PATH}\n")

    # Find compatible wheels
    wheels = find_compatible_wheels(python_version_tag)
    print(f"Found {len(wheels)} compatible wheels to test\n")

    if not wheels:
        print_color("No compatible wheels found!", Fore.RED)
        return 1

    # First pass: Check wheels against exclude_list and remove excluded ones
    excluded = 0
    excluded_wheels = []

    print_color("---------- EXCLUDE LIST CHECK ----------")

    wheels_to_install = []
    for wheel_path in wheels:
        should_exclude, reason = should_exclude_wheel(wheel_path.name, exclude_requirements)
        if should_exclude:
            excluded += 1
            excluded_wheels.append((wheel_path.name, reason))
            wheel_path.unlink()
            print_color(f"-- {wheel_path.name}", Fore.RED)
            print(f"   Reason: {reason}")
        else:
            wheels_to_install.append(wheel_path)

    print_color("---------- END EXCLUDE LIST CHECK ----------")
    print(f"Excluded {excluded} wheels\n")

    # Second pass: Install remaining wheels
    installed = 0
    failed = 0
    deleted = 0
    failed_wheels = []
    deleted_wheels = []

    print_color("---------- INSTALL WHEELS ----------")

    for wheel_path in wheels_to_install:
        success, error_message = install_wheel(wheel_path)

        if success:
            installed += 1
        elif is_python_version_error(error_message):
            # Wheel is valid but has Python version constraints in metadata
            # Delete it as it's incompatible with this Python version
            deleted += 1
            deleted_wheels.append(wheel_path.name)
            wheel_path.unlink()
            print_color(f"-- {wheel_path.name} (Python version constraint)", Fore.YELLOW)
        else:
            failed += 1
            failed_wheels.append((wheel_path.name, error_message))
            print_color(f"-- {wheel_path.name}", Fore.RED)
            if error_message:
                for line in error_message.split("\n")[:3]:
                    print(f"   {line}")

    print_color("---------- END INSTALL WHEELS ----------")

    # Print statistics
    print_color("---------- STATISTICS ----------")
    print_color(f"Installed {installed} wheels", Fore.GREEN)
    if excluded > 0:
        print_color(f"Excluded {excluded} wheels (exclude_list.yaml)", Fore.YELLOW)
    if deleted > 0:
        print_color(f"Deleted {deleted} wheels (Python version constraint)", Fore.YELLOW)
    if failed > 0:
        print_color(f"Failed {failed} wheels", Fore.RED)

    if failed_wheels:
        print_color("\nFailed wheels:", Fore.RED)
        for wheel_name, _ in failed_wheels:
            print(f"  - {wheel_name}")
        return 1

    print_color("\nAll compatible wheels processed successfully!", Fore.GREEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
