#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""macOS / manylinux tag policy and the ``pip wheel`` invocation that applies it."""

from __future__ import annotations

import platform
import re
import subprocess
import sys

from pathlib import Path

from colorama import Fore
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from _helper_functions import _REPO_ROOT
from _helper_functions import DEFAULT_WHEEL_DIR
from _helper_functions import PYPI_SIMPLE_INDEX
from _helper_functions import _force_no_binary_linux_normalized
from _helper_functions import _requirement_for_pip_download
from _helper_functions import get_current_platform
from _helper_functions import is_linux_armv7_runner
from _helper_functions import manylinux_glibc_tags_in_name
from _helper_functions import parse_wheel_name
from _helper_functions import print_color

# macOS: ``pip wheel --only-binary :all:`` so CI does not sdist-compile against the
# runner SDK (higher ``macosx_*`` tags steal pip preference; local ``.so`` can SIGABRT).
# ``--no-binary cryptography`` on Intel still wins (OpenSSL 4 rebuild).
_MACOSX_PLAT_TAG = re.compile(r"macosx_(\d+)_(\d+)_([a-z0-9_]+)", re.IGNORECASE)


def get_macos_only_binary_args() -> list[str]:
    """Prefer PyPI wheels on macOS; do not sdist-build against the runner SDK."""
    if platform.system() != "Darwin":
        return []
    return ["--only-binary", ":all:"]


def _macosx_deployment_and_arch(wheel_name: str) -> tuple[tuple[int, int], str] | None:
    """Return ``((major, minor), arch)`` from a ``macosx_M_N_arch`` wheel filename."""
    match = _MACOSX_PLAT_TAG.search(wheel_name)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2))), match.group(3).lower()


def should_skip_macos_delocate_for_pypi_mirror(wheel_name: str) -> bool:
    """Skip delocate on macOS wheels except the Intel cryptography OpenSSL 4 rebuild.

    delocate on a PyPI copy can rewrite ``WHEEL`` / retag ``macosx_*`` upward so pip
    prefers the CI artifact. cryptography on Intel must still be repaired (bundled OpenSSL 4).
    """
    if platform.system() != "Darwin":
        return False
    if "macosx" not in wheel_name.lower():
        return False
    parsed = parse_wheel_name(wheel_name)
    if not parsed:
        return False
    if parsed[0] == canonicalize_name("cryptography") and get_current_platform() == "macos_x86_64":
        return False
    return True


def prune_ci_macos_newer_than_pypi_mirror(
    *,
    wheel_dir: Path | str = DEFAULT_WHEEL_DIR,
) -> int:
    """Drop locally tagged macOS wheels when a lower ``macosx_*`` sibling exists.

    Pip prefers the highest compatible ``macosx_*`` tag. A CI sdist build on
    ``macos-15-intel`` becomes ``macosx_15_0_x86_64`` and wins over the official
    ``macosx_10_9_x86_64`` PyPI copy on the same extra-index. Applies to every
    distribution (not only psutil).
    """
    dest = Path(wheel_dir)
    if not dest.is_dir():
        return 0
    groups: dict[tuple[str, str, str], list[tuple[tuple[int, int], Path]]] = {}
    for path in dest.rglob("*.whl"):
        parsed = parse_wheel_name(path.name)
        if not parsed:
            continue
        tag = _macosx_deployment_and_arch(path.name)
        if not tag:
            continue
        deployment, arch = tag
        groups.setdefault((parsed[0], parsed[1], arch), []).append((deployment, path))

    removed = 0
    for (dist, version, arch), items in groups.items():
        if len(items) < 2:
            continue
        lowest = min(deployment for deployment, _path in items)
        for deployment, path in items:
            if deployment <= lowest:
                continue
            print_color(
                f"-- removed {path.name} (macosx_{deployment[0]}_{deployment[1]}_{arch}; "
                f"keeping macosx_{lowest[0]}_{lowest[1]} for {dist}=={version})",
                Fore.YELLOW,
            )
            path.unlink()
            removed += 1
    return removed


def pip_wheel_standard_args(find_links_dir: Path | str = DEFAULT_WHEEL_DIR) -> list[str]:
    """Shared ``pip wheel`` flags used by ``build_wheels.py`` and ``build_wheels_from_file.py``."""
    wheel_dir = str(find_links_dir)
    return [
        "--find-links",
        wheel_dir,
        "--find-links",
        PYPI_SIMPLE_INDEX,
        "--wheel-dir",
        wheel_dir,
        "--no-cache-dir",
        "--no-build-isolation",
        *get_macos_only_binary_args(),
    ]


