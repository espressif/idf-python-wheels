#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import unittest

from pathlib import Path

from armv7_docker_image import armv7_docker_spec
from armv7_docker_image import armv7_plat_for_artifact_dir
from armv7_docker_image import armv7_plat_newer_than
from armv7_docker_image import resolve_armv7_plat


class TestArmv7DockerImage(unittest.TestCase):
    def test_current_stays_on_bookworm_before_3_15(self) -> None:
        self.assertEqual(
            armv7_docker_spec("3.14", "current"),
            ("python:3.14-bookworm", "manylinux_2_36_armv7l"),
        )

    def test_current_uses_trixie_from_3_15(self) -> None:
        self.assertEqual(
            armv7_docker_spec("3.15", "current"),
            ("python:3.15-trixie", "manylinux_2_41_armv7l"),
        )

    def test_legacy_stays_on_bullseye_before_3_15(self) -> None:
        self.assertEqual(
            armv7_docker_spec("3.12", "legacy"),
            ("python:3.12-bullseye", "manylinux_2_31_armv7l"),
        )

    def test_legacy_uses_bookworm_from_3_15(self) -> None:
        self.assertEqual(
            armv7_docker_spec("3.15", "legacy"),
            ("python:3.15-bookworm", "manylinux_2_36_armv7l"),
        )

    def test_artifact_dir_uses_the_build_image_not_the_wheel_tag(self) -> None:
        """abi3 filenames name the minimum Python. The artifact directory is the image."""
        current_315 = Path(
            "downloaded_wheels/wheels-download-directory-linux-armv7-3.15/psutil-7.2.2-cp36-abi3-linux_armv7l.whl"
        )
        current_312 = Path(
            "downloaded_wheels/wheels-download-directory-linux-armv7-3.12/psutil-7.2.2-cp36-abi3-linux_armv7l.whl"
        )
        legacy_315 = Path(
            "downloaded_wheels/wheels-download-directory-linux-armv7legacy-3.15"
            "/pycryptodome-3.23.0-cp37-abi3-linux_armv7l.whl"
        )
        legacy_312 = Path(
            "downloaded_wheels/wheels-download-directory-linux-armv7legacy-3.12/pkg-1.0-cp312-cp312-linux_armv7l.whl"
        )
        self.assertEqual(resolve_armv7_plat(current_315, "manylinux_2_36_armv7l"), "manylinux_2_41_armv7l")
        self.assertEqual(resolve_armv7_plat(current_312, "manylinux_2_36_armv7l"), "manylinux_2_36_armv7l")
        self.assertEqual(resolve_armv7_plat(legacy_315, "manylinux_2_31_armv7l"), "manylinux_2_36_armv7l")
        self.assertEqual(resolve_armv7_plat(legacy_312, "manylinux_2_31_armv7l"), "manylinux_2_31_armv7l")
        self.assertIsNone(armv7_plat_for_artifact_dir("wheels-download-directory-linux-x86_64-3.15"))
        flat = Path("downloaded_wheels/pkg-1.0-cp315-cp315-linux_armv7l.whl")
        self.assertEqual(resolve_armv7_plat(flat, "manylinux_2_36_armv7l"), "manylinux_2_36_armv7l")

    def test_trixie_plat_is_newer_than_the_bookworm_repair_image(self) -> None:
        self.assertTrue(armv7_plat_newer_than("manylinux_2_41_armv7l", "manylinux_2_36_armv7l"))
        self.assertTrue(armv7_plat_newer_than("manylinux_2_36_armv7l", "manylinux_2_31_armv7l"))
        self.assertFalse(armv7_plat_newer_than("manylinux_2_36_armv7l", "manylinux_2_36_armv7l"))
        self.assertFalse(armv7_plat_newer_than("manylinux_2_31_armv7l", "manylinux_2_36_armv7l"))


if __name__ == "__main__":
    unittest.main()
