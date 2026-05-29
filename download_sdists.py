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

from pathlib import Path

from colorama import Fore

from _helper_functions import print_color

DEFAULT_OUT = Path("downloaded_wheels")
DEFAULT_INDEX = "https://pypi.org/simple"


def download_sdists(
    requirements_file: Path,
    dest_dir: Path = DEFAULT_OUT,
    index_url: str = DEFAULT_INDEX,
) -> int:
    if not requirements_file.is_file():
        raise SystemExit(f"requirements file not found: {requirements_file}")
    lines = [
        ln.strip()
        for ln in requirements_file.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not lines:
        print_color("No sdist requirements to download.", Fore.YELLOW)
        return 0

    dest_dir.mkdir(parents=True, exist_ok=True)
    print_color(f"---------- DOWNLOAD SDISTS ({len(lines)} requirements) ----------")
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "-r",
        str(requirements_file),
        "-d",
        str(dest_dir),
        "--no-deps",
        "--no-binary",
        ":all:",
        "-i",
        index_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        if result.stderr:
            print_color(result.stderr, Fore.RED)
        return result.returncode
    print_color("---------- END DOWNLOAD SDISTS ----------", Fore.GREEN)
    return 0


def main() -> int:
    req_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("sdist_requirements.txt")
    return download_sdists(req_file)


if __name__ == "__main__":
    sys.exit(main())
