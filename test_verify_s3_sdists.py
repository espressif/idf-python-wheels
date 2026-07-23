#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import tempfile
import unittest

from pathlib import Path

from verify_s3_sdists import _load_requirement_lines
from verify_s3_sdists import verify_sdists_on_s3


class TestVerifyS3Sdists(unittest.TestCase):
    def test_invalid_requirement_line_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sdist_requirements.txt"
            path.write_text("not a requirement!!!\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                _load_requirement_lines(path)

    def test_valid_requirement_lines_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sdist_requirements.txt"
            path.write_text("esptool\n# comment\ngdbgui==0.13.2.0\n", encoding="utf-8")
            requirements = _load_requirement_lines(path)
            self.assertEqual([str(r) for r in requirements], ["esptool", "gdbgui==0.13.2.0"])

    def test_strict_fails_when_requirements_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.txt"
            self.assertEqual(verify_sdists_on_s3("dummy-bucket", missing, strict=True), 1)

    def test_non_strict_skips_when_requirements_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.txt"
            self.assertEqual(verify_sdists_on_s3("dummy-bucket", missing, strict=False), 0)


if __name__ == "__main__":
    unittest.main()
