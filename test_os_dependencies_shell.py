#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parent
PREPARE_PATH = ROOT / "os_dependencies" / "linux_armv7_docker_prepare.sh"
LINUX_ARM = (ROOT / "os_dependencies" / "linux_arm.sh").read_text(encoding="utf-8")

# The prepare script only needs apt-get stubbed out; everything else it touches
# (``/etc/os-release``, ``uname``) is read-only on a Linux runner.
APT_STUB = "#!/bin/sh\nexit 0\n"


def _prepare_errexit_after_sourcing(caller_errexit: bool) -> str:
    """Return "on"/"off" for errexit in a shell that sourced the prepare script."""
    with tempfile.TemporaryDirectory() as tmp:
        stub_dir = Path(tmp) / "bin"
        stub_dir.mkdir()
        apt_get = stub_dir / "apt-get"
        apt_get.write_text(APT_STUB, encoding="utf-8")
        apt_get.chmod(0o755)

        script = "\n".join(
            [
                "set -e" if caller_errexit else "set +e",
                f'. "{PREPARE_PATH}"',
                'case "$-" in *e*) echo errexit=on ;; *) echo errexit=off ;; esac',
            ]
        )
        env = dict(os.environ, PATH=f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
        result = subprocess.run(
            ["bash", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip().rpartition("errexit=")[2]


@unittest.skipIf(
    sys.platform == "win32" or not shutil.which("bash") or not Path("/etc/os-release").exists(),
    "prepare script is sourced in Linux Docker images",
)
class TestArmv7PrepareShellOptions(unittest.TestCase):
    def test_keeps_caller_errexit(self) -> None:
        # A ``$(set +o)`` snapshot reports errexit as off, because bash clears it inside the
        # command substitution of an assignment. Restoring that snapshot disabled ``set -e`` in
        # the workflow shell, so a failing ``linux_arm.sh`` (no Rust, no dbus headers installed)
        # looked like success and only surfaced as unrelated wheel build errors an hour later.
        self.assertEqual("on", _prepare_errexit_after_sourcing(caller_errexit=True))

    def test_does_not_leak_errexit(self) -> None:
        self.assertEqual("off", _prepare_errexit_after_sourcing(caller_errexit=False))


class TestLinuxArmDependencies(unittest.TestCase):
    def test_distro_python_headers_are_optional(self) -> None:
        # The bullseye armhf images carry libpython3.9-stdlib from bullseye-security, which is
        # gone from archive.debian.org, so python3-dev has no installable version there. Wheels
        # build against the interpreter on PATH, so a missing python3-dev must not abort setup.
        lines = [line for line in LINUX_ARM.splitlines() if "python3-dev" in line]
        self.assertEqual(1, len(lines))
        self.assertRegex(lines[0], r"python3-dev\s*\|\|")


if __name__ == "__main__":
    unittest.main()
