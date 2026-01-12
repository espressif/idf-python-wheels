#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
import platform
import re

from colorama import Fore
from colorama import Style
from packaging.requirements import Requirement

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
