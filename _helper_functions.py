#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
from __future__ import annotations

import json
import os
import platform
import re
import sys
import zipfile

from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple
from typing import cast
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request
from urllib.request import urlopen

from colorama import Fore
from colorama import Style
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import InvalidWheelFilename
from packaging.utils import canonicalize_name
from packaging.utils import parse_wheel_filename
from packaging.version import InvalidVersion
from packaging.version import Version
from packaging.version import parse as parse_version

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


def wheel_archive_is_readable(path: Path) -> bool:
    """True if the file is a zip with a readable central directory (valid wheel container).

    ``zipfile.is_zipfile()`` only checks the leading magic; truncated or corrupt wheels can
    still fail with ``BadZipFile`` when reading the central directory (pip, delocate, etc.).
    """
    if not zipfile.is_zipfile(path):
        return False
    try:
        with zipfile.ZipFile(path, "r") as zf:
            zf.namelist()
    except zipfile.BadZipFile:
        return False
    return True


# PyPI JSON API: cache (project canonical name, version) -> requires_python or None if unset/unknown
_PYPI_REQUIRES_PYTHON_CACHE: Dict[Tuple[str, str], Optional[str]] = {}
# Full project JSON per canonical package name; None means fetch failed (cached)
_PYPI_PROJECT_JSON_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def _pypi_user_agent() -> str:
    return "idf-python-wheels (https://github.com/espressif/idf-python-wheels)"


def current_interpreter_satisfies_requires_python(requires_python: Optional[str]) -> bool:
    """True if this interpreter satisfies PyPI ``Requires-Python`` (PEP 345 / PEP 566), or if unset."""
    if requires_python is None or not requires_python.strip():
        return True
    try:
        spec = SpecifierSet(requires_python)
    except ValueError:
        return True
    py_ver = Version(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    return bool(spec.contains(py_ver, prereleases=True))


def fetch_pypi_release_requires_python(project_name: str, version: str, timeout: float = 20.0) -> Optional[str]:
    """Return ``info.requires_python`` for a release, or None if unknown (missing, error, or no field)."""
    key = (canonicalize_name(project_name), version)
    if key in _PYPI_REQUIRES_PYTHON_CACHE:
        return _PYPI_REQUIRES_PYTHON_CACHE[key]
    pkg = canonicalize_name(project_name)
    url = f"https://pypi.org/pypi/{quote(pkg)}/{quote(version)}/json"
    try:
        request = Request(url, headers={"User-Agent": _pypi_user_agent()})
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode())
    except HTTPError as e:
        if e.code == 404:
            _PYPI_REQUIRES_PYTHON_CACHE[key] = None
            return None
        _PYPI_REQUIRES_PYTHON_CACHE[key] = None
        return None
    except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        _PYPI_REQUIRES_PYTHON_CACHE[key] = None
        return None
    rp = data.get("info", {}).get("requires_python")
    if rp is None or (isinstance(rp, str) and not rp.strip()):
        _PYPI_REQUIRES_PYTHON_CACHE[key] = None
        return None
    _PYPI_REQUIRES_PYTHON_CACHE[key] = str(rp).strip()
    return _PYPI_REQUIRES_PYTHON_CACHE[key]


