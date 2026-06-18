#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import unittest

from _helper_functions import native_import_guard_by_name


class TestNativeImportGuard(unittest.TestCase):
    def test_loads_cryptography_fallback(self) -> None:
        guarded = native_import_guard_by_name()
        entry = guarded["cryptography"]
        code = "\n".join(entry.imports)
        self.assertIn("_rust", code)
        self.assertIn("_openssl", code)
        self.assertLess(code.index("_rust"), code.index("_openssl"))


if __name__ == "__main__":
    unittest.main()
