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
from typing import List
from typing import Optional
from typing import Set

from colorama import Fore
from packaging.requirements import Requirement

from _helper_functions import print_color

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


def _idf_release_major(branch: str) -> Optional[int]:
    match = re.match(r"release/v(\d+)\.\d+", branch)
    if match:
        return int(match.group(1))
    if branch == "master":
        return int(os.environ.get("IDF_MASTER_MAJOR", "6"))
    return None


def branch_targets_current_interpreter(branch: str) -> bool:
    """True when this ESP-IDF branch is meant to be solved on the running Python."""
    if branch == "esptool":
        return True
    major = _idf_release_major(branch)
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

    taken: List[str] = []
    seen_resolution = False
    first_error: Optional[int] = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        lower = stripped.lower()
        if first_error is None and stripped.startswith("ERROR:"):
            first_error = index
        if stripped.startswith("ERROR:") and (
            "cannot install" in lower or "resolutionimpossible" in lower or "conflicting" in lower
        ):
            seen_resolution = True
        if seen_resolution:
            taken.append(line)
            if stripped.startswith("To fix this you could try to:"):
                for rest in lines[index + 1 : index + 6]:
                    if rest.strip():
                        taken.append(rest)
                break
    if not taken and first_error is not None:
        taken = lines[first_error:]
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
    with tempfile.TemporaryDirectory() as tmp:
        req_path = os.path.join(tmp, "requirements.txt")
        cons_path = os.path.join(tmp, "constraints.txt")
        report_path = os.path.join(tmp, "report.json")
        with open(req_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(requirement_lines) + "\n")
        with open(cons_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(constraint_lines) + "\n")
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
            out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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


def resolve_branches(branches: List[dict]) -> set:
    if not branches:
        raise SystemExit("No ESP-IDF branches were downloaded to resolve")
    groups = []
    skipped: List[str] = []
    py_mm = f"{sys.version_info.major}.{sys.version_info.minor}"
    for item in branches:
        branch = item["branch"]
        if not branch_targets_current_interpreter(branch):
            skipped.append(branch)
            major = _idf_release_major(branch)
            print_color(
                f"Skipping pin resolve for {branch} on Python {py_mm} "
                f"(ESP-IDF v{major} wheels target Python {IDF6_MIN_PYTHON[0]}.{IDF6_MIN_PYTHON[1]}+)",
                Fore.YELLOW,
            )
            continue
        try:
            groups.append(_resolve_lines(item["requirements"], item["constraints"], branch))
        except BranchResolveError as exc:
            raise SystemExit(str(exc)) from exc
    if skipped:
        print_color(f"Branches not resolved on Python {py_mm}: {', '.join(skipped)}", Fore.YELLOW)
    if not groups:
        raise SystemExit(f"No ESP-IDF branch could be resolved on Python {py_mm}")
    return union_pin_sets(groups)
