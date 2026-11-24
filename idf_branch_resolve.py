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
from packaging.requirements import Requirement

from _helper_functions import canonical_requirement_lines
from _helper_functions import get_current_platform
from _helper_functions import print_color
from yaml_list_adapter import ExcludeRequirements
from yaml_list_adapter import applicable_exclusion_rules
from yaml_list_adapter import exclusion_constraints

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
    taken = lines[conflict_index if conflict_index is not None else (error_indexes[0] if error_indexes else 0) :]
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


def _requirements_for_resolution(
    lines: List[str],
    branch: str,
    exclude_rules: ExcludeRequirements | None,
) -> List[str]:
    if exclude_rules is None:
        return lines
    platform = get_current_platform()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    filtered: List[str] = []
    for requirement in canonical_requirement_lines(lines, source=f"{branch} requirements"):
        parsed = Requirement(requirement)
        applicable = applicable_exclusion_rules(parsed.name, platform, python_version, exclude_rules)
        if any(not rule.specifier for rule in applicable):
            print_color(f"-- skip excluded resolver input {parsed}", Fore.YELLOW)
            continue
        specs = list(parsed.specifier)
        if (
            len(specs) == 1
            and specs[0].operator == "=="
            and any(rule.specifier.contains(specs[0].version, prereleases=True) for rule in applicable)
        ):
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
        try:
            groups.append(
                _resolve_lines(
                    _requirements_for_resolution(
                        _group_value(item, "requirements"),
                        branch,
                        exclude_rules,
                    ),
                    _group_value(item, "constraints")
                    + (
                        exclusion_constraints(
                            exclude_rules,
                            get_current_platform(),
                            py_mm,
                        )
                        if exclude_rules is not None
                        else []
                    ),
                    branch,
                )
            )
        except BranchResolveError as exc:
            raise SystemExit(str(exc)) from exc
    if skipped:
        print_color(f"Branches not resolved on Python {py_mm}: {', '.join(skipped)}", Fore.YELLOW)
    if not groups:
        raise SystemExit(f"No ESP-IDF branch could be resolved on Python {py_mm}")
    return union_pin_sets(groups)


def resolve_branch_subset(branches: List[dict], requirement_lines: List[str]) -> set:
    """Resolve dependent requirements with each branch's own constraints.

    Stage 1 already solved the complete branch closures. Later matrix jobs use
    this smaller solve to reevaluate markers and ``Requires-Python``. Exact
    Stage-1 pins are passed through by the caller; only marker/specifier-bearing
    lines reach this resolver.
    """
    real_branches = [item for item in branches if _group_value(item, "branch") != "esptool"]
    if not real_branches:
        raise SystemExit("No ESP-IDF branches were downloaded to resolve")
    requirements = sorted(set(requirement_lines))
    groups = []
    for item in branches:
        branch = _group_value(item, "branch")
        if not branch_targets_current_interpreter(branch, _group_value(item, "idf_major")):
            continue
        try:
            groups.append(_resolve_lines(requirements, _group_value(item, "constraints"), branch))
        except BranchResolveError as exc:
            raise SystemExit(str(exc)) from exc
    if not groups:
        raise SystemExit(
            f"No ESP-IDF branch could be resolved on Python {sys.version_info.major}.{sys.version_info.minor}"
        )
    return union_pin_sets(groups)
