#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Compute and emit sdist requirements for the Espressif PyPI simple index (PEP 503).

Packages in the assembled IDF dependency tree that have **no buildable wheel path** for **at least one**
supported platform/Python combination (after applying exclude_list.yaml merge rules) get a source
distribution on the index so pip can fall back when no compatible wheel exists for the current environment.
"""

from __future__ import annotations

import json
import sys

from pathlib import Path
from typing import Iterable

from colorama import Fore
from packaging.requirements import Requirement
from packaging.specifiers import Specifier
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

from _helper_functions import EXCLUDE_LIST_PATH
from _helper_functions import print_color
from yaml_list_adapter import YAMLListAdapter
from yaml_list_adapter import _platform_for_marker

# Platforms used in exclude_list.yaml (same names as get_current_platform()).
SDIST_EVAL_PLATFORMS = (
    "windows",
    "macos",
    "linux",
    "linux_x86_64",
    "linux_arm64",
    "linux_armv7",
    "macos_x86_64",
    "macos_arm64",
)

SDIST_REQUIREMENTS_FILE = "sdist_requirements.txt"
_SUPPORTED_VERSIONS_JSON = Path(__file__).resolve().parent / "supported_versions.json"


def _safe_version(version_str: str) -> Version | None:
    try:
        return Version(version_str)
    except Exception:
        return None


def _bump_release(version: Version) -> Version | None:
    release = version.release
    if len(release) >= 3:
        return _safe_version(f"{release[0]}.{release[1]}.{release[2] + 1}")
    if len(release) == 2:
        return _safe_version(f"{release[0]}.{release[1] + 1}.0")
    if len(release) == 1:
        return _safe_version(f"{release[0] + 1}.0.0")
    return None


def _drop_release(version: Version) -> Version | None:
    release = version.release
    if len(release) >= 3 and release[2] > 0:
        return _safe_version(f"{release[0]}.{release[1]}.{release[2] - 1}")
    if len(release) >= 2 and release[1] > 0:
        return _safe_version(f"{release[0]}.{release[1] - 1}.0")
    if len(release) >= 1 and release[0] > 0:
        return _safe_version(f"{release[0] - 1}.0.0")
    return None


def _probes_from_specifier(spec: Specifier) -> list[Version]:
    """Candidate versions derived from one PEP 440 specifier bound."""
    base = _safe_version(spec.version)
    if base is None:
        return []

    probes = [base]
    if spec.operator == ">":
        bumped = _bump_release(base)
        if bumped is not None:
            probes.append(bumped)
    elif spec.operator == "<":
        dropped = _drop_release(base)
        if dropped is not None:
            probes.append(dropped)
    return probes


def _overlap_probe_versions(*spec_sets: SpecifierSet) -> list[Version]:
    """Build version candidates to test whether specifier sets share any allowed version."""
    seen: set[Version] = set()
    ordered: list[Version] = []
    for spec_set in spec_sets:
        for spec in spec_set:
            for version in _probes_from_specifier(spec):
                if version not in seen:
                    seen.add(version)
                    ordered.append(version)
    for sentinel in (_safe_version("0"), _safe_version("9999")):
        if sentinel is not None and sentinel not in seen:
            seen.add(sentinel)
            ordered.append(sentinel)
    return ordered


def _version_buildable(assembled_req: Requirement, after_req: Requirement) -> bool:
    """True if some version allowed by assembled_req is still built per after_req specifier."""
    if not assembled_req.specifier:
        return True
    if not after_req.specifier:
        return True
    a_set = SpecifierSet(str(assembled_req.specifier))
    b_set = SpecifierSet(str(after_req.specifier))
    for version in _overlap_probe_versions(a_set, b_set):
        if version in a_set and version in b_set:
            return True
    return False


def _load_supported_python_versions() -> list[str]:
    if _SUPPORTED_VERSIONS_JSON.is_file():
        data = json.loads(_SUPPORTED_VERSIONS_JSON.read_text(encoding="utf-8"))
        versions = data.get("supported_python")
        if isinstance(versions, list):
            return [str(v) for v in versions]
    return ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]


def _marker_env(platform: str, python_version: str) -> dict[str, str]:
    sys_plat = _platform_for_marker(platform)
    if sys_plat == "win32":
        return {
            "sys_platform": "win32",
            "platform_system": "Windows",
            "os_name": "nt",
            "python_version": python_version,
        }
    if sys_plat == "darwin":
        return {
            "sys_platform": "darwin",
            "platform_system": "Darwin",
            "os_name": "posix",
            "python_version": python_version,
        }
    return {
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "python_version": python_version,
    }


def _requirement_applies_to_env(req: Requirement, platform: str, python_version: str) -> bool:
    """True if ``req`` (including its marker) applies to the evaluated environment."""
    if req.marker is None:
        return True
    return bool(req.marker.evaluate(_marker_env(platform, python_version)))


def _can_build_wheel_on_platform(
    assembled_req: Requirement,
    after_set: set[Requirement],
    platform: str,
    python_version: str,
) -> bool:
    """True if exclude merge still allows building the assembled pin on this platform/Python.

    Caller must only invoke this for assembled requirements that apply to the environment
    (see ``_requirement_applies_to_env``).
    """
    name = canonicalize_name(assembled_req.name)
    matching = [r for r in after_set if canonicalize_name(r.name) == name]
    if not matching:
        return False
    env = _marker_env(platform, python_version)
    for after_req in matching:
        if after_req.marker is not None and not after_req.marker.evaluate(env):
            continue
        if _version_buildable(assembled_req, after_req):
            return True
    return False


def compute_sdist_requirements(assembled: Iterable[Requirement]) -> set[Requirement]:
    """Return pinned requirements that need an sdist on the index.

    A package is included if, for **any** evaluated platform and supported Python version,
    ``exclude_from_requirements`` leaves no buildable wheel path for the assembled pin.
    """
    from build_wheels import exclude_from_requirements

    assembled_set = set(assembled)
    by_name: dict[str, list[Requirement]] = {}
    for req in assembled_set:
        by_name.setdefault(canonicalize_name(req.name), []).append(req)
    needing_names: set[str] = set()
    python_versions = _load_supported_python_versions()

    for platform in SDIST_EVAL_PLATFORMS:
        exclude_reqs = YAMLListAdapter(EXCLUDE_LIST_PATH, exclude=True, current_platform=platform).requirements
        after = exclude_from_requirements(assembled_set, exclude_reqs, print_requirements=False)
        for name, assembled_reqs in by_name.items():
            if name in needing_names:
                continue
            for py_ver in python_versions:
                applicable = [ar for ar in assembled_reqs if _requirement_applies_to_env(ar, platform, py_ver)]
                if not applicable:
                    continue
                if not any(_can_build_wheel_on_platform(ar, after, platform, py_ver) for ar in applicable):
                    needing_names.add(name)
                    break

    return {req for name in needing_names for req in by_name[name]}


def write_sdist_requirements_file(
    path: Path | str,
    requirements: Iterable[Requirement],
) -> int:
    """Write one PEP 508 requirement per line; return count written."""
    lines = sorted((str(r) for r in requirements), key=str.lower)
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def emit_sdist_requirements_to_stdout(assembled: Iterable[Requirement]) -> int:
    reqs = compute_sdist_requirements(assembled)
    for line in sorted((str(r) for r in reqs), key=str.lower):
        print(line)
    return 0


def main() -> int:
    """CLI: read assembled requirements from a file or run full IDF assembly."""
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(
            "Usage: emit_sdist_requirements.py [assembled_requirements.txt]\n"
            "  With no args, runs assemble_requirements() like build_wheels.py (needs network).\n"
            "  Writes sdist_requirements.txt in the current directory."
        )
        return 0

    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        assembled: set[Requirement] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                assembled.add(Requirement(line))
            except Exception:
                continue
    else:
        from build_wheels import assemble_requirements
        from build_wheels import fetch_idf_branches
        from build_wheels import get_constraints_versions
        from build_wheels import get_used_idf_branches

        branches = get_used_idf_branches(fetch_idf_branches())
        constraints = get_constraints_versions(branches)
        assembled = assemble_requirements(branches, constraints, make_txt_file=False)

    reqs = compute_sdist_requirements(assembled)
    count = write_sdist_requirements_file(SDIST_REQUIREMENTS_FILE, reqs)
    print_color(f"Wrote {count} sdist requirement(s) to {SDIST_REQUIREMENTS_FILE}", Fore.GREEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
