#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""Print S3 keys to delete from the Espressif PyPI index.

Usage:
    python delete_index_artifact.py --expected-count N --manifest PATH

Each record is ``package_name, artifact_filename`` (comma or tab separated).
Records are separated by newlines or ``;``. Records starting with ``#`` are ignored.

Prints ``pypi/<package>/<filename>`` one per line. The package directory is lowercased
and otherwise left as written. This script does not call AWS.
"""

from __future__ import annotations

import argparse
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

INDEX_PREFIX = "pypi"


def _fail(message: str) -> NoReturn:
    raise SystemExit(f"Error: {message}")


def _reject_unsafe_segment(value: str, label: str) -> None:
    """Reject a path segment before any basename stripping.

    ``Path(value).name`` drops directory components, so separators and ``..``
    have to be rejected on the raw string. That check is the same on Linux and Windows.
    """
    if not value or "\x00" in value:
        _fail(f"empty or invalid {label}.")
    if "/" in value or "\\" in value or ".." in value:
        _fail(f"{label} must not contain path separators or '..': {value!r}.")
    if value in (".", ".."):
        _fail(f"{label} must be a single path segment (basename), got {value!r}.")


def build_s3_key(package: str, filename: str) -> str:
    _reject_unsafe_segment(package, "package name")
    _reject_unsafe_segment(filename, "artifact filename")
    if filename.lower() == "index.html":
        _fail("deleting index.html artifacts is not allowed.")
    return f"{INDEX_PREFIX}/{package.lower()}/{filename}"


@dataclass(frozen=True)
class ManifestEntry:
    package: str
    filename: str


def parse_manifest_line(line: str, line_no: int) -> ManifestEntry | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "\t" in stripped and "," not in stripped:
        parts = stripped.split("\t", 1)
    else:
        parts = stripped.split(",", 1)
    if len(parts) != 2:
        _fail(f"manifest line {line_no}: expected 'package, filename', got {line!r}.")
    package, filename = parts[0].strip(), parts[1].strip()
    if not package or not filename:
        _fail(f"manifest line {line_no}: empty package or filename.")
    return ManifestEntry(package=package, filename=filename)


def parse_manifest(text: str) -> list[ManifestEntry]:
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for line_no, line in enumerate(text.splitlines() or [""], start=1):
        # ``;`` separates records on one line. The workflow_dispatch string input
        # is a single-line HTML field, so a pasted newline does not survive.
        for record in line.split(";"):
            entry = parse_manifest_line(record, line_no)
            if entry is None:
                continue
            key = build_s3_key(entry.package, entry.filename)
            if key in seen:
                _fail(f"duplicate manifest entry for {entry.package!r}, {entry.filename!r} ({key}).")
            seen.add(key)
            entries.append(entry)
    return entries


def load_manifest(path: str) -> list[ManifestEntry]:
    if path == "-":
        return parse_manifest(sys.stdin.read())
    manifest_path = Path(path)
    if not manifest_path.is_file():
        _fail(f"manifest file not found: {path}")
    return parse_manifest(manifest_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-count",
        type=int,
        required=True,
        metavar="N",
        help="Must equal the number of manifest records. No keys are printed when it does not.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        metavar="PATH",
        help="Manifest file path, or '-' for stdin",
    )
    args = parser.parse_args(argv)
    if args.expected_count < 1:
        _fail("--expected-count must be a positive integer.")
    entries = load_manifest(args.manifest)
    if len(entries) != args.expected_count:
        _fail(f"manifest has {len(entries)} artifact(s) but --expected-count is {args.expected_count}.")
    for entry in entries:
        print(build_s3_key(entry.package, entry.filename))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
