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
import subprocess
import sys
import zipfile

from dataclasses import dataclass
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

import yaml

from colorama import Fore
from colorama import Style
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import InvalidWheelFilename
from packaging.utils import canonicalize_name
from packaging.utils import parse_wheel_filename
from packaging.version import InvalidVersion
from packaging.version import Version
from packaging.version import parse as parse_version

# Linux ``--no-binary`` names: one per line in ``force_no_binary_linux.txt``
# (x86_64/aarch64 Linux only; ARMv7 uses piwheels).

_REPO_ROOT = Path(__file__).resolve().parent
FORCE_NO_BINARY_LINUX_FILE = "force_no_binary_linux.txt"

EXCLUDE_LIST_PATH = "exclude_list.yaml"
NATIVE_IMPORT_GUARD_PATH = "native_import_guard.yaml"
PYPI_SIMPLE_INDEX = "https://pypi.org/simple/"
DEFAULT_WHEEL_DIR = "downloaded_wheels"

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


def is_linux_armv7_runner() -> bool:
    """True on Linux ARMv7 wheel builds (Docker ``armv7l`` / ``armhf``)."""
    return platform.system() == "Linux" and platform.machine().lower() in ("armv7l", "armv7", "armhf")


# Piwheels ``linux_armv7l`` / manylinux wheels for ``cffi`` and ``argon2-cffi-bindings`` may link
# against libffi/glibc newer than the lineage image. Rebuild those from sdists in-container.
# ``cryptography`` stays on piwheels (Rust/maturin sdist rebuild is impractical under ARMv7 QEMU).
# Bookworm native-import probes skip cryptography (piwheels 49+ vs image OpenSSL); see test_wheels_install.
ARMV7_FORCE_NO_BINARY_PACKAGES = frozenset(
    {
        canonicalize_name("cffi"),
        canonicalize_name("argon2-cffi-bindings"),
    }
)

# Applied in ``linux_armv7_docker_prepare.sh`` for all pip invocations (build_requirements, etc.).
# Excludes cryptography: forcing it globally breaks transitive deps on Python 3.8 (maturin sdist).
ARMV7_PIP_NO_BINARY_GLOBAL_CSV = "cffi,argon2-cffi-bindings"
# Same set for explicit ``pip wheel`` rebuilds of cffi / argon2 on ARMv7.
ARMV7_PIP_NO_BINARY_CSV = ARMV7_PIP_NO_BINARY_GLOBAL_CSV


def armv7_force_no_binary_package(name: str) -> bool:
    """True if ARMv7 Docker builds must compile this package from sdist (not piwheels)."""
    return canonicalize_name(name) in ARMV7_FORCE_NO_BINARY_PACKAGES


def armv7_rebuild_instead_of_find_links_skip(name: str, find_links_reason: str) -> bool:
    """True when find-links has a matching piwheels wheel that must be rebuilt in-lineage."""
    if not is_linux_armv7_runner() or not armv7_force_no_binary_package(name):
        return False
    return "already has" in find_links_reason and "matching" in find_links_reason


def armv7_pip_wheel_subprocess_env(requirement_name: str) -> dict[str, str]:
    """Return env for ``pip wheel`` on ARMv7 (extends global ``PIP_NO_BINARY`` when rebuilding natives)."""
    env = os.environ.copy()
    if not is_linux_armv7_runner():
        return env
    match = re.match(r"^([a-zA-Z0-9_-]+)", str(requirement_name).strip())
    if match and armv7_force_no_binary_package(match.group(1)):
        env["PIP_NO_BINARY"] = ARMV7_PIP_NO_BINARY_CSV
    return env


def remove_find_links_wheels_for_package(
    name: str,
    find_links_dir: Path | str = "downloaded_wheels",
) -> int:
    """Remove existing wheels for ``name`` under find-links (before ARMv7 sdist rebuild)."""
    links = Path(find_links_dir)
    if not links.is_dir():
        return 0
    canonical = canonicalize_name(name)
    removed = 0
    for path in links.glob("*.whl"):
        parsed = parse_wheel_name(path.name)
        if parsed and parsed[0] == canonical:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


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
# force_no_binary_linux.txt: resolved repo root -> (names, normalized names for lookup)
_FORCE_NO_BINARY_LINUX_CACHE: Dict[Path, Tuple[List[str], frozenset[str]]] = {}


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


