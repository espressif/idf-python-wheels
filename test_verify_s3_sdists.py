#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import tempfile
import unittest

from pathlib import Path

from verify_s3_sdists import _load_requirement_names


class TestVerifyS3Sdists(unittest.TestCase):
    def test_invalid_requirement_line_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sdist_requirements.txt"
            path.write_text("not a requirement!!!\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                _load_requirement_names(path)

    def test_valid_requirement_lines_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sdist_requirements.txt"
            path.write_text("esptool\n# comment\ngdbgui==0.13.2.0\n", encoding="utf-8")
            names = _load_requirement_names(path)
            self.assertEqual(names, ["esptool", "gdbgui"])


if __name__ == "__main__":
    unittest.main()
