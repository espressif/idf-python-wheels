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
from packaging.utils import InvalidWheelFilename
from packaging.utils import canonicalize_name
from packaging.utils import parse_wheel_filename

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

# macOS: ``pip wheel --only-binary :all:`` first so CI does not sdist-compile against
# the runner SDK (higher ``macosx_*`` tags steal pip preference; local ``.so`` can SIGABRT).
# Retry without that flag when a requirement or dependency has no binary wheel.
# ``--no-binary cryptography`` on Intel still wins (OpenSSL 4 rebuild) and is not pruned.
_MACOSX_PLAT_TAG = re.compile(r"macosx_(\d+)_(\d+)_([a-z0-9_]+)", re.IGNORECASE)
# PEP 425: ``cp`` + one-digit major + remaining digits as minor (``cp38``, ``cp310``).
_CPYTHON_INTERP = re.compile(r"^cp(?P<major>\d)(?P<minor>\d{1,})$")


def _wheel_python_abi_key(wheel_name: str) -> tuple[str, str] | None:
    """Sorted interpreter / ABI tags from a wheel filename (compressed sets joined)."""
    try:
        _name, _version, _build, tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        return None
    interpreters = tuple(sorted({tag.interpreter for tag in tags}))
    abis = tuple(sorted({tag.abi for tag in tags}))
    if not interpreters:
        return None
    return ",".join(interpreters), ",".join(abis)


def _wheel_cpython_ranges(wheel_name: str) -> list[tuple[tuple[int, int], tuple[int, int] | None]]:
    """CPython versions a wheel can serve: ``(min, max)`` with ``max`` None meaning unbounded."""
    try:
        _name, _version, _build, tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        return []
    ranges: list[tuple[tuple[int, int], tuple[int, int] | None]] = []
    seen: set[tuple[tuple[int, int], tuple[int, int] | None]] = set()
    for tag in tags:
        match = _CPYTHON_INTERP.fullmatch(tag.interpreter)
        if not match:
            continue
        ver = (int(match.group("major")), int(match.group("minor")))
        if tag.abi == "abi3":
            item: tuple[tuple[int, int], tuple[int, int] | None] = (ver, None)
        elif tag.abi == tag.interpreter or tag.abi.startswith("cp"):
            item = (ver, ver)
        else:
            continue
        if item not in seen:
            seen.add(item)
            ranges.append(item)
    return ranges


def _cpython_ranges_overlap(
    left: list[tuple[tuple[int, int], tuple[int, int] | None]],
    right: list[tuple[tuple[int, int], tuple[int, int] | None]],
) -> bool:
    for a_min, a_max in left:
        for b_min, b_max in right:
            start = max(a_min, b_min)
            ends = [bound for bound in (a_max, b_max) if bound is not None]
            if not ends or start <= min(ends):
                return True
    return False


def _wheels_compete_for_same_cpython(name_a: str, name_b: str) -> bool:
    """True when pip could consider both wheels for the same CPython interpreter.

    Exclusive ABIs (``cp311-cp311`` vs ``cp312-cp312``) do not compete. ``abi3``
    wheels compete with each other and with exclusive wheels they can serve, so a
    ``cp36-abi3`` PyPI tag still wins over a CI ``cp311-cp311`` sibling.
    """
    left = _wheel_cpython_ranges(name_a)
    right = _wheel_cpython_ranges(name_b)
    if left and right:
        return _cpython_ranges_overlap(left, right)
    return _wheel_python_abi_key(name_a) == _wheel_python_abi_key(name_b)


def get_macos_only_binary_args() -> list[str]:
    """Prefer PyPI wheels on macOS; do not sdist-build against the runner SDK."""
    if platform.system() != "Darwin":
        return []
    return ["--only-binary", ":all:"]


def without_only_binary_all(args: list[str]) -> list[str] | None:
    """Return ``args`` without ``--only-binary :all:``, or ``None`` if that flag is absent."""
    out: list[str] = []
    found = False
    i = 0
    while i < len(args):
        if args[i] == "--only-binary" and i + 1 < len(args) and args[i + 1] == ":all:":
            found = True
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out if found else None