def load_force_no_binary_linux_names(repo_root: Path | None = None) -> list[str]:
    """Package names for Linux ``--no-binary`` / ``PIP_NO_BINARY`` (``force_no_binary_linux.txt``)."""
    root = (repo_root if repo_root is not None else _REPO_ROOT).resolve()
    cached = _FORCE_NO_BINARY_LINUX_CACHE.get(root)
    if cached is not None:
        return cached[0]
    path = root / FORCE_NO_BINARY_LINUX_FILE
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    if not out:
        raise ValueError(f"{path}: need at least one non-comment package name")
    normalized = frozenset(pkg.lower().replace("-", "_") for pkg in out)
    _FORCE_NO_BINARY_LINUX_CACHE[root] = (out, normalized)
    return out


@dataclass(frozen=True)
class NativeImportGuardEntry:
    """Import probes for native ARMv7 wheels (``test_wheels_install.py``)."""

    imports: tuple[str, ...]


_NATIVE_IMPORT_GUARD_CACHE: dict[Path, dict[str, NativeImportGuardEntry]] = {}


def native_import_guard_by_name(
    repo_root: Path | None = None,
) -> dict[str, NativeImportGuardEntry]:
    """Map canonical distribution name → guard config (``native_import_guard.yaml``)."""
    root = (repo_root if repo_root is not None else _REPO_ROOT).resolve()
    cached = _NATIVE_IMPORT_GUARD_CACHE.get(root)
    if cached is not None:
        return cached
    path = root / NATIVE_IMPORT_GUARD_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    by_name: dict[str, NativeImportGuardEntry] = {}
    for entry in data.get("packages") or []:
        if not isinstance(entry, dict):
            continue
        name, imports = entry.get("name"), entry.get("imports")
        if not name or not imports:
            continue
        stmts = tuple(str(s).strip() for s in imports if str(s).strip())
        if not stmts:
            continue
        by_name[canonicalize_name(str(name))] = NativeImportGuardEntry(imports=stmts)
    if not by_name:
        raise ValueError(f"{path}: no packages with imports defined")
    _NATIVE_IMPORT_GUARD_CACHE[root] = by_name
    return by_name


_MANYLINUX_GLIBC_TAG = re.compile(r"manylinux_(\d+)_(\d+)", re.IGNORECASE)


def manylinux_glibc_tags_in_name(name: str) -> list[tuple[int, int]]:
    """Parse ``manylinux_M_N`` glibc levels from a wheel filename or plat string."""
    return [(int(major), int(minor)) for major, minor in _MANYLINUX_GLIBC_TAG.findall(name)]


def _is_linux_tag_armv7_wheel_name(wheel_name: str) -> bool:
    wn = wheel_name.lower()
    return "-linux_" in wn and "manylinux" not in wn and "musllinux" not in wn


def armv7_wheel_matches_forced_plat(
    wheel_name: str,
    plat: str,
    *,
    only_plat: bool,
    repair: bool = False,
) -> bool:
    """True if ``wheel_name`` matches ``AUDITWHEEL_PLAT`` for ARMv7 / ARMv7 Legacy splits."""
    wn = wheel_name.lower()
    if _is_linux_tag_armv7_wheel_name(wn):
        return "armv7l" in wn
    plat_tags = manylinux_glibc_tags_in_name(plat)
    wheel_tags = manylinux_glibc_tags_in_name(wn)
    if not plat_tags:
        return True
    required = plat_tags[0]
    if required not in wheel_tags:
        return False
    if not only_plat:
        return True
    has_31 = (2, 31) in wheel_tags
    has_36 = (2, 36) in wheel_tags
    if repair:
        # ``repair_wheels.py``: auditwheel may emit compat 2_31 tags alongside 2_36.
        if required == (2, 31) and has_36:
            return False
        return True
    # ``test_wheels_install.py``: reject dual 2_31 + 2_36 tags on Legacy (2_31) tests — those
    # are Bookworm auditwheel compat filenames. Bookworm (2_36) tests accept them when 2_36 is present.
    if has_31 and has_36:
        return required == (2, 36)
    if required == (2, 31) and has_36:
        return False
    return True


