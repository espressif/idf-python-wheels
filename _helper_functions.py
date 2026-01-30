#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
from __future__ import annotations

import platform
import re
import sys

from colorama import Fore
from colorama import Style
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

# Packages that should be built from source on Linux to ensure correct library linking
# These packages often have pre-built wheels on PyPI that link against different library versions
# NOTE: This only applies to Linux (especially ARM) - Windows and macOS pre-built wheels work fine
# NOTE: Do NOT add packages with Rust components (cryptography, pynacl, bcrypt) here
# as they have complex build requirements and may not support all Python versions
FORCE_SOURCE_BUILD_PACKAGES_LINUX = [
    "cffi",
    "pillow",
    "pyyaml",
    "brotli",
    "greenlet",
    "bitarray",
]

EXCLUDE_LIST_PATH = "exclude_list.yaml"

# Platform names for exclude_list.yaml (YAML -> runner name)
PLATFORM_MAP = {"win32": "windows", "linux": "linux", "darwin": "macos"}
ALL_PLATFORMS = ["linux", "windows", "macos"]
LINUX_ARCHS = ["linux_x86_64", "linux_arm64", "linux_armv7"]
MACOS_ARCHS = ["macos_x86_64", "macos_arm64"]


def get_current_platform() -> str:
    """Return current runner platform:
    windows, macos, linux, linux_x86_64, linux_arm64, linux_armv7, macos_x86_64, macos_arm64
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        if machine in ("x86_64", "amd64"):
            return "linux_x86_64"
        if machine == "aarch64":
            return "linux_arm64"
        if machine == "armv7l":
            return "linux_armv7"
        return "linux"
    if system == "darwin":
        if machine in ("x86_64", "amd64"):
            return "macos_x86_64"
        if machine == "arm64":
            return "macos_arm64"
        return "macos"
    if system == "windows":
        return "windows"
    return sys.platform


def exclude_entry_applies_to_platform(entry: dict, current_platform: str) -> bool:
    """True if this exclude_list entry applies to current_platform (so we should exclude from build)."""
    platforms = entry.get("platform", [])
    platforms = [platforms] if isinstance(platforms, str) else platforms
    platforms = [PLATFORM_MAP.get(p, p) for p in platforms] or ALL_PLATFORMS
    if current_platform in platforms:
        return True
    if current_platform in LINUX_ARCHS and "linux" in platforms:
        return True
    if current_platform in MACOS_ARCHS and "macos" in platforms:
        return True
    return False


def get_no_binary_args(requirement_name: str) -> list:
    """Get --no-binary arguments if this package should be built from source.

    This only applies on Linux platforms where pre-built wheels may link against
    different library versions. On Windows and macOS, pre-built wheels work correctly.

    Args:
        requirement_name: Package name or requirement string (e.g., "cffi" or "cffi>=1.0")

    Returns:
        List with --no-binary arguments if package should be built from source, empty list otherwise
    """
    # Only force source builds on Linux (where we have library version issues)
    if platform.system() != "Linux":
        return []

    # Extract package name from requirement string (e.g., "cffi>=1.0" -> "cffi")
    match = re.match(r"^([a-zA-Z0-9_-]+)", str(requirement_name).strip())
    if not match:
        return []
    pkg_name = match.group(1).lower().replace("-", "_")

    for pkg in FORCE_SOURCE_BUILD_PACKAGES_LINUX:
        if pkg.lower().replace("-", "_") == pkg_name:
            return ["--no-binary", match.group(1)]
    return []


def print_color(text: str, color: str = Fore.BLUE):
    """Print colored text specified by color argument based on colorama
    - default color BLUE
    """
    print(f"{color}", f"{text}", Style.RESET_ALL)


def merge_requirements(requirement: Requirement, another_req: Requirement) -> Requirement:
    """Merges two requirements into one requirement."""
    new_ver_specifier = ""
    new_markers = ""
    if requirement.specifier and another_req.specifier:
        if not another_req.marker and (
            "==" not in str(requirement.specifier) and "!=" not in str(requirement.specifier)
        ):
            new_ver_specifier = f"{requirement.specifier},{another_req.specifier}"
        else:
            new_ver_specifier = another_req.specifier
    elif requirement.specifier and not another_req.specifier:
        new_ver_specifier = requirement.specifier
    elif not requirement.specifier and another_req.specifier:
        new_ver_specifier = another_req.specifier

    if requirement.marker and another_req.marker:
        new_markers = f"({requirement.marker}) and ({another_req.marker})"
    elif requirement.marker and not another_req.marker:
        new_markers = requirement.marker
    elif not requirement.marker and another_req.marker:
        new_markers = another_req.marker

    # construct new requirement
    new_requirement = Requirement(
        f"{requirement.name}{new_ver_specifier}" + (f"; {new_markers}" if new_markers else "")
    )

    return new_requirement


def parse_wheel_name(wheel_name: str) -> tuple[str, str] | None:
    """
    Parse wheel filename to extract package name and version.

    Wheel format: {distribution}-{version}(-{build tag})?-{python tag}-{abi tag}-{platform tag}.whl

    Returns:
        tuple: (package_name, version) or None if parsing fails
    """
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)-(\d+(?:\.\d+)*(?:[a-zA-Z0-9.]+)?)-")
    match = pattern.match(wheel_name)
    if match:
        return match.group(1), match.group(2)
    return None


def should_exclude_wheel(wheel_name: str, exclude_requirements: set) -> tuple[bool, str]:
    """
    Check if a wheel should be excluded based on exclude_list.yaml rules.

    Evaluates markers against the CURRENT running Python environment.

    Uses YAMLListAdapter with exclude=True, so the logic is inverted:
    - If marker evaluates to True -> wheel satisfies "keep" condition, skip
    - If version is in the (inverted) specifier -> wheel satisfies "keep" condition, skip
    - Otherwise -> wheel should be excluded

    Args:
        wheel_name: The wheel filename (e.g., "requests-2.31.0-py3-none-any.whl")
        exclude_requirements: Set of Requirement objects from YAMLListAdapter

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
        if req.specifier and wheel_version:
            try:
                if Version(wheel_version) in req.specifier:
                    continue
            except Exception:
                pass

        # Name matches, and marker is False (or absent), and version not in specifier (or absent)
        # -> EXCLUDE the wheel
        return True, f"matches exclude rule: {req}"

    return False, ""