def run_pip_wheel(
    requirement: str,
    extra_args: list[str] | None = None,
    *,
    find_links_dir: Path | str = DEFAULT_WHEEL_DIR,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``pip wheel``. On macOS, retry without ``--only-binary :all:`` if binaries are missing."""
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "wheel",
        requirement,
        *pip_wheel_invocation_args(find_links_dir),
        *(extra_args or []),
    ]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if out.returncode == 0:
        return out
    fallback = without_only_binary_all(cmd)
    if fallback is None:
        return out
    print_color(
        f"-- retry {requirement} without --only-binary :all: (no binary wheel for this requirement or a dependency)",
        Fore.YELLOW,
    )
    return subprocess.run(fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def _iter_unique_wheels(dest: Path):
    """Yield ``*.whl`` under ``dest`` once per inode (``rglob`` can list symlink twins)."""
    seen: set[object] = set()
    for path in dest.rglob("*.whl"):
        try:
            if not path.is_file():
                continue
            st = path.stat()
            # Windows FAT (and some NTFS views) report ``st_ino == 0`` for every file.
            key: object = (st.st_dev, st.st_ino) if st.st_ino else path.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        yield path


def _macos_arch_families(arch: str) -> frozenset[str]:
    """Machine families a ``macosx_*_<arch>`` tag can serve (``universal2`` is both)."""
    token = arch.lower()
    if token in {"universal2", "universal", "fat64"}:
        return frozenset({"intel", "arm"})
    if token in {"x86_64", "amd64", "intel", "i386", "i686"}:
        return frozenset({"intel"})
    if token in {"arm64", "aarch64", "arm64e"}:
        return frozenset({"arm"})
    return frozenset({token})


def _macos_archs_compete(arch_a: str, arch_b: str) -> bool:
    return bool(_macos_arch_families(arch_a) & _macos_arch_families(arch_b))


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
    distribution (not only psutil). Only compares wheels that compete for the
    same CPython (python+ABI family) and the same machine family (``x86_64`` /
    ``intel`` / ``universal2``, or ``arm64`` / ``universal2``), so ``cp311-cp311``
    is not dropped just because ``cp312-cp312`` has a lower tag. Intel
    cryptography OpenSSL 4 rebuilds are kept even when a lower PyPI tag exists.
    """
    dest = Path(wheel_dir)
    if not dest.is_dir():
        return 0
    keep_intel_cryptography = get_current_platform() == "macos_x86_64"
    groups: dict[tuple[str, str], list[tuple[tuple[int, int], str, Path]]] = {}
    for path in _iter_unique_wheels(dest):
        parsed = parse_wheel_name(path.name)
        if not parsed:
            continue
        tag = _macosx_deployment_and_arch(path.name)
        if not tag:
            continue
        deployment, arch = tag
        groups.setdefault((parsed[0], parsed[1]), []).append((deployment, arch, path))

    removed = 0
    for (dist, version), items in groups.items():
        if keep_intel_cryptography and dist == canonicalize_name("cryptography"):
            continue
        if len(items) < 2:
            continue
        for deployment, arch, path in items:
            lower_siblings = [
                other_dep
                for other_dep, other_arch, other in items
                if other_dep < deployment
                and _macos_archs_compete(arch, other_arch)
                and _wheels_compete_for_same_cpython(path.name, other.name)
            ]
            if not lower_siblings:
                continue
            keep_tag = min(lower_siblings)
            print_color(
                f"-- removed {path.name} (macosx_{deployment[0]}_{deployment[1]}_{arch}; "
                f"keeping macosx_{keep_tag[0]}_{keep_tag[1]} for {dist}=={version})",
                Fore.YELLOW,
            )
            path.unlink(missing_ok=True)
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
    find_links_dir: Path | str = DEFAULT_WHEEL_DIR,
) -> list[str]:
    """``pip wheel`` flags (may drop ``--no-build-isolation`` on ARMv7)."""
    args = list(pip_wheel_standard_args(find_links_dir))
    if is_linux_armv7_runner():
        # cffi 2.x sdists fail metadata prep under host setuptools + --no-build-isolation
        # (project.license validation), including transitive cffi pulls (e.g. esptool → cryptography).
        # PEP 517 isolation matches build_requirements install on ARMv7 Docker.
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
    candidates: list[Path] = []
    for path in dest.glob("*.whl"):
        parsed = parse_wheel_name(path.name)
        if not parsed or parsed[0] != canonical or parsed[1] != version:
            continue
        candidates.append(path)
    mirrors = [path for path in candidates if _wheel_has_manylinux_228_tag(path.name)]
    removed = 0
    for path in candidates:
        glibc = _wheel_highest_manylinux_glibc(path.name)
        if glibc is None or glibc <= (2, 28) or _wheel_has_manylinux_228_tag(path.name):
            continue
        if mirrors and not any(_wheels_compete_for_same_cpython(path.name, mirror.name) for mirror in mirrors):
            continue
        print_color(
            f"-- removed {path.name} (CI manylinux_{glibc[0]}_{glibc[1]}; PyPI 2_28 mirror kept)",
            Fore.YELLOW,
        )
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def prune_ci_manylinux_newer_than_228_when_228_mirror_present(
    *,
    wheel_dir: Path | str = DEFAULT_WHEEL_DIR,
    package_names: frozenset[str] | None = None,
) -> int:
    """Drop manylinux > ``2_28`` when a competing ``2_28`` wheel exists.

    Used after merging matrix artifacts and after ``repair_wheels`` so CI or auditwheel
    outputs cannot coexist with PyPI ``manylinux_2_28`` mirrors on the upload index.
    A ``2_28`` wheel for one exclusive ABI does not prune ``>2_28`` wheels for
    another interpreter; ``abi3`` mirrors still prune competing newer tags.
    """
    dest = Path(wheel_dir)
    by_dist_version: dict[tuple[str, str], list[Path]] = {}
    for path in _iter_unique_wheels(dest):
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
        mirrors = [path for path in paths if _wheel_has_manylinux_228_tag(path.name)]
        if not mirrors:
            continue
        for path in paths:
            glibc = _wheel_highest_manylinux_glibc(path.name)
            if glibc is None or glibc <= (2, 28) or _wheel_has_manylinux_228_tag(path.name):
                continue
            if not any(_wheels_compete_for_same_cpython(path.name, mirror.name) for mirror in mirrors):
                continue
            print_color(
                f"-- removed {path.name} (manylinux_{glibc[0]}_{glibc[1]}; "
                f"keeping manylinux_2_28 for {dist}=={version})",
                Fore.YELLOW,
            )
            path.unlink(missing_ok=True)
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