def fetch_pypi_project_json(project_name: str, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
    """Return PyPI ``/pypi/{name}/json`` payload, or None on error."""
    pkg = canonicalize_name(project_name)
    if pkg in _PYPI_PROJECT_JSON_CACHE:
        return _PYPI_PROJECT_JSON_CACHE[pkg]
    url = f"https://pypi.org/pypi/{quote(pkg)}/json"
    try:
        request = Request(url, headers={"User-Agent": _pypi_user_agent()})
        with urlopen(request, timeout=timeout) as response:
            # Use typing.Dict in cast(): dict[str, Any] is evaluated at runtime and breaks on Python 3.8.
            data = cast(Dict[str, Any], json.loads(response.read().decode()))
    except (HTTPError, URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
        _PYPI_PROJECT_JSON_CACHE[pkg] = None
        return None
    _PYPI_PROJECT_JSON_CACHE[pkg] = data
    return data


def matching_release_version_strings(req: Requirement) -> Optional[List[str]]:
    """List PyPI release version strings that satisfy ``req.specifier``, newest first.

    Returns None if project metadata could not be fetched (caller should not skip the build).
    Returns an empty list if no published release matches the specifier.
    """
    data = fetch_pypi_project_json(req.name)
    if data is None:
        return None
    releases = data.get("releases") or {}
    candidates: List[Tuple[Version, str]] = []
    for ver_str in releases:
        try:
            parsed = parse_version(ver_str)
        except InvalidVersion:
            continue
        if req.specifier.contains(parsed, prereleases=True):
            candidates.append((parsed, ver_str))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [pair[1] for pair in candidates]


def pypi_requires_python_preflight_skip(req: Requirement) -> Tuple[bool, str]:
    """If True, skip ``pip wheel``: no PyPI release matches the specifier for this interpreter.

    Uses project index + per-release ``Requires-Python`` (covers ``==``, ``~=``, ranges, etc.).
    Set ``SKIP_PYPI_REQUIRES_PYTHON_CHECK`` to disable.
    """
    if os.environ.get("SKIP_PYPI_REQUIRES_PYTHON_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return False, ""
    candidates = matching_release_version_strings(req)
    if candidates is None:
        return False, ""
    if not candidates:
        return True, "no PyPI releases match this requirement specifier"

    for ver_str in candidates:
        rp = fetch_pypi_release_requires_python(req.name, ver_str)
        if current_interpreter_satisfies_requires_python(rp):
            return False, ""

    newest = candidates[0]
    newest_rp = fetch_pypi_release_requires_python(req.name, newest)
    py_mm = f"{sys.version_info.major}.{sys.version_info.minor}"
    if newest_rp:
        return (
            True,
            f"newest matching release {newest!r} requires Python {newest_rp!r}; "
            f"no installable release for Python {py_mm} ({req.name})",
        )
    return True, f"no installable release on PyPI for Python {py_mm} ({req})"


def filter_requirements_by_pypi_requires_python(requirements: Set) -> Set:
    """Drop requirements with no PyPI release installable on this interpreter (``Requires-Python``)."""
    if os.environ.get("SKIP_PYPI_REQUIRES_PYTHON_CHECK", "").strip().lower() in ("1", "true", "yes"):
        return set(requirements)
    kept: Set = set()
    print_color("---------- PYPI Requires-Python PREFLIGHT ----------", Fore.CYAN)
    for req in requirements:
        if not isinstance(req, Requirement):
            kept.add(req)
            continue
        skip, reason = pypi_requires_python_preflight_skip(req)
        if skip:
            print_color(f"-- skip {req} ({reason})", Fore.YELLOW)
            continue
        kept.add(req)
    print_color("---------- END PYPI Requires-Python PREFLIGHT ----------", Fore.CYAN)
    return kept


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


def _safe_text_for_stdout(text: str) -> str:
    """Avoid UnicodeEncodeError when printing pip/tool output on Windows (e.g. cp1252 console)."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    if encoding.lower() in ("utf-8", "utf8"):
        return text
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def print_color(text: str, color: str = Fore.BLUE):
    """Print colored text specified by color argument based on colorama
    - default color BLUE
    """
    print(f"{color}", f"{_safe_text_for_stdout(text)}", Style.RESET_ALL)


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

    Uses packaging.utils.parse_wheel_filename for PEP 440–compliant parsing
    (epochs, local versions, post/dev releases, etc.).

    Returns:
        tuple: (normalized_package_name, version_str) or None if parsing fails
    """
    try:
        name, version, _build, _tags = parse_wheel_filename(wheel_name)
        return name, str(version)
    except InvalidWheelFilename:
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


# Wheel platform tag prefix/suffix -> sys_platform for PEP 508 marker evaluation
_WHEEL_PLATFORM_TO_SYS = (
    (("win_amd64", "win32"), "win32"),
    (("manylinux", "linux_"), "linux"),  # manylinux_*, linux_*
    (("macosx_",), "darwin"),
)


def get_wheel_sys_platforms(wheel_name: str) -> list[str] | None:
    """
    Derive sys_platform value(s) from the wheel filename for marker evaluation.

    Uses the wheel's platform tag(s) from parse_wheel_filename. For universal
    wheels (platform tag "any"), returns all three so platform-specific
    exclusions can be checked against every platform the wheel targets.

    Returns:
        List of sys_platform values ("win32", "linux", "darwin"), or None
        if the filename cannot be parsed.
    """
    try:
        _name, _version, _build, tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        return None
    platforms: set[str] = set()
    for tag in tags:
        pt = tag.platform
        if pt == "any":
            platforms.update(("linux", "win32", "darwin"))
            continue
        for prefixes, sys_plat in _WHEEL_PLATFORM_TO_SYS:
            if any(pt.startswith(p) for p in prefixes):
                platforms.add(sys_plat)
                break
    return list(platforms) if platforms else None


def should_exclude_wheel_s3(
    wheel_name: str,
    exclude_requirements: set,
    supported_python_versions: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Check if a wheel should be excluded for S3 verification.

    Uses DIRECT exclusion logic (not inverted):
    - If marker is True → exclusion applies → EXCLUDE
    - If marker is False → exclusion doesn't apply → KEEP
    - If version matches specifier → EXCLUDE

    Derives the wheel's target platform from its filename (e.g. win_amd64
    -> win32, manylinux_* -> linux) and evaluates sys_platform markers
    against that instead of skipping them, so platform-only exclusions
    in exclude_list.yaml are reported as S3 violations when applicable.

    For universal wheels (no cpXY tag, e.g. py3-none-any), python_version
    markers are evaluated against supported_python_versions when provided,
    so exclusions that apply only to older supported versions are not missed.

    Args:
        wheel_name: The wheel filename
        exclude_requirements: Set of Requirement objects from YAMLListAdapter (exclude=False)
        supported_python_versions: When the wheel has no cpXY tag, evaluate
            python_version markers against these versions (e.g. ["3.8", "3.9", "3.10", ...]).
            If None, falls back to the runner's Python (may miss version-specific exclusions).

    Returns:
        tuple: (should_exclude: bool, reason: str)
    """
    parsed = parse_wheel_name(wheel_name)
    if not parsed:
        return False, ""

    pkg_name, wheel_version = parsed
    canonical_name = canonicalize_name(pkg_name)
    wheel_python = get_wheel_python_version(wheel_name)
    wheel_sys_platforms = get_wheel_sys_platforms(wheel_name)

    # For universal wheels (no cpXY), evaluate python_version against these if provided
    python_versions_to_try: list[str | None] = []
    if wheel_python is not None:
        python_versions_to_try.append(wheel_python)
    elif supported_python_versions:
        python_versions_to_try.extend(supported_python_versions)
    else:
        python_versions_to_try.append(None)

    for req in exclude_requirements:
        if canonicalize_name(req.name) != canonical_name:
            continue

        # Evaluate markers (including sys_platform) using wheel's target platform and Python
        if req.marker:
            if "sys_platform" in str(req.marker):
                if not wheel_sys_platforms:
                    continue  # Cannot derive platform from filename → skip rule
                marker_matches = False
                for sys_plat in wheel_sys_platforms:
                    for pv in python_versions_to_try:
                        env = {"sys_platform": sys_plat}
                        if pv is not None:
                            env["python_version"] = pv
                        if req.marker.evaluate(environment=env):
                            marker_matches = True
                            break
                    if marker_matches:
                        break
                if not marker_matches:
                    continue  # Exclusion condition not met for this wheel's platform(s)
            else:
                marker_matches = False
                for pv in python_versions_to_try:
                    env = {"python_version": pv} if pv is not None else {}
                    if req.marker.evaluate(environment=env if env else None):
                        marker_matches = True
                        break
                if not marker_matches:
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
