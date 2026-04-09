#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys

from colorama import Fore
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from _helper_functions import get_no_binary_args
from _helper_functions import print_color
from _helper_functions import pypi_requires_python_preflight_skip

# Do not pass --no-binary for these in --force-interpreter-binary mode:
# - sdists whose legacy setup breaks under PEP 517 isolation (pkg_resources in isolated env).
# - sdists that fail to compile on CI when a usable wheel exists (e.g. ruamel.yaml.clib + clang).
# - PyObjC: all pyobjc / pyobjc-framework-* use pyobjc_setup.py + pkg_resources (macOS).
# - cryptography: abi3 wheels; avoid PyO3 max-Python / heavy Rust rebuilds in dependent jobs.
# - pydantic-core: maturin + jiter + PyO3 can fail from sdist on some CI combos (e.g. ARM64 3.9:
#   jiter vs pyo3-ffi PyUnicode_* / extract API). Prefer compatible wheels from find-links or PyPI.
_FORCE_INTERPRETER_BINARY_SKIP_EXACT = frozenset(
    {
        canonicalize_name("cryptography"),
        canonicalize_name("pydantic-core"),
        canonicalize_name("protobuf"),
        canonicalize_name("ruamel.yaml.clib"),
    }
)


def _force_interpreter_skip_package(canonical_dist_name: str) -> bool:
    if canonical_dist_name in _FORCE_INTERPRETER_BINARY_SKIP_EXACT:
        return True
    # PyObjC meta and framework bindings (pyobjc-framework-corebluetooth, etc.)
    return canonical_dist_name == "pyobjc" or canonical_dist_name.startswith("pyobjc-")


def _force_interpreter_no_binary_args(requirement_line: str) -> list[str]:
    """Return pip --no-binary for this package so pip cannot reuse e.g. cp311-abi3 wheels on 3.13."""
    line = requirement_line.strip()
    if not line:
        return []
    try:
        req = Requirement(line)
    except InvalidRequirement:
        return []
    if _force_interpreter_skip_package(canonicalize_name(req.name)):
        return []
    return ["--no-binary", req.name]


def _apply_force_interpreter_binary(cli_flag: bool) -> bool:
    """Linux/macOS only: forcing sdist builds for cryptography etc. is unreliable on Windows CI."""
    return cli_flag and platform.system() != "Windows"


def _pypi_preflight_skip_line(requirement_line: str) -> bool:
    """Print and return True if this line should be skipped (PyPI Requires-Python)."""
    try:
        req = Requirement(requirement_line)
    except InvalidRequirement:
        return False
    skip, reason = pypi_requires_python_preflight_skip(req)
    if skip:
        print_color(f"-- skip {requirement_line} ({reason})", Fore.YELLOW)
    return skip


parser = argparse.ArgumentParser(description="Process build arguments.")
parser.add_argument(
    "requirements_path",
    metavar="Path",
    type=str,
    nargs="?",
    default="",
    help="path to Python version dependent requirements txt",
)

parser.add_argument(
    "-r",
    "--requirements",
    metavar="Requirement(s)",
    type=str,
    nargs="*",
    help="requirement(s) to be build wheel(s) for",
)
parser.add_argument(
    "--ci-tests",
    action="store_true",
    help="CI exclude-tests mode: fail if all wheels succeed (expect some to fail, e.g. excluded packages)",
)
parser.add_argument(
    "--force-interpreter-binary",
    action="store_true",
    help=(
        "For each requirement, pass --no-binary <pkg> so pip builds a wheel for the current "
        "interpreter instead of reusing a compatible abi3 / older cpXY wheel from --find-links. "
        "Ignored on Windows (source builds for e.g. cryptography are not used in CI there). "
        "Some packages are always skipped (e.g. cryptography, pydantic-core, protobuf, PyObjC, ruamel.yaml.clib)."
    ),
)

args = parser.parse_args()


requirements_dir = args.requirements_path
in_requirements = args.requirements

failed_wheels = 0
succeeded_wheels = 0
skipped_wheels = 0

# Build wheels for requirements in file
if requirements_dir:
    try:
        with open(f"{requirements_dir}{os.sep}dependent_requirements.txt", "r") as f:
            requirements = f.readlines()
    except FileNotFoundError as e:
        raise SystemExit(f"Python version dependent requirements directory or file not found ({e})")

    for requirement in requirements:
        requirement = requirement.strip()
        if not requirement or requirement.startswith("#"):
            continue
        if _pypi_preflight_skip_line(requirement):
            skipped_wheels += 1
            continue
        # Get no-binary args for packages that should be built from source
        no_binary_args = get_no_binary_args(requirement)
        force_interpreter_args = (
            _force_interpreter_no_binary_args(requirement)
            if _apply_force_interpreter_binary(args.force_interpreter_binary)
            else []
        )

        out = subprocess.run(
            [
                f"{sys.executable}",
                "-m",
                "pip",
                "wheel",
                requirement,
                "--find-links",
                "downloaded_wheels",
                "--wheel-dir",
                "downloaded_wheels",
            ]
            + no_binary_args
            + force_interpreter_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        print(out.stdout.decode("utf-8", errors="replace"))
        if out.stderr:
            print_color(out.stderr.decode("utf-8", errors="replace"), Fore.RED)

        if out.returncode != 0:
            failed_wheels += 1
        else:
            succeeded_wheels += 1

    print_color("---------- STATISTICS ----------")
    print_color(f"Succeeded {succeeded_wheels} wheels", Fore.GREEN)
    print_color(f"Failed {failed_wheels} wheels", Fore.RED)
    if skipped_wheels:
        print_color(f"Skipped {skipped_wheels} wheels (PyPI Requires-Python)", Fore.YELLOW)

    if args.ci_tests:
        if succeeded_wheels > 0 and failed_wheels == 0:
            raise SystemExit("CI: expected some builds to fail (excluded packages)")
    elif failed_wheels != 0:
        raise SystemExit("One or more wheels failed to build")

# Build wheels from passed requirements
else:
    for requirement in in_requirements:
        if _pypi_preflight_skip_line(requirement):
            skipped_wheels += 1
            continue
        # Get no-binary args for packages that should be built from source
        no_binary_args = get_no_binary_args(requirement)
        force_interpreter_args = (
            _force_interpreter_no_binary_args(requirement)
            if _apply_force_interpreter_binary(args.force_interpreter_binary)
            else []
        )

        out = subprocess.run(
            [
                f"{sys.executable}",
                "-m",
                "pip",
                "wheel",
                f"{requirement}",
                "--find-links",
                "downloaded_wheels",
                "--wheel-dir",
                "downloaded_wheels",
            ]
            + no_binary_args
            + force_interpreter_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        print(out.stdout.decode("utf-8", errors="replace"))
        if out.stderr:
            print_color(out.stderr.decode("utf-8", errors="replace"), Fore.RED)

        if out.returncode != 0:
            failed_wheels += 1
        else:
            succeeded_wheels += 1

    print_color("---------- STATISTICS ----------")
    print_color(f"Succeeded {succeeded_wheels} wheels", Fore.GREEN)
    print_color(f"Failed {failed_wheels} wheels", Fore.RED)
    if skipped_wheels:
        print_color(f"Skipped {skipped_wheels} wheels (PyPI Requires-Python)", Fore.YELLOW)

    if args.ci_tests:
        if succeeded_wheels > 0 and failed_wheels == 0:
            raise SystemExit("CI: expected some builds to fail (excluded packages)")
    elif failed_wheels != 0:
        raise SystemExit("One or more wheels failed to build")