def pip_wheel_invocation_args(
    requirement_name: str,
    find_links_dir: Path | str = DEFAULT_WHEEL_DIR,
) -> list[str]:
    """``pip wheel`` flags for a single requirement (may drop ``--no-build-isolation`` on ARMv7)."""
    args = list(pip_wheel_standard_args(find_links_dir))
    if is_linux_armv7_runner():
        # cffi 2.x sdists fail metadata prep under host setuptools + --no-build-isolation
        # (project.license validation), including transitive cffi pulls (e.g. esptool → cryptography).
        # PEP 517 isolation matches build_requirements install on ARMv7 Docker.
        del requirement_name  # per-requirement env still via armv7_pip_wheel_subprocess_env()
        args = [arg for arg in args if arg != "--no-build-isolation"]
    return args


def _wheel_has_manylinux_228_tag(wheel_name: str) -> bool:
    """True when the wheel filename includes a ``manylinux_2_28`` platform tag."""
    return (2, 28) in manylinux_glibc_tags_in_name(wheel_name)


def _requirement_exact_version(req: Requirement) -> str | None:
    """Return the pinned version when the requirement uses ``==``."""
    for spec in req.specifier:
        if spec.operator == "==":
            return str(spec.version)
    return None


def _wheel_highest_manylinux_glibc(wheel_name: str) -> tuple[int, int] | None:
    """Return the highest ``manylinux_M_N`` glibc tag embedded in a wheel filename."""
    tags = manylinux_glibc_tags_in_name(wheel_name)
    return max(tags) if tags else None


def prune_ci_manylinux_newer_than_228(
    package_name: str,
    package_version: str,
    *,
    wheel_dir: Path | str = DEFAULT_WHEEL_DIR,
) -> int:
    """Drop CI-built manylinux wheels newer than ``2_28`` after mirroring PyPI ``2_28``.

    Ubuntu 24.04 rebuilds link against newer system OpenSSL (e.g. SM4 symbols) and
    auditwheel tags ``manylinux_2_34_*``. Pip prefers those over mirrored ``2_28``
    wheels when both are on the index, breaking consumers on older OpenSSL (ESP-IDF
    Docker, esptool PyInstaller on focal, etc.).
    """
    dest = Path(wheel_dir)
    canonical = canonicalize_name(package_name)
    version = str(package_version)
    removed = 0
    for path in dest.glob("*.whl"):
        parsed = parse_wheel_name(path.name)
        if not parsed or parsed[0] != canonical or parsed[1] != version:
            continue
        glibc = _wheel_highest_manylinux_glibc(path.name)
        if glibc is not None and glibc > (2, 28) and not _wheel_has_manylinux_228_tag(path.name):
            print_color(
                f"-- removed {path.name} (CI manylinux_{glibc[0]}_{glibc[1]}; PyPI 2_28 mirror kept)",
                Fore.YELLOW,
            )
            path.unlink()
            removed += 1
    return removed


def prune_ci_manylinux_newer_than_228_when_228_mirror_present(
    *,
    wheel_dir: Path | str = DEFAULT_WHEEL_DIR,
    package_names: frozenset[str] | None = None,
) -> int:
    """Drop manylinux > ``2_28`` when a ``2_28`` wheel exists for the same dist+version.

    Used after merging matrix artifacts and after ``repair_wheels`` so CI or auditwheel
    outputs cannot coexist with PyPI ``manylinux_2_28`` mirrors on the upload index.
    """
    dest = Path(wheel_dir)
    by_dist_version: dict[tuple[str, str], list[Path]] = {}
    for path in dest.rglob("*.whl"):
        parsed = parse_wheel_name(path.name)
        if not parsed:
            continue
        if package_names is not None:
            pkg_key = parsed[0].replace("-", "_")
            if pkg_key not in package_names:
                continue
        by_dist_version.setdefault(parsed, []).append(path)

    removed = 0
    for (dist, version), paths in by_dist_version.items():
        if not any(_wheel_has_manylinux_228_tag(p.name) for p in paths):
            continue
        for path in paths:
            glibc = _wheel_highest_manylinux_glibc(path.name)
            if glibc is not None and glibc > (2, 28) and not _wheel_has_manylinux_228_tag(path.name):
                print_color(
                    f"-- removed {path.name} (manylinux_{glibc[0]}_{glibc[1]}; "
                    f"keeping manylinux_2_28 for {dist}=={version})",
                    Fore.YELLOW,
                )
                path.unlink()
                removed += 1
    return removed


def should_skip_linux_auditwheel_for_pypi_mirror(wheel_name: str) -> bool:
    """Skip auditwheel on PyPI ``manylinux_2_28`` mirrors (repair retags to host ``2_34``)."""
    machine = platform.machine().lower()
    if machine not in ("x86_64", "amd64", "aarch64", "arm64"):
        return False
    parsed = parse_wheel_name(wheel_name)
    if not parsed:
        return False
    pkg_key = parsed[0].replace("-", "_")
    if pkg_key not in _force_no_binary_linux_normalized(_REPO_ROOT):
        return False
    return _wheel_has_manylinux_228_tag(wheel_name)


