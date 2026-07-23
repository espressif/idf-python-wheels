#!/usr/bin/env python3
#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Download source distributions listed in sdist_requirements.txt into downloaded_wheels/."""

from __future__ import annotations

import subprocess
import sys
import tempfile

from pathlib import Path

from colorama import Fore
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement

from _helper_functions import _requirement_for_pip_download
from _helper_functions import print_color

DEFAULT_OUT = Path("downloaded_wheels")
DEFAULT_INDEX = "https://pypi.org/simple"


def _read_requirement_lines(requirements_file: Path) -> list[str]:
    return [
        ln.strip()
        for ln in requirements_file.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _sdist_download_line(requirement_line: str) -> str:
    """PEP 508 name+specifier for ``pip download``; sdists are platform-independent."""
    line = requirement_line.strip()
    if not line:
        return line
    try:
        return _requirement_for_pip_download(Requirement(line))
    except InvalidRequirement:
        return line


def download_sdists(
    requirements_file: Path,
    dest_dir: Path = DEFAULT_OUT,
    index_url: str = DEFAULT_INDEX,
) -> int:
    if not requirements_file.is_file():
        raise SystemExit(f"requirements file not found: {requirements_file}")
    lines = _read_requirement_lines(requirements_file)
    if not lines:
        print_color("No sdist requirements to download.", Fore.YELLOW)
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    print_color(f"---------- DOWNLOAD SDISTS ({len(lines)} requirements) ----------")

    # Each line is an assembled IDF pin; merged branches can list conflicting pins for
    # the same project (e.g. esptool~=4.12.dev2 vs esptool>=5.3.0.dev0). Resolve and
    # download them one at a time so pip does not try to satisfy all pins together.
    failures: list[str] = []
    for index, line in enumerate(lines, start=1):
        download_line = _sdist_download_line(line)
        print_color(f"[{index}/{len(lines)}] {line}", Fore.CYAN)
        if download_line != line.strip():
            print_color(f"  -> pip download as {download_line} (markers ignored for sdist)", Fore.YELLOW)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            encoding="utf-8",
            delete=False,
        ) as req_file:
            req_file.write(download_line + "\n")
            one_line_req = Path(req_file.name)
        try:
            cmd = [
                sys.executable,
                "-m",
                "pip",
                "download",
                "-r",
                str(one_line_req),
                "-d",
                str(dest_dir),
                "--no-deps",
                "--no-binary",
                ":all:",
                "-i",
                index_url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
        finally:
            one_line_req.unlink(missing_ok=True)
        if result.stdout:
            print(result.stdout)
        if result.returncode != 0:
            if result.stderr:
                print_color(result.stderr, Fore.RED)
            failures.append(line)

    if failures:
        print_color(
            f"Failed to download {len(failures)} sdist requirement(s):",
            Fore.RED,
        )
        for line in failures:
            print_color(f"  - {line}", Fore.RED)
        return 1

    print_color("---------- END DOWNLOAD SDISTS ----------", Fore.GREEN)
    return 0


def main() -> int:
    req_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sdist_requirements.txt")
    return download_sdists(req_file)


if __name__ == "__main__":
    sys.exit(main())
