#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

import tempfile
import unittest

from pathlib import Path

import check_wheel_collisions as cwc


class TestCheckWheelCollisions(unittest.TestCase):
    def test_no_collision_unique_basenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "a" / "foo-1.0-py3-none-any.whl").write_bytes(b"a")
            (root / "b" / "bar-1.0-py3-none-any.whl").write_bytes(b"b")
            self.assertEqual(cwc.collect_collision_errors(root), [])

    def test_collision_same_basename_different_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "linux-armv7").mkdir()
            (root / "linux-armv7legacy").mkdir()
            name = "pkg-1.0-cp39-cp39-linux_armv7l.whl"
            (root / "linux-armv7" / name).write_bytes(b"v1")
            (root / "linux-armv7legacy" / name).write_bytes(b"v2-different")
            errs = cwc.collect_collision_errors(root)
            self.assertEqual(len(errs), 1)
            self.assertIn(name, errs[0])

    def test_same_basename_identical_bytes_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x").mkdir()
            (root / "y").mkdir()
            name = "same-1.0-py3-none-any.whl"
            payload = b"identical"
            (root / "x" / name).write_bytes(payload)
            (root / "y" / name).write_bytes(payload)
            self.assertEqual(cwc.collect_collision_errors(root), [])

    def test_main_returns_one_on_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            name = "dup-1.0-py3-none-any.whl"
            (root / "a" / name).write_bytes(b"1")
            (root / "b" / name).write_bytes(b"2")
            self.assertEqual(cwc.main(["_", str(root)]), 1)


if __name__ == "__main__":
    unittest.main()