def _force_no_binary_linux_normalized(repo_root: Path | None = None) -> frozenset[str]:
    """
    Normalized package names from ``force_no_binary_linux.txt``
    (cached with ``load_force_no_binary_linux_names``).
    """
    root = (repo_root if repo_root is not None else _REPO_ROOT).resolve()
    cached = _FORCE_NO_BINARY_LINUX_CACHE.get(root)
    if cached is not None:
        return cached[1]
    load_force_no_binary_linux_names(root)
    return _FORCE_NO_BINARY_LINUX_CACHE[root][1]


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

    # ARMv7 CI uses piwheels for most packages; force sdists for native stacks piwheels
    # mis-builds for Bullseye / Bookworm lineages (see ``armv7_force_no_binary_package``).
    if is_linux_armv7_runner():
        if armv7_force_no_binary_package(match.group(1)):
            return ["--no-binary", match.group(1)]
        return []

    pkg_name = match.group(1).lower().replace("-", "_")

    if pkg_name in _force_no_binary_linux_normalized(_REPO_ROOT):
        return ["--no-binary", match.group(1)]
    return []


# Do not pass ``--no-binary`` for these in ``build_wheels_from_file --force-interpreter-binary``:
# legacy sdists, PyObjC, heavy Rust/maturin stacks, and ARMv7 CFFI-backed wheels where
# forced source builds fail under QEMU (libffi / glibc in pip's isolated build env).
FORCE_INTERPRETER_BINARY_SKIP_EXACT = frozenset(
    {
        canonicalize_name("argon2-cffi-bindings"),
        canonicalize_name("cryptography"),
        canonicalize_name("protobuf"),
        canonicalize_name("pydantic-core"),
        canonicalize_name("pynacl"),
        canonicalize_name("rpds-py"),
        canonicalize_name("ruamel.yaml.clib"),
        canonicalize_name("tibs"),
    }
)


def force_interpreter_skip_package(canonical_dist_name: str) -> bool:
    if canonical_dist_name in FORCE_INTERPRETER_BINARY_SKIP_EXACT:
        return True
    return canonical_dist_name == "pyobjc" or canonical_dist_name.startswith("pyobjc-")


def get_cryptography_macos_intel_pip_wheel_args(requirement_name: str) -> list[str]:
    """``--no-binary cryptography`` on macOS Intel (OpenSSL 4 sdist build in CI)."""
    if get_current_platform() != "macos_x86_64":
        return []
    match = re.match(r"^([a-zA-Z0-9_.-]+)", str(requirement_name).strip())
    if not match:
        return []
    if canonicalize_name(match.group(1)) != canonicalize_name("cryptography"):
        return []
    return ["--no-binary", "cryptography"]


def bounded_pin_without_find_links_skip(
    req: Requirement,
    find_links_dir: Path | str = "downloaded_wheels",
) -> Tuple[bool, str]:
    """Defer bounded pins on Linux for packages that must not be built from sdists.

    Merged IDF requirements list ``cryptography<46.1`` before an unconstrained
    ``cryptography`` line. Until find-links has any wheel for the package, ``pip wheel``
    with ``--no-binary`` (see ``force_no_binary_linux.txt``) downloads an old maturin sdist
    and fails with ``BackendUnavailable`` on Python 3.8+.
    """
    if platform.system() != "Linux":
        return False, ""
    if not force_interpreter_skip_package(canonicalize_name(req.name)):
        return False, ""
    links = Path(find_links_dir)
    if not links.is_dir():
        return False, ""
    canonical = canonicalize_name(req.name)
    has_pkg_wheel = False
    for path in links.glob("*.whl"):
        parsed = parse_wheel_name(path.name)
        if parsed and parsed[0] == canonical:
            has_pkg_wheel = True
            break
    if has_pkg_wheel:
        return False, ""
    if not req.specifier:
        return False, ""
    has_upper = any(s.operator in ("<", "<=") for s in req.specifier)
    has_exact = any(s.operator == "==" for s in req.specifier)
    if has_upper and not has_exact:
        return (
            True,
            f"bounded pin for {req.name} without find-links wheel (avoid maturin sdist build)",
        )
    return False, ""


