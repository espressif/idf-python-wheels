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

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from emit_sdist_requirements import _version_buildable
from emit_sdist_requirements import compute_sdist_requirements
from emit_sdist_requirements import write_sdist_requirements_file


def _req(line: str) -> Requirement:
    return Requirement(line)


class TestVersionBuildable(unittest.TestCase):
    def test_overlap_from_specifier_bounds(self) -> None:
        assembled = _req("cryptography>=43.0.0")
        after = _req("cryptography<49,>=2.1.4")
        self.assertTrue(_version_buildable(assembled, after))

    def test_no_overlap_when_ranges_disjoint(self) -> None:
        assembled = _req("cryptography>=43.0.0")
        after = _req("cryptography<43,>=2.1.4")
        self.assertFalse(_version_buildable(assembled, after))

    def test_exact_pin_matches_open_range(self) -> None:
        assembled = _req("gdbgui==0.13.2.0")
        after = _req("gdbgui>=0.1.0")
        self.assertTrue(_version_buildable(assembled, after))

    def test_strict_greater_than_uses_bumped_probe(self) -> None:
        assembled = _req("pkg>1.0.0")
        after = _req("pkg<2.0.0")
        self.assertTrue(_version_buildable(assembled, after))

    def test_year_style_bounds(self) -> None:
        assembled = _req("certifi>=2023.0.0")
        after = _req("certifi>=2024.0.0")
        self.assertTrue(_version_buildable(assembled, after))


class TestComputeSdistRequirements(unittest.TestCase):
    def test_includes_package_excluded_on_one_platform(self) -> None:
        """dbus-python excluded on win32 only must still get an sdist on the index."""
        assembled = {
            _req("dbus-python>=1.2.0"),
            _req("requests>=2.28.0"),
        }

        def fake_exclude(assembled_set, exclude_reqs, print_requirements=True):
            markers = {str(r.marker) for r in exclude_reqs if r.marker}
            if any("win32" in m for m in markers):
                return {r for r in assembled_set if canonicalize_name(r.name) != "dbus-python"}
            return set(assembled_set)

        with patch("emit_sdist_requirements.YAMLListAdapter") as mock_adapter:
            mock_adapter.return_value.requirements = {_req("dbus-python; sys_platform == 'win32'")}
            with patch("build_wheels.exclude_from_requirements", side_effect=fake_exclude):
                with patch("emit_sdist_requirements.SDIST_EVAL_PLATFORMS", ("windows", "linux")):
                    result = compute_sdist_requirements(assembled)

        names = {r.name for r in result}
        self.assertIn("dbus-python", names)
        self.assertNotIn("requests", names)

    def test_marker_only_requirement_not_forced_sdist_on_other_platforms(self) -> None:
        """Windows-only assembled pins must not force sdists solely because Linux cannot build them."""
        assembled = {
            _req("pywin32>=306; sys_platform == 'win32'"),
            _req("requests>=2.28.0"),
        }

        def fake_exclude(assembled_set, exclude_reqs, print_requirements=True):
            return set(assembled_set)

        with patch("emit_sdist_requirements.YAMLListAdapter") as mock_adapter:
            mock_adapter.return_value.requirements = set()
            with patch("build_wheels.exclude_from_requirements", side_effect=fake_exclude):
                with patch("emit_sdist_requirements.SDIST_EVAL_PLATFORMS", ("linux",)):
                    result = compute_sdist_requirements(assembled)

        names = {r.name for r in result}
        self.assertNotIn("pywin32", names)
        self.assertNotIn("requests", names)

    def test_gdbgui_in_real_exclude_list(self) -> None:
        """gdbgui is fully excluded on all platforms in the repo exclude_list.yaml."""
        assembled = {_req("gdbgui==0.13.2.0"), _req("certifi>=2023.0.0")}
        result = compute_sdist_requirements(assembled)
        names = {r.name for r in result}
        self.assertIn("gdbgui", names)
        self.assertNotIn("certifi", names)

    def test_mixed_markers_do_not_suppress_sdist_for_applicable_env(self) -> None:
        """A Windows-only assembled pin must not mark the package buildable on Linux."""
        assembled = {
            _req("special-lib>=1.0; sys_platform == 'win32'"),
            _req("special-lib>=1.0"),
        }

        def fake_exclude(assembled_set, exclude_reqs, print_requirements=True):
            markers = {str(r.marker) for r in exclude_reqs if r.marker}
            if any("win32" in m for m in markers):
                return {r for r in assembled_set if canonicalize_name(r.name) != "special-lib"}
            return set(assembled_set)

        with patch("emit_sdist_requirements.YAMLListAdapter") as mock_adapter:
            mock_adapter.return_value.requirements = {_req("special-lib; sys_platform == 'win32'")}
            with patch("build_wheels.exclude_from_requirements", side_effect=fake_exclude):
                with patch("emit_sdist_requirements.SDIST_EVAL_PLATFORMS", ("windows", "linux")):
                    result = compute_sdist_requirements(assembled)

        names = {r.name for r in result}
        self.assertIn("special-lib", names)


class TestWriteSdistRequirementsFile(unittest.TestCase):
    def test_writes_sorted_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sdist_requirements.txt"
            count = write_sdist_requirements_file(path, [_req("pygobject>=3.42.0"), _req("cffi>=1.15.0")])
            self.assertEqual(count, 2)
            text = path.read_text(encoding="utf-8")
            self.assertIn("cffi>=1.15.0", text)
            self.assertIn("pygobject>=3.42.0", text)


if __name__ == "__main__":
    unittest.main()
