#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Load ``native_import_guard.yaml`` and resolve import-probe statements for a wheel."""

from __future__ import annotations

import sys
import zipfile

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from packaging.specifiers import InvalidSpecifier
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion
from packaging.version import Version

from _helper_functions import exclude_entry_applies_to_platform
from _helper_functions import get_current_platform
from _helper_functions import parse_wheel_name

_REPO_ROOT = Path(__file__).resolve().parent
NATIVE_IMPORT_GUARD_PATH = "native_import_guard.yaml"

DEFAULT_SKIP_TOP_LEVEL_IMPORTS = frozenset({"test", "tests", "testing"})


def _yaml_scalar_or_list(raw: Any) -> list[str]:
    if raw is None or raw is False or raw == "":
        return []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [str(raw).strip()] if str(raw).strip() else []


def _version_matches_yaml_spec(raw: Any, version: Version) -> bool:
    """Match ``version`` / ``python`` YAML filters (string or list of PEP 440 specifiers).

    A list of only ``==`` / ``===`` specs is OR (e.g. ``['==3.8', '==3.9']``).
    Otherwise specs are AND (e.g. ``['>=1.0', '<2']``).
    """
    items = _yaml_scalar_or_list(raw)
    if not items:
        return True
    try:
        specs = [SpecifierSet(item) for item in items]
        operators = [spec.operator for spec_set in specs for spec in spec_set]
        if operators and all(op in ("==", "===") for op in operators):
            return any(version in spec_set for spec_set in specs)
        return version in SpecifierSet(",".join(items))
    except InvalidSpecifier as exc:
        raise ValueError(f"invalid native_import_guard.yaml version/python specifier {items!r}: {exc}") from exc


@dataclass(frozen=True)
class NativeImportGuardEntry:
    """One ``packages:`` row from ``native_import_guard.yaml``."""

    name: str
    imports: tuple[str, ...]
    skip: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class NativeImportGuardConfig:
    """Loaded ``native_import_guard.yaml`` (policy + per-package rows)."""

    probe_unlisted: bool
    skip_pure_any: bool
    skip_top_level: frozenset[str]
    packages: tuple[NativeImportGuardEntry, ...]


_NATIVE_IMPORT_GUARD_CACHE: dict[Path, NativeImportGuardConfig] = {}


def _native_import_entry_matches(
    entry: NativeImportGuardEntry,
    *,
    current_platform: str,
    wheel_version: str | None,
    python_version: str,
) -> bool:
    if not exclude_entry_applies_to_platform(entry.raw, current_platform):
        return False
    raw_version = entry.raw.get("version")
    if raw_version:
        if not wheel_version:
            return False
        try:
            if not _version_matches_yaml_spec(raw_version, Version(wheel_version)):
                return False
        except InvalidVersion:
            return False
    raw_python = entry.raw.get("python")
    if raw_python:
        try:
            if not _version_matches_yaml_spec(raw_python, Version(python_version)):
                return False
        except InvalidVersion:
            return False
    return True


def load_native_import_guard(repo_root: Path | None = None) -> NativeImportGuardConfig:
    """Load ``native_import_guard.yaml`` (cached per repo root)."""
    root = (repo_root if repo_root is not None else _REPO_ROOT).resolve()
    cached = _NATIVE_IMPORT_GUARD_CACHE.get(root)
    if cached is not None:
        return cached
    path = root / NATIVE_IMPORT_GUARD_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    skip_top = _yaml_scalar_or_list(data.get("skip_top_level"))
    packages: list[NativeImportGuardEntry] = []
    for entry in data.get("packages") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("package_name") or entry.get("name")
        if not name:
            continue
        raw_imports = entry.get("imports") or []
        stmts = tuple(str(s).strip() for s in raw_imports if str(s).strip())
        skip = bool(entry.get("skip"))
        if not stmts and not skip:
            continue
        packages.append(
            NativeImportGuardEntry(
                name=canonicalize_name(str(name)),
                imports=stmts,
                skip=skip,
                raw=entry,
            )
        )
    if not packages:
        raise ValueError(f"{path}: no packages with imports or skip defined")
    config = NativeImportGuardConfig(
        probe_unlisted=bool(data.get("probe_unlisted", True)),
        skip_pure_any=bool(data.get("skip_pure_any", True)),
        skip_top_level=frozenset(skip_top) if skip_top else DEFAULT_SKIP_TOP_LEVEL_IMPORTS,
        packages=tuple(packages),
    )
    _NATIVE_IMPORT_GUARD_CACHE[root] = config
    return config


def native_import_guard_by_name(
    repo_root: Path | None = None,
) -> dict[str, NativeImportGuardEntry]:
    """Map canonical name → first YAML row that defines ``imports`` (custom probes)."""
    by_name: dict[str, NativeImportGuardEntry] = {}
    for entry in load_native_import_guard(repo_root).packages:
        if entry.imports and entry.name not in by_name:
            by_name[entry.name] = entry
    return by_name


