#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2024 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0

"""
Test wheel installation script for CI workflows.

This script finds and installs wheels compatible with the current Python version,
verifying that wheel files are valid and platform-compatible.
It also checks wheels against exclude_list.yaml and removes incompatible ones.

After a successful run, wheels that do not match this job's Python version and host
platform are deleted from ``downloaded_wheels`` so CI ``wheels-tested-*`` artifacts
do not carry wheels for other Python versions. CI downloads ``wheels-repaired-<arch>``
per matrix row (not the full merge) so ARMv7 vs ARMv7 Legacy binaries are not mixed.

After ``pip install``, :func:`_run_native_import_probes` applies
``native_import_guard.yaml`` (custom imports, skip filters, and default
``import <top_level>`` when ``probe_unlisted`` is true) so wheels that
install cleanly but fail at load time (OpenSSL / SIGABRT) fail CI before upload.

Wheels are ZIP archives (PEP 427). pip opens them with the zipfile module; a
BadZipFile / "Bad magic number" error means the bytes on disk are not a valid
ZIP (truncated, corrupted, or not a wheel), not that ".whl" was mistaken for ".zip".
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys

from pathlib import Path

from colorama import Fore
from packaging.utils import canonicalize_name

from _helper_functions import EXCLUDE_LIST_PATH
from _helper_functions import armv7_wheel_matches_forced_plat
from _helper_functions import get_current_platform
from _helper_functions import parse_wheel_name
from _helper_functions import print_color
from _helper_functions import should_exclude_wheel
from _helper_functions import wheel_archive_is_readable
from native_import_guard import load_native_import_guard
from native_import_guard import resolve_native_import_statements
from native_import_probe import run_import_probes
from yaml_list_adapter import YAMLListAdapter

WHEELS_DIR = Path("./downloaded_wheels")


def get_python_version_tag() -> str:
    """Get the Python version tag (e.g., '311' for Python 3.11)."""
    return f"{sys.version_info.major}{sys.version_info.minor}"


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


def _armv7_test_plat() -> tuple[str, bool] | None:
    """Return ``(AUDITWHEEL_PLAT, only_plat)`` on Linux ARMv7 test runners."""
    if platform.system() != "Linux":
        return None
    if platform.machine().lower() not in ("armv7l", "armv7", "armhf"):
        return None
    plat = os.environ.get("AUDITWHEEL_PLAT", "").strip()
    only_plat = os.environ.get("AUDITWHEEL_ONLY_PLAT", "").strip().lower() in ("1", "true", "yes")
    if plat:
        return plat, only_plat
    try:
        codename = ""
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION_CODENAME="):
                codename = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        return None
    if codename == "bullseye":
        return "manylinux_2_31_armv7l", True
    if codename == "bookworm":
        return "manylinux_2_36_armv7l", True
    return None


def _should_skip_native_import_probe(dist_name: str) -> bool:
    """Platform-specific skips for native import probes after ``pip install``."""
    return _armv7_skip_cryptography_native_probe(dist_name)


def _run_native_import_probes(installed_wheels: list[Path]) -> tuple[int, list[tuple[str, str]]]:
    """Import native extensions after install; catch load-time ABI/OpenSSL mismatches.

    Resolution is ``native_import_guard.yaml`` (custom imports, skip filters,
    ``probe_unlisted`` default ``import <top_level>``).
    """
    config = load_native_import_guard()
    native_failed = 0
    failed_wheels: list[tuple[str, str]] = []
    if not installed_wheels:
        return native_failed, failed_wheels

    print_color("---------- NATIVE IMPORT PROBES ----------")
    for wheel_path in installed_wheels:
        parsed = parse_wheel_name(wheel_path.name)
        if not parsed:
            continue
        stmts = resolve_native_import_statements(wheel_path, config=config)
        if stmts is None:
            continue
        if _should_skip_native_import_probe(parsed[0]):
            print_color(
                f"-- skip {wheel_path.name} (native import probe skipped on this platform)",
                Fore.YELLOW,
            )
            continue
        ok, msg = run_import_probes(stmts)
        if ok:
            print_color(f"-- {wheel_path.name}", Fore.GREEN)
            if msg:
                for line in msg.splitlines():
                    print(f"   {line}")
        else:
            native_failed += 1
            err = msg or "native import probe failed"
            failed_wheels.append((wheel_path.name, err))
            print_color(f"-- {wheel_path.name}", Fore.RED)
            if msg:
                for line in msg.splitlines()[:8]:
                    print(f"   {line}")
            wheel_path.unlink(missing_ok=True)
    print_color("---------- END NATIVE IMPORT PROBES ----------")
    if native_failed:
        print_color(f"Native import failures: {native_failed}", Fore.RED)
    return native_failed, failed_wheels


def _armv7_skip_cryptography_native_probe(dist_name: str) -> bool:
    """Skip cryptography import probe on Bookworm ARMv7 test images.

    Piwheels cryptography 49+ is often linked against OpenSSL 3.2+ while the bookworm
    ``python:*-bookworm`` test container ships OpenSSL 3.0.x. The wheel is still published
    for newer Raspberry Pi OS; cffi/argon2 in-lineage rebuilds are probed separately.
    """
    if canonicalize_name(dist_name) != canonicalize_name("cryptography"):
        return False
    armv7_plat = _armv7_test_plat()
    return armv7_plat is not None and armv7_plat[0] == "manylinux_2_36_armv7l"


def _platform_compatible(wheel_name: str) -> bool:
    platform_patterns = get_platform_patterns()
    if not any(re.search(pattern, wheel_name) for pattern in platform_patterns):
        return False
    armv7_plat = _armv7_test_plat()
    if armv7_plat is not None:
        plat, only_plat = armv7_plat
        if not armv7_wheel_matches_forced_plat(wheel_name, plat, only_plat=only_plat):
            return False
    return True


def is_wheel_compatible(wheel_name: str, python_version: str) -> bool:
    """
    Check if a wheel is compatible with the given Python version AND current platform.

    Python version compatibility:
    - cpXY-cpXY: exact Python version match (e.g., cp311-cp311 for Python 3.11 only)
    - cpXY-abi3: stable ABI wheels (compatible with Python >= XY)
    - py3: universal Python 3 wheels
    - py2.py3: universal Python 2/3 wheels

    Platform compatibility:
    - Windows: win32, win_amd64, any
    - macOS: macosx_*, any
    - Linux: manylinux*, linux*, any
    """
    current_version = int(python_version)  # e.g., 311 for Python 3.11

    # Check for abi3 wheels first - they have a minimum Python version requirement
    abi3_match = re.search(r"-cp(\d+)-abi3-", wheel_name)
    if abi3_match:
        base_version = int(abi3_match.group(1))  # e.g., 38 or 311 (not 3.8 or 3.11)
        # abi3 wheels work on Python >= base_version (using these integer tags)
        if current_version >= base_version:
            return _platform_compatible(wheel_name)
        return False

    # Check Python version compatibility for non-abi3 wheels
    python_patterns = [
        rf"-cp{python_version}-cp{python_version}-",  # Exact version match (cpXY-cpXY)
        rf"-cp{python_version}-",  # Fallback for other cpXY patterns
        r"-py3-",  # Universal Python 3
        r"-py2\.py3-",  # Universal Python 2/3
    ]
    if not any(re.search(pattern, wheel_name) for pattern in python_patterns):
        return False

    return _platform_compatible(wheel_name)


def find_compatible_wheels(python_version: str) -> list[Path]:
    """Find all wheel files compatible with the given Python version."""
    if not WHEELS_DIR.exists():
        return []

    wheels = []
    for wheel_path in WHEELS_DIR.glob("*.whl"):
        if is_wheel_compatible(wheel_path.name, python_version):
            wheels.append(wheel_path)

    return sorted(wheels)


def prune_wheels_not_for_current_python(
    python_version_tag: str,
    wheels_dir: Path | None = None,
) -> int:
    """Remove ``*.whl`` files that are not compatible with this Python + platform.

    CI downloads ``wheels-repaired-<arch>`` per matrix row into ``downloaded_wheels``,
    then tests only compatible wheels. Without pruning, the subsequent
    ``wheels-tested-<arch>-<py>`` artifact would still contain every cp/py tag from the
    repair tree for that arch. ``wheels_dir`` defaults to ``WHEELS_DIR`` for production;
    tests may pass a temporary directory.
    """
    base = wheels_dir if wheels_dir is not None else WHEELS_DIR
    if not base.exists():
        return 0
    removed = 0
    for wheel_path in base.glob("*.whl"):
        if not is_wheel_compatible(wheel_path.name, python_version_tag):
            wheel_path.unlink(missing_ok=True)
            removed += 1
    return removed


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


def is_compatibility_error(error_message: str) -> bool:
    """Check if the error is due to Python version or platform constraints."""
    compatibility_errors = [
        "requires a different Python",
        "not a supported wheel on this platform",
        "is not a supported wheel",
    ]
    return any(err in error_message for err in compatibility_errors)


def is_corrupt_wheel_archive_error(error_message: str) -> bool:
    """True if pip failed because the file is not a readable ZIP / wheel archive."""
    if not error_message:
        return False
    # pip.exceptions.InvalidWheel -> "Wheel 'pkg' located at <path> is invalid."
    if "Wheel '" in error_message and " is invalid." in error_message:
        return True
    markers = (
        "BadZipFile",
        "Bad magic number for file header",
        "Bad magic number for central directory",
        "has an invalid wheel",
        "zipfile.BadZipFile",
    )
    return any(m in error_message for m in markers)


def discard_corrupt_wheel(wheel_path: Path, note: str) -> None:
    """Remove wheel from the test tree and print a single-line warning."""
    wheel_path.unlink(missing_ok=True)
    print_color(f"-- {wheel_path.name} ({note})", Fore.YELLOW)


def _platform_wheels_all_excluded(exclude_requirements: set) -> bool:
    """True when every platform-matching wheel in ``WHEELS_DIR`` is excluded by policy."""
    if not WHEELS_DIR.exists():
        return False
    platform_wheels = [p for p in WHEELS_DIR.glob("*.whl") if _platform_compatible(p.name)]
    if not platform_wheels:
        return False
    return all(should_exclude_wheel(p.name, exclude_requirements)[0] for p in platform_wheels)


def main() -> int:
    python_version_tag = get_python_version_tag()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    print_color(f"---------- TEST WHEELS INSTALL (Python {python_version}) ----------")
    print(f"Platform: {sys.platform}\n")

    # Load exclude list for current platform (exclude=True for runtime filtering)
    exclude_requirements = YAMLListAdapter(
        EXCLUDE_LIST_PATH, exclude=True, current_platform=get_current_platform()
    ).requirements
    print(f"Loaded {len(exclude_requirements)} exclude requirements from {EXCLUDE_LIST_PATH}\n")

    # Find compatible wheels
    wheels = find_compatible_wheels(python_version_tag)
    print(f"Found {len(wheels)} compatible wheels to test\n")

    if not wheels:
        if _platform_wheels_all_excluded(exclude_requirements):
            print_color(
                "No installable wheels for this Python version; platform wheels are excluded by policy.",
                Fore.YELLOW,
            )
            return 0
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
    discarded_corrupt = 0
    failed_wheels = []
    deleted_wheels = []
    installed_wheels: list[Path] = []

    print_color("---------- INSTALL WHEELS ----------")

    for wheel_path in wheels_to_install:
        if not wheel_archive_is_readable(wheel_path):
            discarded_corrupt += 1
            discard_corrupt_wheel(
                wheel_path,
                "unreadable / corrupt zip — not a valid wheel archive (PEP 427)",
            )
            continue

        success, error_message = install_wheel(wheel_path)

        if success:
            installed += 1
            installed_wheels.append(wheel_path)
        elif is_compatibility_error(error_message):
            # Wheel is valid but has Python version or platform constraints
            # Delete it as it's incompatible with this environment
            deleted += 1
            deleted_wheels.append(wheel_path.name)
            wheel_path.unlink()
            print_color(f"-- {wheel_path.name} (compatibility constraint)", Fore.YELLOW)
        elif is_corrupt_wheel_archive_error(error_message):
            # Truncated/corrupt artifact or bad repair output; drop from this test artifact
            # so CI can continue (see module docstring).
            discarded_corrupt += 1
            discard_corrupt_wheel(wheel_path, "invalid / corrupt zip (pip could not read wheel)")
        else:
            failed += 1
            failed_wheels.append((wheel_path.name, error_message))
            print_color(f"-- {wheel_path.name}", Fore.RED)
            if error_message:
                for line in error_message.split("\n")[:3]:
                    print(f"   {line}")

    print_color("---------- END INSTALL WHEELS ----------")

    native_failed, probe_failures = _run_native_import_probes(installed_wheels)
    if native_failed:
        failed += native_failed
        failed_wheels.extend(probe_failures)

    # Print statistics
    print_color("---------- STATISTICS ----------")
    print_color(f"Installed {installed} wheels", Fore.GREEN)
    if excluded > 0:
        print_color(f"Excluded {excluded} wheels (exclude_list.yaml)", Fore.YELLOW)
    if deleted > 0:
        print_color(f"Deleted {deleted} wheels (compatibility constraint)", Fore.YELLOW)
    if discarded_corrupt > 0:
        print_color(
            f"Discarded {discarded_corrupt} wheels (invalid or corrupt zip archive)",
            Fore.YELLOW,
        )
    if failed > 0:
        print_color(f"Failed {failed} wheels", Fore.RED)

    if failed_wheels:
        print_color("\nFailed wheels:", Fore.RED)
        for wheel_name, _ in failed_wheels:
            print(f"  - {wheel_name}")
        return 1

    pruned = prune_wheels_not_for_current_python(python_version_tag)
    if pruned:
        print_color(
            f"Pruned {pruned} wheel(s) not for this matrix (Python {python_version} / "
            f"current platform) before artifact upload",
            Fore.YELLOW,
        )

    print_color("\nAll compatible wheels processed successfully!", Fore.GREEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