def armv7_bounded_pin_without_find_links_skip(
    req: Requirement,
    find_links_dir: Path | str = "downloaded_wheels",
) -> Tuple[bool, str]:
    """Alias for :func:`bounded_pin_without_find_links_skip` (ARMv7 + std Linux)."""
    return bounded_pin_without_find_links_skip(req, find_links_dir)


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


_MANYLINUX_GLIBC_TAG_RE = re.compile(r"manylinux_(\d+)_(\d+)_")


def _wheel_has_manylinux_228_tag(wheel_name: str) -> bool:
    """True when the wheel filename includes a ``manylinux_2_28`` platform tag."""
    try:
        _name, _version, _build, tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        return False
    for tag in tags:
        plat = str(getattr(tag, "platform", tag))
        for match in _MANYLINUX_GLIBC_TAG_RE.finditer(plat):
            if (int(match.group(1)), int(match.group(2))) == (2, 28):
                return True
    return False


def _requirement_exact_version(req: Requirement) -> str | None:
    """Return the pinned version when the requirement uses ``==``."""
    for spec in req.specifier:
        if spec.operator == "==":
            return str(spec.version)
    return None


def _wheel_highest_manylinux_glibc(wheel_name: str) -> tuple[int, int] | None:
    """Return the highest ``manylinux_M_N`` glibc tag embedded in a wheel filename."""
    try:
        _name, _version, _build, tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        return None
    highest: tuple[int, int] | None = None
    for tag in tags:
        plat = str(getattr(tag, "platform", tag))
        for match in _MANYLINUX_GLIBC_TAG_RE.finditer(plat):
            candidate = (int(match.group(1)), int(match.group(2)))
            if highest is None or candidate > highest:
                highest = candidate
    return highest


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
                line,
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
                f"-- mirrored {pip_platform} wheel for {line} from PyPI (abi={abi}; older glibc consumers)",
                Fore.YELLOW,
            )
            _prune_mirrored_manylinux228_ci_builds(req, wheel_dir=dest)
            return True

    if out.stderr:
        print_color(out.stderr.decode("utf-8", errors="replace"), Fore.YELLOW)
    print_color(
        f"-- could not mirror {pip_platform} wheel for {line} from PyPI",
        Fore.YELLOW,
    )
    return False


# Backward-compatible alias (same behavior; not cryptography-specific).
mirror_pypi_manylinux228_x86_64_wheel = mirror_pypi_manylinux228_wheel


def _find_links_versions_too_new_for_pin(versions: list[Version], req: Requirement) -> bool:
    """True when every find-links wheel is excluded by an upper bound (obsolete pin; skip sdist)."""
    if not req.specifier:
        return False
    oldest = min(versions)
    for spec in req.specifier:
        if spec.operator not in ("<", "<="):
            continue
        try:
            bound = Version(spec.version)
        except InvalidVersion:
            continue
        if spec.operator == "<" and oldest >= bound:
            return True
        if spec.operator == "<=" and oldest > bound:
            return True
    return False


def pip_wheel_or_mirror_success(
    requirement_line: str,
    pip_returncode: int,
    *,
    wheel_dir: Path | str = DEFAULT_WHEEL_DIR,
) -> bool:
    """True when ``pip wheel`` succeeded or a PyPI ``manylinux_2_28`` mirror was fetched."""
    if pip_returncode == 0:
        mirror_pypi_manylinux228_wheel(requirement_line, wheel_dir=wheel_dir)
        return True
    return mirror_pypi_manylinux228_wheel(requirement_line, wheel_dir=wheel_dir)


