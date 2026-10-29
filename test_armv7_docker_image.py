#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import unittest

from armv7_docker_image import armv7_docker_spec


class TestArmv7DockerImage(unittest.TestCase):
    def test_current_stays_on_bookworm(self) -> None:
        self.assertEqual(
            armv7_docker_spec("3.12", "current"),
            ("python:3.12-bookworm", "manylinux_2_36_armv7l"),
        )
        self.assertEqual(
            armv7_docker_spec("3.14", "current"),
            ("python:3.14-bookworm", "manylinux_2_36_armv7l"),
        )
        self.assertEqual(
            armv7_docker_spec("3.15", "current"),
            ("python:3.15-bookworm", "manylinux_2_36_armv7l"),
        )

    def test_legacy_stays_on_bullseye_before_3_14(self) -> None:
        self.assertEqual(
            armv7_docker_spec("3.12", "legacy"),
            ("python:3.12-bullseye", "manylinux_2_31_armv7l"),
        )

    def test_legacy_has_no_bullseye_image_from_3_14(self) -> None:
        with self.assertRaises(ValueError):
            armv7_docker_spec("3.14", "legacy")
        with self.assertRaises(ValueError):
            armv7_docker_spec("3.15", "legacy")


if __name__ == "__main__":
    unittest.main()