def get_wheel_python_version(wheel_name: str) -> str | None:
    """
    Extract Python version from wheel filename.

    Examples:
        - "pkg-1.0-cp311-cp311-linux.whl" -> "3.11"
        - "pkg-1.0-py3-none-any.whl" -> None (universal)
    """
    match = re.search(r"-cp(\d)(\d+)-", wheel_name)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return None


def should_exclude_wheel_s3(wheel_name: str, exclude_requirements: set) -> tuple[bool, str]:
    """
    Check if a wheel should be excluded for S3 verification.

    Uses DIRECT exclusion logic (not inverted):
    - If marker is True → exclusion applies → EXCLUDE
    - If marker is False → exclusion doesn't apply → KEEP
    - If version matches specifier → EXCLUDE

    Skips sys_platform markers (can't evaluate cross-platform).

    Args:
        wheel_name: The wheel filename
        exclude_requirements: Set of Requirement objects from YAMLListAdapter (exclude=False)

    Returns:
        tuple: (should_exclude: bool, reason: str)
    """
    parsed = parse_wheel_name(wheel_name)
    if not parsed:
        return False, ""

    pkg_name, wheel_version = parsed
    canonical_name = canonicalize_name(pkg_name)
    wheel_python = get_wheel_python_version(wheel_name)

    for req in exclude_requirements:
        if canonicalize_name(req.name) != canonical_name:
            continue

        # Skip rules with sys_platform - can't evaluate cross-platform
        if req.marker and "sys_platform" in str(req.marker):
            continue

        # Evaluate python_version markers with wheel's target Python
        # If marker is False -> exclusion doesn't apply -> KEEP (continue)
        if req.marker:
            env = {"python_version": wheel_python} if wheel_python else {}
            if not req.marker.evaluate(environment=env if env else None):
                continue  # Exclusion condition not met → keep

        # If we get here, marker is True (or no marker)
        # Check version specifier - if version matches, EXCLUDE
        if req.specifier and wheel_version:
            try:
                if Version(wheel_version) not in req.specifier:
                    continue  # Version doesn't match exclusion → keep
            except Exception:
                pass

        return True, f"matches exclude rule: {req}"

    return False, ""
