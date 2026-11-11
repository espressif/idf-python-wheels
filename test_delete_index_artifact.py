#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import tempfile
import unittest

from io import StringIO
from pathlib import Path
from unittest.mock import patch

from delete_index_artifact import build_s3_key
from delete_index_artifact import load_manifest
from delete_index_artifact import main
from delete_index_artifact import parse_manifest

_WHEEL = "psutil-7.2.2-cp36-abi3-macosx_15_0_x86_64.whl"


class TestBuildS3Key(unittest.TestCase):
    def test_lowercases_package_only(self) -> None:
        key = build_s3_key("Flask_Compress", "Flask_Compress-1.14-py3-none-any.whl")
        self.assertEqual(key, "pypi/flask_compress/Flask_Compress-1.14-py3-none-any.whl")

    def test_rejects_path_in_filename(self) -> None:
        with self.assertRaises(SystemExit):
            build_s3_key("psutil", f"evil/../{_WHEEL}")

    def test_rejects_prefix_segment_in_filename(self) -> None:
        with self.assertRaises(SystemExit):
            build_s3_key("psutil", f"pypi/other_dir/{_WHEEL}")

    def test_rejects_windows_separator_in_filename(self) -> None:
        with self.assertRaises(SystemExit):
            build_s3_key("psutil", f"evil\\{_WHEEL}")

    def test_rejects_dotdot_in_package(self) -> None:
        with self.assertRaises(SystemExit):
            build_s3_key("..", _WHEEL)

    def test_rejects_index_html(self) -> None:
        with self.assertRaises(SystemExit):
            build_s3_key("psutil", "index.html")


class TestParseManifest(unittest.TestCase):
    def test_parses_comma_lines(self) -> None:
        entries = parse_manifest(f"# comment\npsutil,{_WHEEL}\n")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].package, "psutil")

    def test_parses_semicolon_records(self) -> None:
        text = f"# package_name, filename; psutil,{_WHEEL}; flask_compress,Flask_Compress-1.14-py3-none-any.whl"
        entries = parse_manifest(text)
        self.assertEqual([entry.package for entry in entries], ["psutil", "flask_compress"])

    def test_rejects_duplicates(self) -> None:
        with self.assertRaises(SystemExit):
            parse_manifest(f"psutil,{_WHEEL}\npsutil,{_WHEEL}\n")

    def test_rejects_path_before_building_a_key(self) -> None:
        with self.assertRaises(SystemExit):
            parse_manifest(f"psutil,evil/../{_WHEEL}\n")


class TestLoadManifest(unittest.TestCase):
    def test_missing_file(self) -> None:
        with self.assertRaises(SystemExit):
            load_manifest("no-such-delete-manifest.txt")

    def test_reads_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.txt"
            path.write_text(f"psutil,{_WHEEL}\n", encoding="utf-8")
            entries = load_manifest(str(path))
        self.assertEqual(entries[0].filename, _WHEEL)

    @patch("delete_index_artifact.sys.stdin", StringIO(f"psutil,{_WHEEL}\n"))
    def test_reads_stdin(self) -> None:
        entries = load_manifest("-")
        self.assertEqual(entries[0].package, "psutil")


class TestMain(unittest.TestCase):
    def test_prints_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.txt"
            path.write_text(f"Psutil,{_WHEEL}\n", encoding="utf-8")
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                rc = main(["--expected-count", "1", "--manifest", str(path)])
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue(), f"pypi/psutil/{_WHEEL}\n")

    def test_expected_count_mismatch_prints_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.txt"
            path.write_text(f"psutil,{_WHEEL}\n", encoding="utf-8")
            with patch("sys.stdout", new_callable=StringIO) as stdout:
                with self.assertRaises(SystemExit):
                    main(["--expected-count", "2", "--manifest", str(path)])
        self.assertEqual(stdout.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