def _mirrored_manylinux228_version(package_name: str, wheel_dir: Path) -> str | None:
    """Return the version string of a ``manylinux_2_28`` wheel mirrored for ``package_name``."""
    canonical = canonicalize_name(package_name)
    for path in wheel_dir.glob("*.whl"):
        parsed = parse_wheel_name(path.name)
        if not parsed or parsed[0] != canonical:
            continue
        if _wheel_has_manylinux_228_tag(path.name):
            return parsed[1]
    return None


def _prune_mirrored_manylinux228_ci_builds(
    req: Requirement,
    *,
    wheel_dir: Path,
) -> int:
    """Remove CI manylinux > ``2_28`` after mirroring PyPI ``2_28`` for ``req``."""
    mirrored_version = _mirrored_manylinux228_version(req.name, wheel_dir)
    if not mirrored_version:
        mirrored_version = _requirement_exact_version(req)
    if not mirrored_version:
        return 0
    pruned = prune_ci_manylinux_newer_than_228(req.name, mirrored_version, wheel_dir=wheel_dir)
    if pruned:
        print_color(
            f"-- pruned {pruned} CI manylinux wheel(s) newer than 2_28 for {req.name}=={mirrored_version}",
            Fore.YELLOW,
        )
    return pruned


def mirror_pypi_manylinux228_wheel(
    requirement_line: str,
    *,
    wheel_dir: Path | str = DEFAULT_WHEEL_DIR,
) -> bool:
    """Download a ``manylinux_2_28`` wheel from PyPI for older glibc/OpenSSL consumers.

    CI rebuilds on Ubuntu 24.04 produce ``manylinux_2_34_*`` wheels (glibc >= 2.34,
    newer system OpenSSL). Downstream tools (ESP-IDF Docker, esptool PyInstaller on
    Ubuntu 20.04) need PyPI's ``manylinux_2_28`` wheel instead. After a successful
    mirror, locally rebuilt ``manylinux`` wheels newer than ``2_28`` for the same
    version are removed so pip cannot prefer the incompatible CI build.

    ARMv7 builds use piwheels and different manylinux lineage tags; no mirroring here.
    """
    if platform.system() != "Linux" or is_linux_armv7_runner():
        return False
    machine = platform.machine().lower()
    pip_platform = {
        "x86_64": "manylinux_2_28_x86_64",
        "amd64": "manylinux_2_28_x86_64",
        "aarch64": "manylinux_2_28_aarch64",
        "arm64": "manylinux_2_28_aarch64",
    }.get(machine)
    if not pip_platform:
        return False

    line = requirement_line.strip()
    if not line:
        return False
    try:
        req = Requirement(line)
    except InvalidRequirement:
        return False

    download_line = _requirement_for_pip_download(req)

    pkg_key = canonicalize_name(req.name).replace("-", "_")
    if pkg_key not in _force_no_binary_linux_normalized(_REPO_ROOT):
        return False

    py_major, py_minor = sys.version_info[:2]
    dest = Path(wheel_dir)
    dest.mkdir(exist_ok=True)
    cp_abi = f"cp{py_major}{py_minor}"
    abi_candidates = [cp_abi, "abi3"] if cp_abi != "abi3" else ["abi3"]

    for abi in abi_candidates:
        out = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                download_line,
                "--only-binary",
                ":all:",
                "--dest",
                str(dest),
                "--no-deps",
                "--platform",
                pip_platform,
                "--python-version",
                f"{py_major}.{py_minor}",
                "--implementation",
                "cp",
                "--abi",
                abi,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if out.stdout:
            print(out.stdout.decode("utf-8", errors="replace"))
        if out.returncode == 0:
            print_color(
                f"-- mirrored {pip_platform} wheel for {download_line} from PyPI (abi={abi}; older glibc consumers)",
                Fore.YELLOW,
            )
            _prune_mirrored_manylinux228_ci_builds(req, wheel_dir=dest)
            return True

    if out.stderr:
        print_color(out.stderr.decode("utf-8", errors="replace"), Fore.YELLOW)
    print_color(
        f"-- could not mirror {pip_platform} wheel for {download_line} from PyPI",
        Fore.YELLOW,
    )
    return False


# Backward-compatible alias (same behavior; not cryptography-specific).
mirror_pypi_manylinux228_x86_64_wheel = mirror_pypi_manylinux228_wheel


def pip_wheel_or_mirror_success(
    requirement_line: str,
    pip_returncode: int,
    *,
    wheel_dir: Path | str = DEFAULT_WHEEL_DIR,
) -> bool:
    """True when ``pip wheel`` succeeded or a PyPI ``manylinux_2_28`` mirror was fetched."""
    if pip_returncode == 0:
        mirror_pypi_manylinux228_wheel(requirement_line, wheel_dir=wheel_dir)
        prune_ci_macos_newer_than_pypi_mirror(wheel_dir=wheel_dir)
        return True
    mirrored = mirror_pypi_manylinux228_wheel(requirement_line, wheel_dir=wheel_dir)
    prune_ci_macos_newer_than_pypi_mirror(wheel_dir=wheel_dir)
    return mirrored
