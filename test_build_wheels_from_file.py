#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import sys
import unittest

from unittest.mock import patch

# build_wheels_from_file parses argv at import time; load it with a minimal argv first.
with patch.object(sys, "argv", ["build_wheels_from_file.py", "-r"]):
    import build_wheels_from_file as bwf


class TestBuildWheelsFromFile(unittest.TestCase):
    @patch.object(bwf.platform, "system", return_value="Darwin")
    def test_force_interpreter_is_linux_only(self, _system) -> None:
        self.assertFalse(bwf._apply_force_interpreter_binary(True))

    @patch.object(bwf, "build_one", return_value="succeeded")
    def test_build_requirement_delegates_to_build_one(self, build_one) -> None:
        self.assertEqual(
            bwf._build_requirement("requests==2.32.0", force_interpreter=True, resolved_exact_pin=True),
            "succeeded",
        )
        build_one.assert_called_once_with(
            "requests==2.32.0",
            force_interpreter=True,
            no_deps=True,
        )

    def test_resolve_dependent_lines_without_inputs_is_unchanged(self) -> None:
        lines = ["requests==2.32.0"]
        self.assertEqual(bwf._resolve_dependent_lines(lines, "missing"), (lines, False))


if __name__ == "__main__":
    unittest.main()