def find_links_wheel_build_skip(
    req: Requirement,
    find_links_dir: Path | str = DEFAULT_WHEEL_DIR,
) -> Tuple[bool, str]:
    """Skip ``pip wheel`` when find-links already satisfies the pin or only an sdist would work.

    Merged IDF requirements can list several cryptography pins. Stage-1 may already have
    ``cryptography-47.0.0`` in ``downloaded_wheels`` while a later line says ``cryptography<45``.
    Pip then resolves an older sdist (maturin) and fails with ``BackendUnavailable``.

    When find-links wheels are older than a lower-bound pin (e.g. ``>=49`` with only 47 present),
    do **not** skip so pip can fetch a newer wheel from PyPI.
    """
    links = Path(find_links_dir)
    if not links.is_dir():
        return False, ""
    canonical = canonicalize_name(req.name)
    versions: list[Version] = []
    for path in links.glob("*.whl"):
        parsed = parse_wheel_name(path.name)
        if not parsed or parsed[0] != canonical:
            continue
        try:
            versions.append(Version(parsed[1]))
        except InvalidVersion:
            continue
    if not versions:
        return False, ""
    matching = [v for v in versions if req.specifier.contains(v, prereleases=True)]
    if matching:
        best = max(matching)
        return True, f"find-links already has {req.name} {best} matching {req.specifier}"
    newest = max(versions)
    for spec in req.specifier:
        if spec.operator != "==":
            continue
        try:
            pinned = Version(spec.version)
        except InvalidVersion:
            continue
        if newest > pinned and not any(s.operator in ("<", "<=") for s in req.specifier):
            return (
                True,
                f"find-links has {req.name} {newest} newer than obsolete pin =={spec.version}",
            )
    if _find_links_versions_too_new_for_pin(versions, req):
        return (
            True,
            f"find-links has {req.name} up to {newest} but none match {req.specifier}",
        )
    return False, ""


def get_pip_wheel_extra_args(requirement_name: str) -> list[str]:
    """Extra ``pip wheel`` CLI flags for packages with known CI build quirks.

    ``bleak-winrt`` 1.2.0 uses legacy ``scikit-build``, whose MSVC probe only accepts
    the compiler version band for the generator it tries (e.g. 1930–1949 for VS 2022).
    GHA ``windows-latest`` can expose VS 2025 (MSVC 19.50) by default, which fails that
    probe when building from the sdist under PEP 517 isolation. Use the host env
    (after ``setup-msvc-dev`` on ``windows-2022`` with VS 17.x) and ``--no-build-isolation``.
    """
    match = re.match(r"^([a-zA-Z0-9_.-]+)", str(requirement_name).strip())
    if not match:
        return []
    canonical = canonicalize_name(match.group(1))
    if platform.system() == "Windows" and canonical == canonicalize_name("bleak-winrt"):
        return ["--no-build-isolation"]
    if is_linux_armv7_runner() and canonical == canonicalize_name("cryptography"):
        # piwheels ships cryptography wheels; avoid re-wheeling cffi sdists (license metadata failure).
        return ["--no-deps"]
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
        tuple: (canonical distribution name, version_str) or None if parsing fails
    """
    try:
        name, version, _build, _tags = parse_wheel_filename(wheel_name)
        return canonicalize_name(str(name)), str(version)
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

    canonical_name, wheel_version = parsed

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
        - "pkg-1.0-cp39-abi3-manylinux_2_31_armv7l.whl" -> "3.9"
        - "pkg-1.0-py3-none-any.whl" -> None (universal)
    """
    match = re.search(r"-cp(\d)(\d+)-", wheel_name)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    match = re.search(r"-cp(\d{2})-abi3-", wheel_name)
    if match:
        tag = match.group(1)
        return f"{int(tag[0])}.{tag[1:]}"
    return None


def get_wheel_linux_arch(wheel_name: str) -> str | None:
    """Return ``linux_armv7`` / ``linux_arm64`` / ``linux_x86_64`` from platform tags, or None."""
    try:
        _name, _version, _build, tags = parse_wheel_filename(wheel_name)
    except InvalidWheelFilename:
        return None
    for tag in tags:
        pt = tag.platform.lower()
        if "armv7l" in pt:
            return "linux_armv7"
        if "aarch64" in pt:
            return "linux_arm64"
        if "x86_64" in pt:
            return "linux_x86_64"
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

    canonical_name, wheel_version = parsed
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
