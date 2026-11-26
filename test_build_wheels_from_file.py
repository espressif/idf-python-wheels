#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import json
import sys
import unittest

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from packaging.requirements import Requirement

# build_wheels_from_file parses argv at import time; load it with a minimal argv first.
with patch.object(sys, "argv", ["build_wheels_from_file.py", "-r"]):
    import build_wheels_from_file as bwf


class TestDependentRequirementSkipLine(unittest.TestCase):
    @patch.object(bwf, "bounded_pin_without_find_links_skip", return_value=(False, ""))
    @patch.object(bwf, "_pypi_preflight_skip_line", return_value=False)
    @patch.object(
        bwf,
        "find_links_wheel_build_skip",
        return_value=(True, "find-links already has wheel"),
    )
    def test_force_interpreter_bypasses_find_links_skip_for_normal_packages(
        self,
        _mock_find_links,
        _mock_pypi,
        _mock_armv7,
    ) -> None:
        self.assertFalse(
            bwf._dependent_requirement_skip_line("requests>=2.28.0", force_interpreter=True),
        )

    @patch.object(bwf, "prune_ci_macos_newer_than_pypi_mirror")
    @patch.object(bwf, "bounded_pin_without_find_links_skip", return_value=(False, ""))
    @patch.object(bwf, "_pypi_preflight_skip_line", return_value=False)
    @patch.object(
        bwf,
        "find_links_wheel_build_skip",
        return_value=(True, "find-links already has wheel"),
    )
    @patch.object(bwf, "is_linux_armv7_runner", return_value=False)
    def test_force_interpreter_still_applies_find_links_skip_for_cryptography(
        self,
        _mock_armv7,
        _mock_find_links,
        _mock_pypi,
        _mock_armv7_pin,
        _mock_prune,
    ) -> None:
        self.assertTrue(
            bwf._dependent_requirement_skip_line("cryptography>=2.1.4", force_interpreter=True),
        )

    @patch.object(bwf, "prune_ci_macos_newer_than_pypi_mirror")
    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    @patch.object(bwf, "bounded_pin_without_find_links_skip", return_value=(False, ""))
    @patch.object(bwf, "_pypi_preflight_skip_line", return_value=False)
    @patch.object(
        bwf,
        "find_links_wheel_build_skip",
        return_value=(
            True,
            "find-links already has cryptography 49.0.0 matching >=2.1.4",
        ),
    )
    def test_armv7_skips_cryptography_find_links(
        self,
        _mock_find_links,
        _mock_pypi,
        _mock_armv7_pin,
        _mock_armv7,
        _mock_prune,
    ) -> None:
        self.assertTrue(
            bwf._dependent_requirement_skip_line("cryptography>=2.1.4", force_interpreter=True),
        )

    @patch.object(bwf, "prune_ci_macos_newer_than_pypi_mirror")
    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    @patch.object(bwf, "bounded_pin_without_find_links_skip", return_value=(False, ""))
    @patch.object(bwf, "_pypi_preflight_skip_line", return_value=False)
    @patch.object(
        bwf,
        "find_links_wheel_build_skip",
        return_value=(
            True,
            "find-links has cryptography up to 49.0.0 but none match <43",
        ),
    )
    def test_armv7_still_skips_obsolete_cryptography_pin(
        self,
        _mock_find_links,
        _mock_pypi,
        _mock_armv7_pin,
        _mock_armv7,
        _mock_prune,
    ) -> None:
        self.assertTrue(
            bwf._dependent_requirement_skip_line("cryptography<43", force_interpreter=True),
        )

    @patch.object(bwf, "macos_intel_cryptography_rebuild_instead_of_find_links_skip", return_value=True)
    @patch.object(bwf, "bounded_pin_without_find_links_skip", return_value=(False, ""))
    @patch.object(bwf, "_pypi_preflight_skip_line", return_value=False)
    @patch.object(
        bwf,
        "find_links_wheel_build_skip",
        return_value=(
            True,
            "find-links already has cryptography 49.0.0 matching >=2.1.4",
        ),
    )
    def test_macos_intel_rebuilds_stale_cryptography_find_links(
        self,
        _mock_find_links,
        _mock_pypi,
        _mock_armv7,
        _mock_rebuild,
    ) -> None:
        self.assertFalse(
            bwf._dependent_requirement_skip_line("cryptography>=2.1.4", force_interpreter=True),
        )

    @patch.object(bwf, "prune_ci_macos_newer_than_pypi_mirror")
    @patch.object(bwf, "bounded_pin_without_find_links_skip", return_value=(False, ""))
    @patch.object(bwf, "_pypi_preflight_skip_line", return_value=False)
    @patch.object(
        bwf,
        "find_links_wheel_build_skip",
        return_value=(True, "find-links already has wheel"),
    )
    def test_find_links_skip_when_not_forcing_interpreter(
        self,
        _mock_find_links,
        _mock_pypi,
        _mock_armv7,
        mock_prune,
    ) -> None:
        self.assertTrue(
            bwf._dependent_requirement_skip_line("cryptography>=2.1.4", force_interpreter=False),
        )
        mock_prune.assert_called_once()

    @patch.object(bwf.platform, "system", return_value="Darwin")
    def test_force_interpreter_ignored_on_macos(self, _sys) -> None:
        self.assertFalse(bwf._apply_force_interpreter_binary(True))
        self.assertEqual(bwf._force_interpreter_no_binary_args("cffi>=1.15"), [])

    @patch.object(bwf.platform, "system", return_value="Linux")
    def test_force_interpreter_no_binary_on_linux(self, _sys) -> None:
        self.assertTrue(bwf._apply_force_interpreter_binary(True))
        self.assertEqual(bwf._force_interpreter_no_binary_args("cffi>=1.15"), ["--no-binary", "cffi"])


class TestStage2RequirementLoading(unittest.TestCase):
    def test_required_idf_inputs_do_not_fall_back_to_unconstrained_lines(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requirements_dir = Path(temp_dir)
            (requirements_dir / "dependent_requirements.txt").write_text("requests\n", encoding="utf-8")
            with patch.object(bwf.args, "require_idf_inputs", True):
                with self.assertRaisesRegex(SystemExit, "IDF branch inputs file not found"):
                    bwf._load_stage2_requirements(str(requirements_dir))

    def test_resolves_deferred_lines_with_idf_inputs_and_drops_esptool(self) -> None:
        with TemporaryDirectory() as temp_dir:
            requirements_dir = Path(temp_dir)
            (requirements_dir / "dependent_requirements.txt").write_text(
                "esptool\ndbus-python\nrequests\n",
                encoding="utf-8",
            )
            branches = [
                {
                    "branch": "release/v6.0",
                    "idf_major": 6,
                    "requirements": ["requests"],
                    "constraints": ["requests<=3"],
                }
            ]
            (requirements_dir / "idf_inputs.json").write_text(
                json.dumps({"branches": branches}),
                encoding="utf-8",
            )
            with patch.object(bwf, "resolve_branch_subset", return_value={Requirement("requests==2.0")}) as resolve:
                with patch.object(bwf, "filter_requirements_by_pypi_requires_python", side_effect=lambda pins: pins):
                    result = bwf._load_stage2_requirements(str(requirements_dir))

        self.assertEqual(result, ["requests==2.0"])
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[1], ["dbus-python", "requests"])


if __name__ == "__main__":
    unittest.main()