def is_pure_any_wheel_name(wheel_name: str) -> bool:
    """True for ``*-none-any.whl`` (no native extension to probe)."""
    return "-none-any.whl" in wheel_name.lower()


def wheel_top_level_modules(
    wheel_path: Path | str,
    skip_top_level: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Read ``*.dist-info/top_level.txt`` from a wheel (import names)."""
    skip = skip_top_level if skip_top_level is not None else DEFAULT_SKIP_TOP_LEVEL_IMPORTS
    path = Path(wheel_path)
    try:
        with zipfile.ZipFile(path) as zf:
            matches = [n for n in zf.namelist() if n.endswith(".dist-info/top_level.txt")]
            if not matches:
                return ()
            text = zf.read(matches[0]).decode("utf-8", errors="replace")
    except (OSError, zipfile.BadZipFile):
        return ()
    names: list[str] = []
    for line in text.splitlines():
        name = line.strip()
        if name and name not in skip:
            names.append(name)
    return tuple(names)


def _is_importable_module_name(name: str) -> bool:
    """True when ``name`` is safe to pass to ``import`` (identifier or dotted identifiers)."""
    return bool(name) and all(part.isidentifier() for part in name.split("."))


def default_native_import_statements(
    wheel_path: Path | str,
    skip_top_level: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """One ``import <top_level>`` for a native wheel without custom YAML imports.

    Installs in ``test_wheels_install.py`` use ``--no-deps``. Guessing the import
    from the distribution name (when ``top_level.txt`` is missing) or importing
    every top-level name often fails. Prefer the public name that matches the
    distribution; otherwise the first public identifier. Skip the probe if none.
    """
    tops = [name for name in wheel_top_level_modules(wheel_path, skip_top_level) if _is_importable_module_name(name)]
    if not tops:
        return ()
    parsed = parse_wheel_name(Path(wheel_path).name)
    dist_mod = parsed[0].replace("-", "_") if parsed else ""
    public = [name for name in tops if not name.startswith("_")]
    if dist_mod in tops:
        chosen = dist_mod
    elif public:
        chosen = public[0]
    else:
        chosen = tops[0]
    return (f"import {chosen}",)


def resolve_native_import_statements(
    wheel_path: Path | str,
    *,
    config: NativeImportGuardConfig | None = None,
    current_platform: str | None = None,
    python_version: str | None = None,
) -> tuple[str, ...] | None:
    """Import statements for a wheel, or ``None`` to skip the probe.

    First matching YAML row (name + platform / python / version) wins: ``skip: true``
    skips; ``imports`` override the default. A row that does not match its filters
    is ignored (the package is treated as unlisted). Unlisted platform wheels use
    ``top_level.txt`` when ``probe_unlisted`` is true. Use ``skip: true`` when a
    probe should not run.
    """
    path = Path(wheel_path)
    parsed = parse_wheel_name(path.name)
    if not parsed:
        return None
    cfg = config if config is not None else load_native_import_guard()
    if cfg.skip_pure_any and is_pure_any_wheel_name(path.name):
        return None
    plat = current_platform if current_platform is not None else get_current_platform()
    py_ver = python_version if python_version is not None else f"{sys.version_info.major}.{sys.version_info.minor}"
    for entry in cfg.packages:
        if entry.name != parsed[0]:
            continue
        if not _native_import_entry_matches(
            entry,
            current_platform=plat,
            wheel_version=parsed[1],
            python_version=py_ver,
        ):
            continue
        if entry.skip:
            return None
        if entry.imports:
            return entry.imports
        break
    if not cfg.probe_unlisted:
        return None
    stmts = default_native_import_statements(path, cfg.skip_top_level)
    return stmts or None


def package_import_statements(
    package_name: str,
    *,
    config: NativeImportGuardConfig | None = None,
    repo_root: Path | None = None,
    current_platform: str | None = None,
    python_version: str | None = None,
    wheel_version: str | None = None,
) -> tuple[str, ...] | None:
    """YAML ``imports`` for a distribution name after platform / python / version filters.

    Used by ``scripts/run_native_import_probe.py`` (no wheel path). ``skip`` and
    unmatched filters return ``None``; unlisted names are not default-probed.
    """
    cfg = config if config is not None else load_native_import_guard(repo_root)
    plat = current_platform if current_platform is not None else get_current_platform()
    py_ver = python_version if python_version is not None else f"{sys.version_info.major}.{sys.version_info.minor}"
    canonical = canonicalize_name(package_name)
    for entry in cfg.packages:
        if entry.name != canonical:
            continue
        if not _native_import_entry_matches(
            entry,
            current_platform=plat,
            wheel_version=wheel_version,
            python_version=py_ver,
        ):
            continue
        if entry.skip:
            return None
        if entry.imports:
            return entry.imports
        break
    return None
