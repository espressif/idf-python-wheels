#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parent
HELPER_PATH = ROOT / "os_dependencies" / "debian_eol_apt.sh"
HELPER = HELPER_PATH.read_text(encoding="utf-8")
PREPARE = (ROOT / "os_dependencies" / "linux_armv7_docker_prepare.sh").read_text(encoding="utf-8")


class TestDebianEolApt(unittest.TestCase):
    def test_helper_ignores_expired_inrelease_on_bullseye_only(self) -> None:
        self.assertIn("bullseye", HELPER)
        self.assertIn('Acquire::Check-Valid-Until "false"', HELPER)
        self.assertIn("debian_prepare_eol_apt", HELPER)
        self.assertNotIn("bookworm)", HELPER)

    def test_prepare_applies_helper_before_apt_update(self) -> None:
        self.assertIn("debian_eol_apt.sh", PREPARE)
        self.assertIn("debian_prepare_eol_apt /", PREPARE)
        self.assertLess(
            PREPARE.index("debian_prepare_eol_apt"),
            PREPARE.index("apt-get update"),
        )

    @unittest.skipIf(
        sys.platform == "win32" or not shutil.which("bash"),
        "helper is sourced in Linux Docker, not on Windows",
    )
    def test_bullseye_writes_apt_conf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(
                [
                    "bash",
                    "-c",
                    f'. "{HELPER_PATH}" && debian_prepare_eol_apt "{root}" bullseye',
                ],
                check=True,
            )
            conf = root / "etc" / "apt" / "apt.conf.d" / "99no-check-valid-until"
            self.assertTrue(conf.is_file())
            self.assertIn('Acquire::Check-Valid-Until "false"', conf.read_text(encoding="utf-8"))

    @unittest.skipIf(
        sys.platform == "win32" or not shutil.which("bash"),
        "helper is sourced in Linux Docker, not on Windows",
    )
    def test_bookworm_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(
                [
                    "bash",
                    "-c",
                    f'. "{HELPER_PATH}" && debian_prepare_eol_apt "{root}" bookworm',
                ],
                check=True,
            )
            self.assertFalse((root / "etc").exists())
