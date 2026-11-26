#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Per-ESP-IDF-branch pip dry-run solves (exact pins for wheel builds)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time

from dataclasses import dataclass
from typing import Any
from typing import List
from typing import Optional
from typing import Set

from colorama import Fore
from packaging.markers import UndefinedComparison
from packaging.markers import UndefinedEnvironmentName
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from _helper_functions import canonical_requirement_lines
from _helper_functions import get_current_platform
from _helper_functions import print_color
from yaml_list_adapter import ExcludeRequirements
from yaml_list_adapter import applicable_exclusion_rules
from yaml_list_adapter import exclusion_constraint_alternatives
from yaml_list_adapter import marker_environment

_DEFAULT_RETRIES = int(os.environ.get("REQUEST_RETRIES", "3"))
PIP_RESOLVE_RETRIES = int(os.environ.get("PIP_RESOLVE_RETRIES", str(_DEFAULT_RETRIES)))
PIP_RESOLVE_RETRY_SLEEP = float(os.environ.get("PIP_RESOLVE_RETRY_SLEEP", "5"))
IDF6_MIN_PYTHON = (3, 10)

_RESOLVE_TRANSIENT_MARKERS = (
    "connection reset",
    "remote end closed",
    "connection aborted",
    "read timed out",
    "temporary failure in name resolution",
    "connection broken by",
)


@dataclass
class BranchResolveError(Exception):
    branch: str
    detail: str

    def __str__(self) -> str:
        py_mm = f"{sys.version_info.major}.{sys.version_info.minor}"
        return f"Failed to resolve requirements for {self.branch} on Python {py_mm}.\n{self.detail}"


@dataclass(frozen=True)
class ResolveGroup:
    """Structured input for one branch solve."""

    branch: str
    requirements: List[str]
    constraints: List[str]
    idf_major: Optional[int] = None


def _group_value(group: Any, key: str) -> Any:
    return getattr(group, key) if isinstance(group, ResolveGroup) else group.get(key)


def _idf_release_major(branch: str) -> Optional[int]:
    match = re.match(r"release/v(\d+)\.\d+", branch)
    if match:
        return int(match.group(1))
    return None


def branch_targets_current_interpreter(branch: str, idf_major: Optional[int] = None) -> bool:
    """True when this ESP-IDF branch is meant to be solved on the running Python."""
    if branch == "esptool":
        return True
    major = idf_major if idf_major is not None else _idf_release_major(branch)
    if branch == "master" and major is None:
        major = 6
    if major is None or major < 6:
        return True
    current = (sys.version_info.major, sys.version_info.minor)
    return current >= IDF6_MIN_PYTHON


def _resolve_transient_failure(detail: str) -> bool:
    lower = detail.lower()
    if "resolutionimpossible" in lower or "conflicting dependencies" in lower:
        return False
    return any(marker in lower for marker in _RESOLVE_TRANSIENT_MARKERS)


def _pip_resolve_failure_detail(stdout: bytes, stderr: bytes) -> str:
    text = (stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")).strip()
    if not text:
        return "(pip produced no output)"
    lines = text.splitlines()
    lower_full = text.lower()
    if "resolutionimpossible" not in lower_full and "conflicting dependencies" not in lower_full:
        return text[-6000:] if len(text) > 6000 else text

    error_indexes = [i for i, line in enumerate(lines) if line.strip().startswith("ERROR:")]
    conflict_index = next(
        (
            i
            for i in error_indexes
            if any(marker in lines[i].lower() for marker in ("cannot install", "resolutionimpossible", "conflicting"))
        ),
        None,
    )
    error_at = conflict_index if conflict_index is not None else (error_indexes[0] if error_indexes else 0)
    conflict_at = next((i for i, line in enumerate(lines) if "the conflict is caused by" in line.lower()), None)
    # Pip may write the explanation to stdout and the ERROR line to stderr.
    # Concatenating the streams then puts the explanation before the error.
    start = min(error_at, conflict_at) if conflict_at is not None else error_at
    taken = lines[start:]
    if not taken:
        return text[-6000:] if len(text) > 6000 else text
    block = "\n".join(taken).strip()
    return block[:8000] if len(block) > 8000 else block


def pins_from_pip_report(report: dict) -> set:
    """Turn a ``pip install --report`` document into exact ``name==version`` pins."""
    pins: Set[Requirement] = set()
    for item in report.get("install") or []:
        metadata = item.get("metadata") or {}
        name = metadata.get("name")
        version = metadata.get("version")
        if not name or not version:
            raise ValueError("pip report install entry has no name or version")
        pins.add(Requirement(f"{name}=={version}"))
    return pins


def pins_from_pip_report_for_branch(report: dict, branch: str) -> set:
    try:
        return pins_from_pip_report(report)
    except ValueError as exc:
        raise BranchResolveError(branch, str(exc)) from exc


def union_pin_sets(groups: List[set]) -> set:
    pins: Set[Requirement] = set()
    for group in groups:
        pins.update(group)
    return pins


def _resolve_lines(requirement_lines: List[str], constraint_lines: List[str], branch: str) -> set:
    clean_requirements = canonical_requirement_lines(requirement_lines, source=f"{branch} requirements")
    clean_constraints = canonical_requirement_lines(constraint_lines, source=f"{branch} constraints")
    with tempfile.TemporaryDirectory() as tmp:
        req_path = os.path.join(tmp, "requirements.txt")
        cons_path = os.path.join(tmp, "constraints.txt")
        report_path = os.path.join(tmp, "report.json")
        with open(req_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(clean_requirements) + "\n")
        with open(cons_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(clean_constraints) + "\n")
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--ignore-installed",
            "--report",
            report_path,
            "-r",
            req_path,
            "-c",
            cons_path,
        ]
        detail = ""
        for attempt in range(1, PIP_RESOLVE_RETRIES + 1):
            env = os.environ.copy()
            for secret in (
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
            ):
                env.pop(secret, None)
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
            if out.returncode == 0 and os.path.isfile(report_path):
                break
            detail = _pip_resolve_failure_detail(out.stdout, out.stderr)
            if not _resolve_transient_failure(detail) or attempt >= PIP_RESOLVE_RETRIES:
                raise BranchResolveError(branch, detail)
            print_color(
                f"Retry pin resolve for {branch} ({attempt}/{PIP_RESOLVE_RETRIES}) after transient pip error",
                Fore.YELLOW,
            )
            time.sleep(PIP_RESOLVE_RETRY_SLEEP)
        else:
            raise BranchResolveError(branch, detail)
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
    pins = pins_from_pip_report_for_branch(report, branch)
    print(f"Resolved {len(pins)} pins for {branch}")
    return pins


