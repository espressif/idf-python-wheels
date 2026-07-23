#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from download_sdists import _sdist_download_line
from download_sdists import download_sdists


class TestDownloadSdists(unittest.TestCase):
    def test_downloads_each_line_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "sdist_requirements.txt"
            dest = Path(tmp) / "out"
            req_path.write_text(
                "esptool\nesptool<6 and >=5.3.0.dev0\nesptool~=4.12.dev2\n",
                encoding="utf-8",
            )
            calls: list[list[str]] = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("download_sdists.subprocess.run", side_effect=fake_run):
                rc = download_sdists(req_path, dest_dir=dest)

            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 3)
            for cmd in calls:
                self.assertEqual(cmd[3], "download")
                self.assertEqual(cmd[4], "-r")
            self.assertTrue(calls[0][5].endswith(".txt"))

    def test_reports_failures_without_stopping_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "sdist_requirements.txt"
            dest = Path(tmp) / "out"
            req_path.write_text("pkg-a\npkg-b\n", encoding="utf-8")
            attempt = {"n": 0}

            def fake_run(cmd, **kwargs):
                attempt["n"] += 1
                req_file = Path(cmd[5])
                line = req_file.read_text(encoding="utf-8").strip()
                rc = 1 if line == "pkg-a" else 0
                return type(
                    "R",
                    (),
                    {"returncode": rc, "stdout": "", "stderr": "boom" if rc else ""},
                )()

            with patch("download_sdists.subprocess.run", side_effect=fake_run):
                rc = download_sdists(req_path, dest_dir=dest)

            self.assertEqual(rc, 1)
            self.assertEqual(attempt["n"], 2)

    def test_empty_file_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "sdist_requirements.txt"
            req_path.write_text("# only comments\n\n", encoding="utf-8")
            with patch("download_sdists.subprocess.run") as mock_run:
                rc = download_sdists(req_path, dest_dir=Path(tmp) / "out")
            self.assertEqual(rc, 0)
            mock_run.assert_not_called()

    def test_strips_markers_for_pip_download(self) -> None:
        line = 'gdbgui==0.13.2.0; python_version < "3.11" and sys_platform == "win32"'
        self.assertEqual(_sdist_download_line(line), "gdbgui==0.13.2.0")

    def test_marker_only_line_downloads_by_name(self) -> None:
        line = 'gdbgui; sys_platform == "win32" and python_version < "3.11"'
        self.assertEqual(_sdist_download_line(line), "gdbgui")

    def test_writes_marker_stripped_line_to_pip_requirements_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "sdist_requirements.txt"
            dest = Path(tmp) / "out"
            line = 'gdbgui==0.13.2.0; python_version < "3.11" and sys_platform == "win32"'
            req_path.write_text(line + "\n", encoding="utf-8")
            written: list[str] = []

            def fake_run(cmd, **kwargs):
                written.append(Path(cmd[5]).read_text(encoding="utf-8").strip())
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("download_sdists.subprocess.run", side_effect=fake_run):
                rc = download_sdists(req_path, dest_dir=dest)

            self.assertEqual(rc, 0)
            self.assertEqual(written, ["gdbgui==0.13.2.0"])


if __name__ == "__main__":
    unittest.main()
