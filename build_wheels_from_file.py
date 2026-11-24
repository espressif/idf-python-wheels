#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
from __future__ import annotations

import argparse
import json
import os
import platform
import sys

from pathlib import Path

from colorama import Fore
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement

from _helper_functions import filter_requirements_by_pypi_requires_python
from _helper_functions import get_current_platform
from _helper_functions import print_color
from build_wheels import build_one
from idf_branch_resolve import resolve_branch_subset
from yaml_list_adapter import is_excluded
from yaml_list_adapter import load_exclude_rules


def _apply_force_interpreter_binary(cli_flag: bool) -> bool:
    """Linux only. Windows source builds are unreliable; macOS prefers PyPI ``--only-binary``."""
    return bool(cli_flag) and platform.system() == "Linux"


def _resolve_dependent_lines(lines: list[str], requirements_dir: str) -> tuple[list[str], bool]:
    """Resolve dependent package names against the branch constraints if available."""
    input_path = Path(requirements_dir) / "idf_inputs.json"
    if not input_path.is_file():
        return lines, False
    data = json.loads(input_path.read_text(encoding="utf-8"))
    branches = data.get("branches") or []
    exact_pins = set()
    resolve_lines = []
    for line in lines:
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        specifiers = list(requirement.specifier)
        if len(specifiers) == 1 and specifiers[0].operator == "==":
            exact_pins.add(requirement)
        else:
            resolve_lines.append(str(requirement))
    if not exact_pins and not resolve_lines:
        return [], True
    pins = set(exact_pins)
    if resolve_lines:
        pins.update(resolve_branch_subset(branches, resolve_lines))
    rules = load_exclude_rules(
        Path(__file__).with_name("exclude_list.yaml"),
        current_platform=get_current_platform(),
    )
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    kept = {
        pin
        for pin in pins
        if not is_excluded(pin.name, next(iter(pin.specifier)).version, get_current_platform(), python_version, rules)
    }
    kept = filter_requirements_by_pypi_requires_python(kept)
    Path("resolved_pins.txt").write_text(
        "\n".join(sorted(str(pin) for pin in kept)) + "\n",
        encoding="utf-8",
    )
    return [str(pin) for pin in sorted(kept, key=str.lower)], True


def _build_requirement(
    requirement: str,
    *,
    force_interpreter: bool,
    resolved_exact_pin: bool,
) -> str:
    return build_one(
        requirement,
        force_interpreter=force_interpreter,
        no_deps=resolved_exact_pin,
    )


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
        "Ignored on Windows (source builds for e.g. cryptography are not used in CI there) "
        "and on macOS (``--only-binary :all:`` keeps official macosx tags). "
        "Some packages are always skipped (e.g. cryptography, pydantic-core, argon2-cffi-bindings, PyNaCl, PyObjC)."
    ),
)

args = parser.parse_args()

Path("downloaded_wheels").mkdir(exist_ok=True)

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

    requirements, resolved_exact_pins = _resolve_dependent_lines(requirements, requirements_dir)
    for requirement in requirements:
        requirement = requirement.strip()
        if not requirement or requirement.startswith("#"):
            continue
        force_interpreter = _apply_force_interpreter_binary(args.force_interpreter_binary)
        result = _build_requirement(
            requirement,
            force_interpreter=force_interpreter,
            resolved_exact_pin=resolved_exact_pins,
        )
        if result == "skipped":
            skipped_wheels += 1
        elif result == "succeeded":
            succeeded_wheels += 1
        else:
            failed_wheels += 1

    print_color("---------- STATISTICS ----------")
    print_color(f"Succeeded {succeeded_wheels} wheels", Fore.GREEN)
    print_color(f"Failed {failed_wheels} wheels", Fore.RED)
    if skipped_wheels:
        print_color(f"Skipped {skipped_wheels} wheels", Fore.YELLOW)

    if args.ci_tests:
        if succeeded_wheels > 0 and failed_wheels == 0:
            raise SystemExit("CI: expected some builds to fail (excluded packages)")
    elif failed_wheels != 0:
        raise SystemExit("One or more wheels failed to build")

# Build wheels from passed requirements
else:
    force_interpreter = _apply_force_interpreter_binary(args.force_interpreter_binary)
    for requirement in in_requirements:
        result = _build_requirement(
            requirement,
            force_interpreter=force_interpreter,
            resolved_exact_pin=False,
        )
        if result == "skipped":
            skipped_wheels += 1
        elif result == "succeeded":
            succeeded_wheels += 1
        else:
            failed_wheels += 1

    print_color("---------- STATISTICS ----------")
    print_color(f"Succeeded {succeeded_wheels} wheels", Fore.GREEN)
    print_color(f"Failed {failed_wheels} wheels", Fore.RED)
    if skipped_wheels:
        print_color(f"Skipped {skipped_wheels} wheels", Fore.YELLOW)

    if args.ci_tests:
        if succeeded_wheels > 0 and failed_wheels == 0:
            raise SystemExit("CI: expected some builds to fail (excluded packages)")
    elif failed_wheels != 0:
        raise SystemExit("One or more wheels failed to build")