def _current_python() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _exact_version(specifier: SpecifierSet) -> Optional[str]:
    """Return the version when ``specifier`` is a single ``==`` pin."""
    specs = list(specifier)
    if len(specs) == 1 and specs[0].operator == "==":
        return str(specs[0].version)
    return None


def _version_is_excluded(version: str, applicable: List[Requirement]) -> bool:
    return any(rule.specifier.contains(version, prereleases=True) for rule in applicable if rule.specifier)


def _constraints_for_name(lines: List[str], branch: str, platform: str, python_version: str) -> dict:
    grouped: dict = {}
    for line in canonical_requirement_lines(lines, source=f"{branch} constraints"):
        parsed = Requirement(line)
        if parsed.marker is not None:
            try:
                if not parsed.marker.evaluate(marker_environment(platform, python_version)):
                    continue
            except (TypeError, UndefinedComparison, UndefinedEnvironmentName, ValueError):
                continue
        grouped.setdefault(canonicalize_name(parsed.name), []).append(parsed)
    return grouped


def _effective_specifier(requirement: Requirement, constraints: List[Requirement]) -> SpecifierSet:
    """Intersect a requirement with the branch caps for the same package."""
    effective = requirement.specifier
    for constraint in constraints:
        effective &= constraint.specifier
    return effective


def _fully_excluded(requirement: Requirement, constraints: List[Requirement], applicable: List[Requirement]) -> bool:
    """True when this environment allows no version of the requirement."""
    if any(not rule.specifier for rule in applicable):
        return True
    version = _exact_version(_effective_specifier(requirement, constraints))
    return version is not None and _version_is_excluded(version, applicable)


def _constraints_without_excluded_pins(
    lines: List[str],
    branch: str,
    exclude_rules: ExcludeRequirements | None,
) -> List[str]:
    """Drop caps that pin a version the exclude list rejects. Keep every other cap."""
    if exclude_rules is None:
        return list(lines)
    platform = get_current_platform()
    python_version = _current_python()
    kept: List[str] = []
    for line in canonical_requirement_lines(lines, source=f"{branch} constraints"):
        parsed = Requirement(line)
        if parsed.marker is not None:
            try:
                if not parsed.marker.evaluate(marker_environment(platform, python_version)):
                    kept.append(line)
                    continue
            except (TypeError, UndefinedComparison, UndefinedEnvironmentName, ValueError):
                kept.append(line)
                continue
        applicable = applicable_exclusion_rules(parsed.name, platform, python_version, exclude_rules)
        version = _exact_version(parsed.specifier)
        if version is not None and _version_is_excluded(version, applicable):
            print_color(f"-- drop excluded constraint pin {parsed}", Fore.YELLOW)
            continue
        kept.append(line)
    return kept


def _requirements_for_resolution(
    lines: List[str],
    branch: str,
    exclude_rules: ExcludeRequirements | None,
    constraint_lines: Optional[List[str]] = None,
) -> List[str]:
    if exclude_rules is None:
        return lines
    platform = get_current_platform()
    python_version = _current_python()
    by_name = _constraints_for_name(constraint_lines or [], branch, platform, python_version)
    filtered: List[str] = []
    for requirement in canonical_requirement_lines(lines, source=f"{branch} requirements"):
        parsed = Requirement(requirement)
        if parsed.marker is not None:
            try:
                if not parsed.marker.evaluate(marker_environment(platform, python_version)):
                    filtered.append(requirement)
                    continue
            except (TypeError, UndefinedComparison, UndefinedEnvironmentName, ValueError):
                filtered.append(requirement)
        applicable = applicable_exclusion_rules(parsed.name, platform, python_version, exclude_rules)
        package_constraints = by_name.get(canonicalize_name(parsed.name), [])
        if _fully_excluded(parsed, package_constraints, applicable):
            print_color(f"-- skip excluded resolver input {parsed}", Fore.YELLOW)
            continue
        filtered.append(requirement)
    return filtered


def resolve_branches(
    branches: List[dict],
    exclude_rules: ExcludeRequirements | None = None,
) -> set:
    real_branches = [item for item in branches if _group_value(item, "branch") != "esptool"]
    if not real_branches:
        raise SystemExit("No ESP-IDF branches were downloaded to resolve")
    groups = []
    skipped: List[str] = []
    py_mm = f"{sys.version_info.major}.{sys.version_info.minor}"
    for item in branches:
        branch = _group_value(item, "branch")
        if not branch_targets_current_interpreter(branch, _group_value(item, "idf_major")):
            skipped.append(branch)
            major = _group_value(item, "idf_major") or _idf_release_major(branch) or "master"
            print_color(
                f"Skipping pin resolve for {branch} on Python {py_mm} "
                f"(ESP-IDF v{major} wheels target Python {IDF6_MIN_PYTHON[0]}.{IDF6_MIN_PYTHON[1]}+)",
                Fore.YELLOW,
            )
            continue
        raw_constraints = list(_group_value(item, "constraints") or [])
        requirements = _requirements_for_resolution(
            _group_value(item, "requirements"),
            branch,
            exclude_rules,
            raw_constraints,
        )
        kept_constraints = _constraints_without_excluded_pins(raw_constraints, branch, exclude_rules)
        alternatives = (
            exclusion_constraint_alternatives(get_current_platform(), py_mm, exclude_rules)
            if exclude_rules is not None
            else [[]]
        )
        errors: List[BranchResolveError] = []
        solved = False
        for extra in alternatives:
            try:
                groups.append(_resolve_lines(requirements, kept_constraints + extra, branch))
                solved = True
            except BranchResolveError as exc:
                errors.append(exc)
        if not solved:
            raise SystemExit(str(errors[0])) from errors[0]
    if skipped:
        print_color(f"Branches not resolved on Python {py_mm}: {', '.join(skipped)}", Fore.YELLOW)
    if not groups:
        raise SystemExit(f"No ESP-IDF branch could be resolved on Python {py_mm}")
    return union_pin_sets(groups)


def resolve_branch_subset(
    branches: List[dict],
    requirement_lines: List[str],
    exclude_rules: ExcludeRequirements | None = None,
) -> set:
    """Resolve dependent requirements with each branch's own constraints.

    Stage 1 already solved the complete branch closures. Later matrix jobs use
    this smaller solve to reevaluate markers and ``Requires-Python``. Exact
    Stage-1 pins and deferred marker-bearing lines are passed through by the
    caller. The current interpreter still applies each branch's constraints
    and exclusion rules before resolving.
    """
    real_branches = [item for item in branches if _group_value(item, "branch") != "esptool"]
    if not real_branches:
        raise SystemExit("No ESP-IDF branches were downloaded to resolve")
    requirements = sorted(set(requirement_lines))
    root_names = {canonicalize_name(Requirement(requirement).name) for requirement in requirements}
    groups = []
    for item in real_branches:
        branch = _group_value(item, "branch")
        if not branch_targets_current_interpreter(branch, _group_value(item, "idf_major")):
            continue
        raw_constraints = list(_group_value(item, "constraints") or [])
        filtered_requirements = _requirements_for_resolution(
            requirements,
            branch,
            exclude_rules,
            raw_constraints,
        )
        kept_constraints = _constraints_without_excluded_pins(raw_constraints, branch, exclude_rules)
        alternatives = (
            exclusion_constraint_alternatives(get_current_platform(), _current_python(), exclude_rules)
            if exclude_rules is not None
            else [[]]
        )
        errors: List[BranchResolveError] = []
        solved = False
        for extra in alternatives:
            try:
                groups.append(_resolve_lines(filtered_requirements, kept_constraints + extra, branch))
                solved = True
            except BranchResolveError as exc:
                errors.append(exc)
        if not solved:
            raise SystemExit(str(errors[0])) from errors[0]
    if not groups:
        raise SystemExit(
            f"No ESP-IDF branch could be resolved on Python {sys.version_info.major}.{sys.version_info.minor}"
        )
    return {pin for pin in union_pin_sets(groups) if canonicalize_name(pin.name) in root_names}
