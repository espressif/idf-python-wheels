# ruff: noqa: E501
# line too long skip in ruff for whole file (formatting would be worst than long lines)
#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
import os
import sys
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from typing import Dict
from unittest.mock import MagicMock
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from _helper_functions import armv7_pip_wheel_subprocess_env
from _helper_functions import armv7_rebuild_instead_of_find_links_skip
from _helper_functions import bounded_pin_without_find_links_skip
from _helper_functions import current_interpreter_satisfies_requires_python
from _helper_functions import filter_requirements_by_pypi_requires_python
from _helper_functions import find_links_wheel_build_skip
from _helper_functions import force_interpreter_skip_package
from _helper_functions import get_cryptography_macos_intel_pip_wheel_args
from _helper_functions import get_current_platform
from _helper_functions import get_no_binary_args
from _helper_functions import macos_intel_cryptography_rebuild_instead_of_find_links_skip
from _helper_functions import merge_requirements
from _helper_functions import pypi_requires_python_preflight_skip
from _helper_functions import remove_find_links_wheels_for_package
from build_wheels import _add_into_requirements
from build_wheels import _download_branch_constraints
from build_wheels import _pin_markers_for_other_interpreters
from build_wheels import collect_idf_inputs
from build_wheels import constraint_requirements_for_sdist
from build_wheels import exclude_from_requirements
from build_wheels import get_python_dependent_wheels
from build_wheels import get_used_idf_branches
from build_wheels import requirements_for_other_interpreters
from build_wheels import retain_exact_pins
from idf_branch_resolve import BranchResolveError
from idf_branch_resolve import _constraints_without_excluded_pins
from idf_branch_resolve import _pip_resolve_failure_detail
from idf_branch_resolve import _requirements_for_resolution
from idf_branch_resolve import _resolve_lines
from idf_branch_resolve import _resolve_transient_failure
from idf_branch_resolve import branch_targets_current_interpreter
from idf_branch_resolve import pins_from_pip_report
from idf_branch_resolve import pins_from_pip_report_for_branch
from idf_branch_resolve import resolve_branch_subset
from idf_branch_resolve import resolve_branches
from idf_branch_resolve import union_pin_sets
from wheel_compat_policy import _mirrored_manylinux228_version
from wheel_compat_policy import get_macos_only_binary_args
from wheel_compat_policy import mirror_pypi_manylinux228_wheel
from wheel_compat_policy import pip_wheel_or_mirror_success
from wheel_compat_policy import prune_ci_macos_newer_than_pypi_mirror
from wheel_compat_policy import prune_ci_manylinux_newer_than_228
from wheel_compat_policy import prune_ci_manylinux_newer_than_228_when_228_mirror_present
from wheel_compat_policy import run_pip_wheel
from wheel_compat_policy import should_skip_linux_auditwheel_for_pypi_mirror
from wheel_compat_policy import should_skip_macos_delocate_for_pypi_mirror
from wheel_compat_policy import without_only_binary_all
from yaml_list_adapter import ExcludeRequirements
from yaml_list_adapter import YAMLListAdapter
from yaml_list_adapter import exclusion_constraint_alternatives


class TestChangeSpecifierLogic(unittest.TestCase):
    """Test the _change_specifier_logic method."""

    def setUp(self):
        """Create a YAMLListAdapter instance for testing."""
        # Create instance with a minimal valid YAML file
        self.adapter = YAMLListAdapter.__new__(YAMLListAdapter)
        self.adapter._yaml_list = []
        self.adapter.exclude = False
        self.adapter.requirements = set()

    def test_change_specifier_logic(self):
        """Test that specifier logic is correctly inverted (logical negation)."""
        # The function performs logical negation:
        # > becomes <= (not greater means less or equal)
        # < becomes >= (not less means greater or equal)
        # >= becomes < (not greater-or-equal means less)
        # <= becomes > (not less-or-equal means greater)
        test_cases = (
            (">0.9.0.2", "<=0.9.0.2"),
            ("<0.9.0.2", ">=0.9.0.2"),
            ("==0.9.0.2", "!=0.9.0.2"),
            (">=0.9.0.2", "<0.9.0.2"),
            ("<=0.9.0.2", ">0.9.0.2"),
            ("!=0.9.0.2", "==0.9.0.2"),
            ("===0.9.0.2", "===0.9.0.2"),
        )

        for original, expected in test_cases:
            with self.subTest(original=original):
                new_spec, ver, _ = self.adapter._change_specifier_logic(original)
                result = f"{new_spec}{ver}"
                self.assertEqual(result, expected)


class TestYAMLtoRequirement(unittest.TestCase):
    """Test the _yaml_to_requirement method."""

    def setUp(self):
        """Create a YAMLListAdapter instance for testing."""
        self.adapter = YAMLListAdapter.__new__(YAMLListAdapter)
        self.adapter._yaml_list = []
        self.adapter.exclude = False
        self.adapter.requirements = set()

    def test_simple_package(self):
        """Test conversion of a simple package without markers."""
        yaml_list = [{"package_name": "numpy"}]
        result = self.adapter._yaml_to_requirement(yaml_list)
        self.assertEqual(result, {Requirement("numpy")})

    def test_package_with_version(self):
        """Test conversion of a package with version specifier."""
        yaml_list = [{"package_name": "numpy", "version": "<1.20"}]
        result = self.adapter._yaml_to_requirement(yaml_list)
        self.assertEqual(result, {Requirement("numpy<1.20")})

    def test_package_with_multiple_versions(self):
        """Test conversion of a package with multiple version specifiers."""
        yaml_list = [{"package_name": "numpy", "version": ["<1.20", ">=1.10"]}]
        result = self.adapter._yaml_to_requirement(yaml_list)
        self.assertEqual(result, {Requirement("numpy<1.20,>=1.10")})

    def test_package_with_platform(self):
        """Test conversion of a package with platform marker."""
        yaml_list = [{"package_name": "pywin32", "platform": "win32"}]
        result = self.adapter._yaml_to_requirement(yaml_list)
        self.assertEqual(result, {Requirement("pywin32; sys_platform == 'win32'")})

    def test_package_with_multiple_platforms(self):
        """Test conversion of a package with multiple platform markers."""
        yaml_list = [{"package_name": "pkg", "platform": ["win32", "linux"]}]
        result = self.adapter._yaml_to_requirement(yaml_list)
        self.assertEqual(result, {Requirement("pkg; sys_platform == 'win32' or sys_platform == 'linux'")})

    def test_package_with_python_version(self):
        """Test conversion of a package with python version marker."""
        yaml_list = [{"package_name": "pkg", "python": ">=3.8"}]
        result = self.adapter._yaml_to_requirement(yaml_list)
        self.assertEqual(result, {Requirement("pkg; python_version >= '3.8'")})

    def test_package_with_version_and_platform(self):
        """Test conversion of a package with version and platform."""
        yaml_list = [{"package_name": "numpy", "version": "<=1.20", "platform": "win32"}]
        result = self.adapter._yaml_to_requirement(yaml_list)
        self.assertEqual(result, {Requirement("numpy<=1.20; sys_platform == 'win32'")})

    def test_exclude_simple_platform(self):
        """Test exclude mode with platform marker."""
        yaml_list = [{"package_name": "pkg", "platform": "win32"}]
        result = self.adapter._yaml_to_requirement(yaml_list, exclude=True)
        self.assertEqual(result, {Requirement("pkg; sys_platform != 'win32'")})

    def test_exclude_version(self):
        """Test exclude mode with version specifier."""
        yaml_list = [{"package_name": "numpy", "version": "<1.20"}]
        result = self.adapter._yaml_to_requirement(yaml_list, exclude=True)
        self.assertEqual(result, {Requirement("numpy>=1.20")})

    def test_exclude_platform_and_python_intersection_single_os(self):
        """exclude + platform + python (no package version) = drop only on that OS ∩ Python."""
        yaml_list = [{"package_name": "pydantic_core", "platform": "win32", "python": "==3.14"}]
        result = self.adapter._yaml_to_requirement(yaml_list, exclude=True)
        expected = Requirement("pydantic_core; (sys_platform != 'win32' or (python_version != '3.14'))")
        self.assertEqual(result, {expected})

    def test_exclude_platform_and_python_intersection_two_os(self):
        yaml_list = [{"package_name": "pydantic_core", "platform": ["win32", "darwin"], "python": "==3.14"}]
        result = self.adapter._yaml_to_requirement(yaml_list, exclude=True)
        expected = Requirement(
            "pydantic_core; (sys_platform != 'win32' or (python_version != '3.14')) and "
            "(sys_platform != 'darwin' or (python_version != '3.14'))"
        )
        self.assertEqual(result, {expected})


class TestYAMLListAdapterIntegration(unittest.TestCase):
    """Integration tests using actual YAML files."""

    def test_load_include_list(self):
        """Test loading the include_list.yaml file."""
        try:
            adapter = YAMLListAdapter("include_list.yaml")
            self.assertIsInstance(adapter.requirements, set)
        except FileNotFoundError:
            self.skipTest("include_list.yaml not found")

    def test_load_exclude_list(self):
        """Test loading the exclude_list.yaml file."""
        try:
            adapter = YAMLListAdapter("exclude_list.yaml", exclude=True)
            self.assertIsInstance(adapter.requirements, set)
        except FileNotFoundError:
            self.skipTest("exclude_list.yaml not found")

    def test_dbus_python_1_2_is_linux_315_only(self):
        """dbus-python<1.3 is replaced on Linux 3.15 and stays off Windows.

        An unversioned Linux requirement must stay on 1.2.x before 3.15. Leaving it
        unpinned installs 1.5.1, which needs ninja and fails on Bullseye.
        """
        incoming = {
            Requirement('dbus-python<1.3; python_version > "3.10"'),
            Requirement('dbus-python; sys_platform == "linux"'),
        }
        linux = YAMLListAdapter("exclude_list.yaml", exclude=True, current_platform="linux_x86_64")
        linux_reqs = exclude_from_requirements(incoming, linux.requirements, print_requirements=False)
        linux_315 = {"python_version": "3.15", "sys_platform": "linux"}
        linux_313 = {"python_version": "3.13", "sys_platform": "linux"}

        def active(env: dict) -> list:
            return [req for req in linux_reqs if req.marker is None or req.marker.evaluate(env)]

        active_315 = active(linux_315)
        active_313 = active(linux_313)
        self.assertTrue(active_315)
        self.assertTrue(active_313)
        self.assertTrue(all(req.specifier.contains("1.5.1") for req in active_315))
        self.assertTrue(all(not req.specifier.contains("1.2.18") for req in active_315))
        self.assertTrue(all(req.specifier.contains("1.2.18") for req in active_313))
        self.assertTrue(all(not req.specifier.contains("1.5.1") for req in active_313))

        windows = YAMLListAdapter("exclude_list.yaml", exclude=True, current_platform="windows")
        windows_reqs = exclude_from_requirements(incoming, windows.requirements, print_requirements=False)
        for env in (
            {"python_version": "3.11", "sys_platform": "win32"},
            {"python_version": "3.15", "sys_platform": "win32"},
        ):
            active = [req for req in windows_reqs if req.marker is None or req.marker.evaluate(env)]
            self.assertEqual(active, [])


def _current_platform_wheel_tag():
    """Return a wheel platform tag matching the current OS for is_wheel_compatible tests."""
    if sys.platform == "win32":
        return "win_amd64"
    if sys.platform == "darwin":
        return "macosx_11_0_arm64"
    if sys.platform == "linux":
        return "manylinux_2_17_x86_64"
    return "any"


class TestWheelCompatibility(unittest.TestCase):
    """Test the is_wheel_compatible function from test_wheels_install.py."""

    def setUp(self):
        """Import the function to test."""
        sys.path.insert(0, str(Path(__file__).parent))
        from test_wheels_install import is_wheel_compatible

        self.is_wheel_compatible = is_wheel_compatible

    def test_exact_python_version_match(self):
        """Test that cpXY wheels match the exact Python version."""
        tag = _current_platform_wheel_tag()
        self.assertTrue(self.is_wheel_compatible(f"numpy-1.0.0-cp311-cp311-{tag}.whl", "311"))
        self.assertFalse(self.is_wheel_compatible(f"numpy-1.0.0-cp310-cp310-{tag}.whl", "311"))

    def test_universal_py3_wheel(self):
        """Test that py3 wheels are compatible with any Python 3."""
        self.assertTrue(self.is_wheel_compatible("six-1.0.0-py3-none-any.whl", "311"))
        self.assertTrue(self.is_wheel_compatible("six-1.0.0-py3-none-any.whl", "39"))

    def test_universal_py2_py3_wheel(self):
        """Test that py2.py3 wheels are compatible with any Python."""
        self.assertTrue(self.is_wheel_compatible("six-1.0.0-py2.py3-none-any.whl", "311"))
        self.assertTrue(self.is_wheel_compatible("six-1.0.0-py2.py3-none-any.whl", "39"))

    def test_abi3_wheel(self):
        """Test that abi3 wheels are compatible."""
        tag = _current_platform_wheel_tag()
        self.assertTrue(self.is_wheel_compatible(f"cryptography-41.0.0-cp39-abi3-{tag}.whl", "311"))
        self.assertTrue(self.is_wheel_compatible(f"cryptography-41.0.0-cp39-abi3-{tag}.whl", "39"))

    @patch.dict(
        os.environ,
        {"AUDITWHEEL_PLAT": "manylinux_2_31_armv7l", "AUDITWHEEL_ONLY_PLAT": "1"},
        clear=False,
    )
    @patch("test_wheels_install.sys.platform", "linux")
    @patch("test_wheels_install.platform.machine", return_value="armv7l")
    @patch("test_wheels_install.platform.system", return_value="Linux")
    def test_armv7_legacy_rejects_bookworm_manylinux_tags(self, _sys, _machine):
        """Legacy Bullseye tests must not install Bookworm manylinux_2_36 wheels."""
        self.assertTrue(self.is_wheel_compatible("cffi-2.0.0-cp311-cp311-manylinux_2_31_armv7l.whl", "311"))
        self.assertFalse(self.is_wheel_compatible("cffi-2.0.0-cp311-cp311-manylinux_2_36_armv7l.whl", "311"))
        self.assertFalse(
            self.is_wheel_compatible(
                "cryptography-47.0.0-cp38-abi3-manylinux_2_31_armv7l.manylinux_2_36_armv7l.whl",
                "311",
            )
        )

    @patch.dict(
        os.environ,
        {"AUDITWHEEL_PLAT": "manylinux_2_36_armv7l", "AUDITWHEEL_ONLY_PLAT": "1"},
        clear=False,
    )
    @patch("test_wheels_install.sys.platform", "linux")
    @patch("test_wheels_install.platform.machine", return_value="armv7l")
    @patch("test_wheels_install.platform.system", return_value="Linux")
    def test_armv7_bookworm_requires_manylinux_236(self, _sys, _machine):
        self.assertTrue(self.is_wheel_compatible("cffi-2.0.0-cp311-cp311-manylinux_2_36_armv7l.whl", "311"))
        self.assertFalse(self.is_wheel_compatible("cffi-2.0.0-cp311-cp311-manylinux_2_31_armv7l.whl", "311"))
        self.assertTrue(
            self.is_wheel_compatible(
                "cryptography-49.0.0-cp311-abi3-manylinux_2_31_armv7l.manylinux_2_36_armv7l.whl",
                "311",
            )
        )

    @patch.dict(
        os.environ,
        {"AUDITWHEEL_PLAT": "manylinux_2_36_armv7l", "AUDITWHEEL_ONLY_PLAT": "1"},
        clear=False,
    )
    @patch("test_wheels_install.sys.platform", "linux")
    @patch("test_wheels_install.platform.machine", return_value="armv7l")
    @patch("test_wheels_install.platform.system", return_value="Linux")
    def test_armv7_bookworm_skips_cryptography_native_probe(self, _sys, _machine):
        from test_wheels_install import _armv7_skip_cryptography_native_probe

        self.assertTrue(_armv7_skip_cryptography_native_probe("cryptography"))
        self.assertFalse(_armv7_skip_cryptography_native_probe("cffi"))

    @patch.dict(
        os.environ,
        {"AUDITWHEEL_PLAT": "manylinux_2_31_armv7l", "AUDITWHEEL_ONLY_PLAT": "1"},
        clear=False,
    )
    @patch("test_wheels_install.sys.platform", "linux")
    @patch("test_wheels_install.platform.machine", return_value="armv7l")
    @patch("test_wheels_install.platform.system", return_value="Linux")
    def test_armv7_legacy_does_not_skip_cryptography_native_probe(self, _sys, _machine):
        from test_wheels_install import _armv7_skip_cryptography_native_probe

        self.assertFalse(_armv7_skip_cryptography_native_probe("cryptography"))

    @patch.dict(
        os.environ,
        {"AUDITWHEEL_PLAT": "manylinux_2_31_armv7l", "AUDITWHEEL_ONLY_PLAT": "1"},
        clear=False,
    )
    @patch("test_wheels_install.sys.platform", "linux")
    @patch("test_wheels_install.platform.machine", return_value="armv7l")
    @patch("test_wheels_install.platform.system", return_value="Linux")
    @patch(
        "test_wheels_install.run_import_probes",
        return_value=(False, "ImportError: libssl.so.3: cannot open shared object file"),
    )
    def test_armv7_legacy_fails_cryptography_linked_to_openssl3(self, _probe, _sys, _machine):
        from test_wheels_install import _run_native_import_probes

        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "cryptography-50.0.2-cp313-abi3-manylinux_2_31_armv7l.whl"
            wheel.write_bytes(b"x")
            failed, failures = _run_native_import_probes([wheel])
            self.assertEqual(failed, 1)
            self.assertIn("libssl.so.3", failures[0][1])
            self.assertFalse(wheel.exists())

    @patch.dict(
        os.environ,
        {"AUDITWHEEL_PLAT": "manylinux_2_31_armv7l", "AUDITWHEEL_ONLY_PLAT": "1"},
        clear=False,
    )
    @patch("test_wheels_install.sys.platform", "linux")
    @patch("test_wheels_install.platform.machine", return_value="armv7l")
    @patch("test_wheels_install.platform.system", return_value="Linux")
    @patch(
        "test_wheels_install.run_import_probes",
        return_value=(False, "ImportError: /lib/libc.so.6: version `GLIBC_2.34' not found"),
    )
    def test_armv7_legacy_fails_cryptography_needing_glibc_234(self, _probe, _sys, _machine):
        from test_wheels_install import _run_native_import_probes

        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "cryptography-50.0.2-cp313-abi3-manylinux_2_31_armv7l.whl"
            wheel.write_bytes(b"x")
            failed, failures = _run_native_import_probes([wheel])
            self.assertEqual(failed, 1)
            self.assertIn("GLIBC_2.34", failures[0][1])
            self.assertFalse(wheel.exists())

    @patch("test_wheels_install.platform.machine", return_value="x86_64")
    @patch("test_wheels_install.platform.system", return_value="Linux")
    def test_linux_x86_64_does_not_skip_cryptography_native_probe(self, _sys, _machine):
        from test_wheels_install import _should_skip_native_import_probe

        self.assertFalse(_should_skip_native_import_probe("cryptography"))

    @patch("test_wheels_install.run_import_probes", return_value=(True, "cryptography import OK x509"))
    def test_run_native_import_probes_on_linux_x86_64(self, mock_probe):
        from test_wheels_install import _run_native_import_probes

        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "cryptography-49.0.0-cp311-abi3-manylinux_2_28_x86_64.whl"
            wheel.write_bytes(b"")
            failed, failures = _run_native_import_probes([wheel])
            self.assertEqual(failed, 0)
            self.assertEqual(failures, [])
            mock_probe.assert_called_once()

    @patch("test_wheels_install.run_import_probes", return_value=(False, "undefined symbol: EVP_sm4_cfb128"))
    def test_run_native_import_probes_fails_on_openssl_mismatch(self, mock_probe):
        from test_wheels_install import _run_native_import_probes

        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "cryptography-49.0.0-cp312-abi3-manylinux_2_34_x86_64.whl"
            wheel.write_bytes(b"x")
            failed, failures = _run_native_import_probes([wheel])
            self.assertEqual(failed, 1)
            self.assertIn("EVP_sm4", failures[0][1])
            self.assertFalse(wheel.exists())
            mock_probe.assert_called_once()

    @patch("test_wheels_install.run_import_probes", return_value=(True, "ok"))
    def test_run_native_import_probes_defaults_for_unguarded_platform_wheel(self, mock_probe):
        import zipfile

        from test_wheels_install import _run_native_import_probes

        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "greenlet-3.1.0-cp311-cp311-macosx_10_9_x86_64.whl"
            with zipfile.ZipFile(wheel, "w") as zf:
                zf.writestr("greenlet-3.1.0.dist-info/top_level.txt", "greenlet\n")
            failed, failures = _run_native_import_probes([wheel])
            self.assertEqual(failed, 0)
            self.assertEqual(failures, [])
            mock_probe.assert_called_once()
            self.assertEqual(mock_probe.call_args[0][0], ("import greenlet",))

    @patch("test_wheels_install.run_import_probes")
    def test_run_native_import_probes_skips_pure_any_wheel(self, mock_probe):
        from test_wheels_install import _run_native_import_probes

        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "six-1.16.0-py2.py3-none-any.whl"
            wheel.write_bytes(b"")
            failed, failures = _run_native_import_probes([wheel])
            self.assertEqual(failed, 0)
            self.assertEqual(failures, [])
            mock_probe.assert_not_called()


class TestPruneWheelsForArtifact(unittest.TestCase):
    """``prune_wheels_not_for_current_python`` keeps per-matrix wheels for CI artifacts."""

    def test_prune_removes_other_python_same_platform(self):
        import tempfile

        from test_wheels_install import prune_wheels_not_for_current_python

        tag = _current_platform_wheel_tag()
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / f"drop-1.0-cp310-cp310-{tag}.whl").write_bytes(b"a")
            (d / f"keep-1.0-cp311-cp311-{tag}.whl").write_bytes(b"b")
            (d / "universal-1.0-py3-none-any.whl").write_bytes(b"c")

            removed = prune_wheels_not_for_current_python("311", d)
            self.assertEqual(removed, 1)
            self.assertFalse((d / f"drop-1.0-cp310-cp310-{tag}.whl").exists())
            self.assertTrue((d / f"keep-1.0-cp311-cp311-{tag}.whl").exists())
            self.assertTrue((d / "universal-1.0-py3-none-any.whl").exists())


class TestGetPipWheelExtraArgs(unittest.TestCase):
    def test_bleak_winrt_windows_no_build_isolation(self) -> None:
        from _helper_functions import get_pip_wheel_extra_args

        with patch("_helper_functions.platform.system", return_value="Windows"):
            self.assertEqual(
                get_pip_wheel_extra_args("bleak-winrt==1.2.0"),
                ["--no-build-isolation"],
            )
            self.assertEqual(get_pip_wheel_extra_args("bleak_winrt"), ["--no-build-isolation"])

    @patch("_helper_functions.platform.system", return_value="Linux")
    def test_bleak_winrt_not_on_linux(self, _sys: object) -> None:
        from _helper_functions import get_pip_wheel_extra_args

        self.assertEqual(get_pip_wheel_extra_args("bleak-winrt"), [])

    @patch("_helper_functions.platform.system", return_value="Windows")
    def test_other_packages_unchanged(self, _sys: object) -> None:
        from _helper_functions import get_pip_wheel_extra_args

        self.assertEqual(get_pip_wheel_extra_args("cryptography"), [])

    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_cryptography_no_deps(self, _armv7: object) -> None:
        from _helper_functions import get_pip_wheel_extra_args

        self.assertEqual(get_pip_wheel_extra_args("cryptography==49.0.0"), ["--no-deps"])


class TestPipWheelInvocationArgs(unittest.TestCase):
    @patch("wheel_compat_policy.is_linux_armv7_runner", return_value=True)
    def test_armv7_cffi_uses_build_isolation(self, _armv7: object) -> None:
        from wheel_compat_policy import pip_wheel_invocation_args

        args = pip_wheel_invocation_args()
        self.assertNotIn("--no-build-isolation", args)

    @patch("wheel_compat_policy.is_linux_armv7_runner", return_value=True)
    def test_armv7_any_package_uses_build_isolation(self, _armv7: object) -> None:
        from wheel_compat_policy import pip_wheel_invocation_args

        args = pip_wheel_invocation_args()
        self.assertNotIn("--no-build-isolation", args)

    @patch("wheel_compat_policy.is_linux_armv7_runner", return_value=False)
    def test_non_armv7_keeps_no_build_isolation(self, _armv7: object) -> None:
        from wheel_compat_policy import pip_wheel_invocation_args

        args = pip_wheel_invocation_args()
        self.assertIn("--no-build-isolation", args)

    @patch("wheel_compat_policy.platform.system", return_value="Darwin")
    @patch("wheel_compat_policy.is_linux_armv7_runner", return_value=False)
    def test_macos_pip_wheel_only_binary_all(self, _armv7: object, _sys: object) -> None:
        from wheel_compat_policy import pip_wheel_invocation_args

        args = pip_wheel_invocation_args()
        only_at = args.index("--only-binary")
        self.assertEqual(args[only_at + 1], ":all:")


class TestMacosPypiMirrorPolicy(unittest.TestCase):
    @patch("wheel_compat_policy.platform.system", return_value="Darwin")
    def test_only_binary_args_on_macos(self, _sys: object) -> None:
        self.assertEqual(get_macos_only_binary_args(), ["--only-binary", ":all:"])

    @patch("wheel_compat_policy.platform.system", return_value="Linux")
    def test_only_binary_args_off_linux(self, _sys: object) -> None:
        self.assertEqual(get_macos_only_binary_args(), [])

    @patch("_helper_functions.platform.machine", return_value="x86_64")
    @patch("wheel_compat_policy.platform.system", return_value="Darwin")
    def test_skip_delocate_for_pypi_macos_wheels(self, _sys: object, _mach: object) -> None:
        self.assertTrue(should_skip_macos_delocate_for_pypi_mirror("psutil-7.2.2-cp36-abi3-macosx_10_9_x86_64.whl"))
        self.assertTrue(should_skip_macos_delocate_for_pypi_mirror("cffi-1.17.1-cp311-cp311-macosx_11_0_arm64.whl"))

    @patch("_helper_functions.platform.machine", return_value="x86_64")
    @patch("_helper_functions.platform.system", return_value="Darwin")
    @patch("wheel_compat_policy.platform.system", return_value="Darwin")
    def test_keep_delocate_for_cryptography_intel(self, _sys: object, _hf_sys: object, _mach: object) -> None:
        self.assertFalse(
            should_skip_macos_delocate_for_pypi_mirror("cryptography-49.0.0-cp313-abi3-macosx_10_12_x86_64.whl")
        )

    @patch("_helper_functions.platform.machine", return_value="arm64")
    @patch("_helper_functions.platform.system", return_value="Darwin")
    @patch("wheel_compat_policy.platform.system", return_value="Darwin")
    def test_skip_delocate_for_cryptography_arm(self, _sys: object, _hf_sys: object, _mach: object) -> None:
        self.assertTrue(
            should_skip_macos_delocate_for_pypi_mirror("cryptography-49.0.0-cp313-abi3-macosx_11_0_arm64.whl")
        )

    @patch("wheel_compat_policy.platform.system", return_value="Linux")
    def test_skip_delocate_off_linux(self, _sys: object) -> None:
        self.assertFalse(should_skip_macos_delocate_for_pypi_mirror("psutil-7.2.2-cp36-abi3-macosx_10_9_x86_64.whl"))

    def test_prune_keeps_lowest_macosx_tag_any_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep = links / "cffi-1.17.1-cp311-cp311-macosx_10_9_x86_64.whl"
            drop = links / "cffi-1.17.1-cp311-cp311-macosx_15_0_x86_64.whl"
            other = links / "cffi-1.17.1-cp311-cp311-macosx_11_0_arm64.whl"
            keep.write_bytes(b"pypi")
            drop.write_bytes(b"local")
            other.write_bytes(b"arm")
            removed = prune_ci_macos_newer_than_pypi_mirror(wheel_dir=links)
            self.assertEqual(removed, 1)
            self.assertTrue(keep.exists())
            self.assertFalse(drop.exists())
            self.assertTrue(other.exists())

    def test_prune_noop_when_only_one_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            only = links / "psutil-7.2.2-cp36-abi3-macosx_15_0_x86_64.whl"
            only.write_bytes(b"local")
            self.assertEqual(prune_ci_macos_newer_than_pypi_mirror(wheel_dir=links), 0)
            self.assertTrue(only.exists())

    def test_prune_does_not_drop_other_interpreter_exclusive_abi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep_311 = links / "cffi-1.17.1-cp311-cp311-macosx_10_9_x86_64.whl"
            keep_312 = links / "cffi-1.17.1-cp312-cp312-macosx_15_0_x86_64.whl"
            keep_311.write_bytes(b"pypi")
            keep_312.write_bytes(b"ci")
            self.assertEqual(prune_ci_macos_newer_than_pypi_mirror(wheel_dir=links), 0)
            self.assertTrue(keep_311.exists())
            self.assertTrue(keep_312.exists())

    @patch("wheel_compat_policy.get_current_platform", return_value="macos_x86_64")
    def test_prune_keeps_intel_cryptography_rebuild(self, _plat: object) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            pypi = links / "cryptography-47.0.0-cp38-abi3-macosx_10_9_x86_64.whl"
            rebuild = links / "cryptography-47.0.0-cp38-abi3-macosx_10_12_x86_64.whl"
            pypi.write_bytes(b"pypi")
            rebuild.write_bytes(b"openssl4")
            self.assertEqual(prune_ci_macos_newer_than_pypi_mirror(wheel_dir=links), 0)
            self.assertTrue(pypi.exists())
            self.assertTrue(rebuild.exists())

    @patch("wheel_compat_policy.get_current_platform", return_value="macos_arm64")
    def test_prune_still_drops_cryptography_on_arm(self, _plat: object) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep = links / "cryptography-47.0.0-cp38-abi3-macosx_10_9_x86_64.whl"
            drop = links / "cryptography-47.0.0-cp38-abi3-macosx_15_0_x86_64.whl"
            keep.write_bytes(b"pypi")
            drop.write_bytes(b"ci")
            self.assertEqual(prune_ci_macos_newer_than_pypi_mirror(wheel_dir=links), 1)
            self.assertTrue(keep.exists())
            self.assertFalse(drop.exists())

    def test_prune_abi3_still_drops_competing_exclusive_higher_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep = links / "psutil-7.2.2-cp36-abi3-macosx_10_9_x86_64.whl"
            drop = links / "psutil-7.2.2-cp311-cp311-macosx_15_0_x86_64.whl"
            keep.write_bytes(b"pypi")
            drop.write_bytes(b"ci")
            self.assertEqual(prune_ci_macos_newer_than_pypi_mirror(wheel_dir=links), 1)
            self.assertTrue(keep.exists())
            self.assertFalse(drop.exists())

    def test_prune_x86_64_loses_to_lower_universal2_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep = links / "cffi-1.17.1-cp311-cp311-macosx_11_0_universal2.whl"
            drop = links / "cffi-1.17.1-cp311-cp311-macosx_15_0_x86_64.whl"
            keep.write_bytes(b"pypi")
            drop.write_bytes(b"ci")
            self.assertEqual(prune_ci_macos_newer_than_pypi_mirror(wheel_dir=links), 1)
            self.assertTrue(keep.exists())
            self.assertFalse(drop.exists())

    def test_cpython_ranges_parse_three_digit_tags(self) -> None:
        from wheel_compat_policy import _wheel_cpython_ranges
        from wheel_compat_policy import _wheels_compete_for_same_cpython

        self.assertEqual(
            _wheel_cpython_ranges("cryptography-49.0.0-cp311-abi3-macosx_10_12_x86_64.whl"),
            [((3, 11), None)],
        )
        self.assertEqual(
            _wheel_cpython_ranges("cffi-1.17.1-cp310-cp310-macosx_10_9_x86_64.whl"),
            [((3, 10), (3, 10))],
        )
        self.assertTrue(
            _wheels_compete_for_same_cpython(
                "psutil-7.2.2-cp36-abi3-macosx_10_9_x86_64.whl",
                "psutil-7.2.2-cp311-cp311-macosx_15_0_x86_64.whl",
            )
        )
        self.assertTrue(
            _wheels_compete_for_same_cpython(
                "cryptography-49.0.0-cp311-abi3-manylinux_2_28_x86_64.whl",
                "cryptography-49.0.0-cp312-abi3-manylinux_2_34_x86_64.whl",
            )
        )
        self.assertFalse(
            _wheels_compete_for_same_cpython(
                "cffi-1.17.1-cp311-cp311-macosx_10_9_x86_64.whl",
                "cffi-1.17.1-cp312-cp312-macosx_15_0_x86_64.whl",
            )
        )

    def test_prune_intel_and_x86_64_compete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep = links / "psutil-7.2.2-cp36-abi3-macosx_10_9_intel.whl"
            drop = links / "psutil-7.2.2-cp36-abi3-macosx_15_0_x86_64.whl"
            keep.write_bytes(b"pypi")
            drop.write_bytes(b"ci")
            self.assertEqual(prune_ci_macos_newer_than_pypi_mirror(wheel_dir=links), 1)
            self.assertTrue(keep.exists())
            self.assertFalse(drop.exists())


class TestParseWheelName(unittest.TestCase):
    """Test the parse_wheel_name function from _helper_functions.py."""

    def setUp(self):
        """Import the function to test."""
        from _helper_functions import parse_wheel_name

        self.parse_wheel_name = parse_wheel_name

    def test_parse_simple_wheel(self):
        """Test parsing a simple wheel name."""
        result = self.parse_wheel_name("numpy-1.24.0-cp311-cp311-linux_x86_64.whl")
        self.assertEqual(result, ("numpy", "1.24.0"))

    def test_parse_wheel_with_underscores(self):
        """Test parsing wheel name with underscores (name is normalized to canonical form)."""
        result = self.parse_wheel_name("ruamel_yaml_clib-0.2.8-cp311-cp311-linux_x86_64.whl")
        self.assertEqual(result, ("ruamel-yaml-clib", "0.2.8"))

    def test_parse_wheel_with_pre_release(self):
        """Test parsing wheel name with pre-release version."""
        result = self.parse_wheel_name("package-1.0.0a1-py3-none-any.whl")
        self.assertEqual(result, ("package", "1.0.0a1"))

    def test_parse_universal_wheel(self):
        """Test parsing universal wheel name."""
        result = self.parse_wheel_name("six-1.16.0-py2.py3-none-any.whl")
        self.assertEqual(result, ("six", "1.16.0"))

    def test_parse_wheel_pep440_epoch(self):
        """Test parsing wheel with PEP 440 epoch (e.g. 1!1.0)."""
        result = self.parse_wheel_name("pkg-1!1.0-py3-none-any.whl")
        self.assertEqual(result, ("pkg", "1!1.0"))

    def test_parse_wheel_pep440_local_version(self):
        """Test parsing wheel with PEP 440 local version (e.g. 1.0+cpu)."""
        result = self.parse_wheel_name("pkg-1.0+cpu-py3-none-any.whl")
        self.assertEqual(result, ("pkg", "1.0+cpu"))


class TestShouldExcludeWheel(unittest.TestCase):
    """Test the direct exclusion-rule path used by the wheel pipeline."""

    def setUp(self):
        """Import the function to test."""
        from _helper_functions import should_exclude_wheel
        from yaml_list_adapter import load_exclude_rules

        self.should_exclude_wheel = should_exclude_wheel
        self.rules = load_exclude_rules("exclude_list.yaml")

    def test_exclude_by_package_name_only(self):
        """Test excluding a package by name only (no inversion needed)."""
        result, reason = self.should_exclude_wheel("esptool-4.0.0-py3-none-any.whl", self.rules)
        self.assertTrue(result)
        self.assertIn("esptool", reason)

    @patch("_helper_functions.sys.version_info", SimpleNamespace(major=3, minor=11, micro=0))
    @patch("_helper_functions.get_current_platform", return_value="windows")
    def test_exclude_by_version(self, _platform):
        result, _ = self.should_exclude_wheel("gevent-1.5.0-cp311-cp311-win_amd64.whl", self.rules)
        self.assertTrue(result)
        result, _ = self.should_exclude_wheel("gevent-2.0.0-cp311-cp311-win_amd64.whl", self.rules)
        self.assertFalse(result)

    def test_no_match_returns_false(self):
        """Test that non-matching packages return False."""
        result, _ = self.should_exclude_wheel("numpy-1.24.0-cp311-cp311-linux_x86_64.whl", self.rules)
        self.assertFalse(result)

    @patch("_helper_functions.get_current_platform", return_value="linux_x86_64")
    def test_versioned_rule_only_matches_its_python_marker(self, _platform):
        wheel = "dbus_python-1.2.18-cp38-abi3-manylinux_2_34_x86_64.whl"
        with patch("_helper_functions.sys.version_info", SimpleNamespace(major=3, minor=13, micro=0)):
            old_pin, _ = self.should_exclude_wheel(wheel, self.rules)
        with patch("_helper_functions.sys.version_info", SimpleNamespace(major=3, minor=15, micro=0)):
            excluded_pin, reason = self.should_exclude_wheel(wheel, self.rules)
        self.assertFalse(old_pin)
        self.assertTrue(excluded_pin)
        self.assertIn("dbus-python", reason)

    @patch("_helper_functions.sys.version_info", SimpleNamespace(major=3, minor=13, micro=0))
    def test_exclude_with_version_range(self):
        result, _ = self.should_exclude_wheel("Pillow-9.5.0-cp313-cp313-linux_x86_64.whl", self.rules)
        self.assertTrue(result)
        result, _ = self.should_exclude_wheel("Pillow-10.0.0-cp313-cp313-linux_x86_64.whl", self.rules)
        self.assertFalse(result)

    @patch("_helper_functions.get_current_platform", return_value="linux_armv7")
    def test_abi3_wheel_uses_the_running_interpreter(self, _platform):
        wheel = "tree_sitter_c-0.21.4-cp38-abi3-linux_armv7l.whl"
        with patch("_helper_functions.sys.version_info", SimpleNamespace(major=3, minor=13, micro=0)):
            on_newer, _ = self.should_exclude_wheel(wheel, self.rules)
        with patch("_helper_functions.sys.version_info", SimpleNamespace(major=3, minor=8, micro=0)):
            on_tagged, _ = self.should_exclude_wheel(wheel, self.rules)
        self.assertFalse(on_newer)
        self.assertTrue(on_tagged)

    @patch("_helper_functions.get_current_platform", return_value="linux_x86_64")
    def test_structured_rules_do_not_leak_armv7_exclusions(self, _platform):
        from yaml_list_adapter import load_exclude_rules

        rules = load_exclude_rules("exclude_list.yaml")
        excluded, _ = self.should_exclude_wheel(
            "cryptography-42.0.8-cp38-cp38-linux_x86_64.whl",
            rules,
        )
        self.assertFalse(excluded)


class TestGetUsedIdfBranches(unittest.TestCase):
    """Test the get_used_idf_branches function."""

    @patch("build_wheels.MIN_IDF_MAJOR_VERSION", 5)
    @patch("build_wheels.MIN_IDF_MINOR_VERSION", 0)
    def test_filters_old_branches(self):
        """Test that branches older than minimum version are filtered out."""
        branches = [
            "release/v4.4",
            "release/v5.0",
            "release/v5.1",
            "release/v5.2",
            "master",
        ]
        result = get_used_idf_branches(branches)
        self.assertIn("release/v5.0", result)
        self.assertIn("release/v5.1", result)
        self.assertIn("release/v5.2", result)
        self.assertIn("master", result)
        self.assertNotIn("release/v4.4", result)

    @patch("build_wheels.MIN_IDF_MAJOR_VERSION", 5)
    @patch("build_wheels.MIN_IDF_MINOR_VERSION", 1)
    def test_filters_by_minor_version(self):
        """Test that filtering works correctly with minor version."""
        branches = [
            "release/v5.0",
            "release/v5.1",
            "release/v5.2",
        ]
        result = get_used_idf_branches(branches)
        self.assertNotIn("release/v5.0", result)
        self.assertIn("release/v5.1", result)
        self.assertIn("release/v5.2", result)

    def test_ignores_non_release_branches(self):
        """Test that non-release branches (except master) are ignored."""
        branches = [
            "feature/test",
            "bugfix/something",
            "release/v5.0",
        ]
        result = get_used_idf_branches(branches)
        self.assertNotIn("feature/test", result)
        self.assertNotIn("bugfix/something", result)
        self.assertIn("master", result)


class TestAddIntoRequirements(unittest.TestCase):
    """Test the _add_into_requirements function."""

    def test_parses_simple_requirements(self):
        """Test parsing simple requirement lines."""
        lines = ["numpy", "pandas>=1.0", "requests==2.28.0"]
        result = _add_into_requirements(lines)
        self.assertEqual(len(result), 3)
        names = {r.name for r in result}
        self.assertIn("numpy", names)
        self.assertIn("pandas", names)
        self.assertIn("requests", names)

    def test_ignores_comments(self):
        """Test that comment lines are ignored."""
        lines = [
            "# This is a comment",
            "numpy",
            "pandas  # inline comment",
        ]
        result = _add_into_requirements(lines)
        self.assertEqual(len(result), 2)

    def test_ignores_empty_lines(self):
        """Test that empty lines are ignored."""
        lines = ["numpy", "", "  ", "pandas"]
        result = _add_into_requirements(lines)
        self.assertEqual(len(result), 2)

    def test_handles_whitespace(self):
        """Test that leading/trailing whitespace is handled."""
        lines = ["  numpy  ", "\tpandas\t"]
        result = _add_into_requirements(lines)
        self.assertEqual(len(result), 2)


class TestResolvedPins(unittest.TestCase):
    """The pip report is the wheel list. Constraint options are not packages."""

    def test_report_becomes_exact_pins(self):
        report = {
            "install": [
                {"metadata": {"name": "cryptography", "version": "44.0.3"}},
                {"metadata": {"name": "cffi", "version": "1.17.1"}},
            ]
        }
        pins = pins_from_pip_report(report)
        self.assertEqual({str(pin) for pin in pins}, {"cryptography==44.0.3", "cffi==1.17.1"})

    def test_union_keeps_both_versions_of_one_package(self):
        older = pins_from_pip_report({"install": [{"metadata": {"name": "cffi", "version": "1.17.1"}}]})
        newer = pins_from_pip_report({"install": [{"metadata": {"name": "cffi", "version": "2.0.0"}}]})
        union = union_pin_sets([older, newer])
        self.assertEqual({str(pin) for pin in union}, {"cffi==1.17.1", "cffi==2.0.0"})

    def test_only_binary_is_not_a_pin(self):
        parsed = _add_into_requirements(["--only-binary cryptography"])
        self.assertEqual(parsed, set())
        self.assertEqual(pins_from_pip_report({"install": []}), set())

    def test_install_entry_without_version_fails(self):
        with self.assertRaises(ValueError):
            pins_from_pip_report({"install": [{"metadata": {"name": "cffi"}}]})

    def test_incomplete_report_names_the_branch(self):
        with self.assertRaises(BranchResolveError) as caught:
            pins_from_pip_report_for_branch(
                {"install": [{"metadata": {"version": "1.0"}}]},
                "release/v5.4",
            )
        self.assertEqual(caught.exception.branch, "release/v5.4")


class TestResolveBranches(unittest.TestCase):
    def test_idf6_not_targeted_on_python_38(self):
        with patch.object(sys, "version_info", MagicMock(major=3, minor=8, micro=18)):
            self.assertFalse(branch_targets_current_interpreter("release/v6.0"))
            self.assertFalse(branch_targets_current_interpreter("master"))
            self.assertTrue(branch_targets_current_interpreter("release/v5.5"))
            self.assertTrue(branch_targets_current_interpreter("esptool"))

    def test_idf6_resolves_on_python_310(self):
        with patch.object(sys, "version_info", MagicMock(major=3, minor=10, micro=0)):
            self.assertTrue(branch_targets_current_interpreter("release/v6.0"))

    def test_resolve_only_branches_for_this_interpreter(self):
        branches = [
            {"branch": "release/v5.5", "requirements": ["a"], "constraints": []},
            {"branch": "release/v6.0", "requirements": ["b"], "constraints": []},
        ]
        with patch.object(sys, "version_info", MagicMock(major=3, minor=8, micro=18)):
            with patch(
                "idf_branch_resolve._resolve_lines", return_value={Requirement("requests==2.32.0")}
            ) as mock_resolve:
                pins = resolve_branches(branches)
        mock_resolve.assert_called_once()
        self.assertEqual(mock_resolve.call_args[0][2], "release/v5.5")
        self.assertEqual({str(pin) for pin in pins}, {"requests==2.32.0"})

    def test_resolve_failure_aborts(self):
        with patch(
            "idf_branch_resolve._resolve_lines", side_effect=BranchResolveError("release/v5.5", "ResolutionImpossible")
        ):
            with self.assertRaises(SystemExit):
                resolve_branches([{"branch": "release/v5.5", "requirements": [], "constraints": []}])


class TestResolveTransient(unittest.TestCase):
    def test_resolution_impossible_is_not_transient(self):
        self.assertFalse(_resolve_transient_failure("ERROR: ResolutionImpossible: idf-component-manager"))

    def test_connection_reset_is_transient(self):
        self.assertTrue(_resolve_transient_failure("Connection reset by peer while fetching simple/cryptography/"))


class TestPipResolveFailureDetail(unittest.TestCase):
    def test_extracts_conflict_block_from_pip_output(self):
        sample = """
Collecting idf-component-manager>=2.2
ERROR: Cannot install idf-component-manager>=2.2 because these package versions have conflicting dependencies.

The conflict is caused by:
    The user requested idf-component-manager>=2.2
    idf-component-manager 2.2.0 depends on requests>=2.31
    esp-idf-pkg 0.1 depends on requests<2.20

To fix this you could try to:
1. loosen constraints
ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/topics/dependency-resolution/
"""
        detail = _pip_resolve_failure_detail(sample.encode(), b"")
        self.assertIn("idf-component-manager>=2.2", detail)
        self.assertIn("The conflict is caused by:", detail)
        self.assertIn("requests<2.20", detail)
        self.assertIn("ResolutionImpossible", detail)

    def test_non_conflict_still_returns_tail(self):
        detail = _pip_resolve_failure_detail(b"", b"ERROR: No matching distribution found for foo")
        self.assertIn("No matching distribution", detail)

    def test_keeps_conflict_explanation_that_precedes_the_error(self):
        stdout = b"The conflict is caused by:\n    The user requested gdbgui==0.13.2.0\n"
        stderr = b"ERROR: ResolutionImpossible: for help visit https://pip.pypa.io\n"
        detail = _pip_resolve_failure_detail(stdout, stderr)
        self.assertIn("The conflict is caused by:", detail)
        self.assertIn("gdbgui==0.13.2.0", detail)
        self.assertIn("ResolutionImpossible", detail)


def _marker_env(python_version: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    for key, value in default_environment().items():
        env[str(key)] = str(value)
    env["python_version"] = python_version
    return env


class TestExcludedConstraintPins(unittest.TestCase):
    def test_exact_constraint_pin_is_skipped_and_other_caps_remain(self):
        rules = ExcludeRequirements({Requirement("gdbgui==0.13.2.0")})
        requirements = _requirements_for_resolution(
            ["gdbgui", "requests"],
            "release/v5.5",
            rules,
            ["gdbgui==0.13.2.0", "requests<3"],
        )
        constraints = _constraints_without_excluded_pins(
            ["gdbgui==0.13.2.0", "requests<3"],
            "release/v5.5",
            rules,
        )
        self.assertEqual(requirements, ["requests"])
        self.assertEqual(constraints, ["requests<3"])


class TestRangeExclusionAlternatives(unittest.TestCase):
    def test_range_complement_is_solved_as_alternatives(self):
        rules = ExcludeRequirements({Requirement("pkg>=1,<2")})
        seen = []

        def fake(_requirements, constraints, _branch):
            seen.append(list(constraints))
            if any(item.startswith("pkg<") for item in constraints):
                return {Requirement("pkg==0.5")}
            return {Requirement("pkg==3")}

        with patch("idf_branch_resolve._resolve_lines", side_effect=fake):
            pins = resolve_branches(
                [{"branch": "release/v5.5", "requirements": ["pkg"], "constraints": []}],
                rules,
            )
        self.assertEqual(len(seen), 2)
        self.assertTrue(any("pkg<1" in group for group in seen))
        self.assertTrue(any("pkg>=2" in group for group in seen))
        self.assertFalse(any("pkg<1" in group and "pkg>=2" in group for group in seen))
        self.assertEqual({str(pin) for pin in pins}, {"pkg==0.5", "pkg==3"})

    def test_single_bound_stays_one_constraint(self):
        rules = ExcludeRequirements({Requirement("gdbgui==0.13.2.0"), Requirement("pkg>=1,<2")})
        groups = exclusion_constraint_alternatives("linux_x86_64", "3.12", rules)
        self.assertEqual(len(groups), 2)
        for group in groups:
            self.assertIn("gdbgui!=0.13.2.0", group)
            self.assertEqual(len(group), 2)


class TestRequirementsForOtherInterpreters(unittest.TestCase):
    def test_solved_branch_forwards_only_markers_that_do_not_match(self):
        branches = [
            {
                "branch": "release/v5.5",
                "requirements": [
                    "solved==1",
                    'later>=1; python_version >= "99"',
                    'here>=1; python_version >= "3.0"',
                ],
            },
            {
                "branch": "release/v6.0",
                "requirements": ["idf6-only==1", 'also>=1; python_version >= "3.0"'],
            },
        ]
        with patch(
            "build_wheels.branch_targets_current_interpreter",
            side_effect=lambda branch, idf_major=None: branch != "release/v6.0",
        ):
            lines = requirements_for_other_interpreters(branches)
        self.assertIn('later>=1; python_version >= "99"', lines)
        self.assertNotIn("solved==1", lines)
        self.assertFalse(any(line.startswith("here>=") for line in lines))
        self.assertIn("idf6-only==1", lines)
        self.assertTrue(any(line.startswith("also>=") for line in lines))

    def test_forwards_marker_bearing_constraints(self):
        branches = [
            {
                "branch": "release/v5.5",
                "requirements": ["pkg"],
                "constraints": ['pkg<=2; python_version >= "99"', "other<=4"],
            }
        ]
        with patch("build_wheels.branch_targets_current_interpreter", return_value=True):
            lines = requirements_for_other_interpreters(branches)
        self.assertIn('pkg<=2; python_version >= "99"', lines)
        self.assertNotIn("other<=4", lines)

    def test_does_not_forward_wholly_excluded_packages(self):
        branches = [
            {
                "branch": "release/v6.0",
                "idf_major": 6,
                "requirements": ["esptool", "dbus-python", "requests"],
                "constraints": ['esptool<=6; python_version >= "99"'],
            }
        ]
        rules = ExcludeRequirements(
            {
                Requirement("esptool"),
                Requirement('dbus-python; python_version == "3.8"'),
            }
        )
        with patch("build_wheels.branch_targets_current_interpreter", return_value=False):
            lines = requirements_for_other_interpreters(branches, rules)
        self.assertNotIn("esptool", lines)
        self.assertFalse(any(line.startswith("esptool") for line in lines))
        self.assertIn("dbus-python", lines)
        self.assertIn("requests", lines)

    def test_inactive_requirement_marker_is_not_removed_by_exclusion(self):
        rules = ExcludeRequirements({Requirement("pkg")})
        with patch("idf_branch_resolve.get_current_platform", return_value="linux_x86_64"), patch(
            "idf_branch_resolve._current_python", return_value="3.13"
        ):
            result = _requirements_for_resolution(
                ['pkg==1; sys_platform == "win32"'],
                "release/v5.5",
                rules,
                [],
            )
        self.assertEqual(result, ['pkg==1; sys_platform == "win32"'])

    def test_subset_resolver_passes_branch_constraints(self):
        captured = []

        def fake_resolve(requirements, constraints, branch):
            captured.append((requirements, constraints, branch))
            return {Requirement("pkg==2"), Requirement("transitive==1")}

        branches = [
            {
                "branch": "release/v6.0",
                "idf_major": 6,
                "requirements": ["pkg"],
                "constraints": ["pkg<=2"],
            },
            {
                "branch": "esptool",
                "requirements": ["esptool-dep"],
                "constraints": [],
            },
        ]
        with patch("idf_branch_resolve.branch_targets_current_interpreter", return_value=True), patch(
            "idf_branch_resolve._resolve_lines", side_effect=fake_resolve
        ):
            result = resolve_branch_subset(branches, ["pkg"], ExcludeRequirements(set()))
        self.assertEqual(result, {Requirement("pkg==2")})
        self.assertEqual(captured[0][1], ["pkg<=2"])
        self.assertEqual([item[2] for item in captured], ["release/v6.0"])


class TestDeferredPinPolicies(unittest.TestCase):
    def test_pin_gets_newer_python_constraint_marker(self):
        pins = {Requirement("pydantic-core==2.27.0")}
        branches = [
            {
                "branch": "master",
                "constraints": ['pydantic-core<=2.35; python_version < "3.14"'],
            }
        ]
        marked = _pin_markers_for_other_interpreters(pins, branches)
        self.assertEqual(
            {str(pin) for pin in marked},
            {'pydantic-core==2.27.0; python_version < "3.14"'},
        )

    def test_sdist_constraints_include_packages_not_in_requirements(self):
        branches = [
            {
                "branch": "release/v6.0",
                "constraints": ["constraint-only-package<=1.0", "esptool<=6"],
            }
        ]
        result = constraint_requirements_for_sdist(branches, ExcludeRequirements({Requirement("esptool")}))
        self.assertEqual({str(requirement) for requirement in result}, {"constraint-only-package<=1.0"})


class TestRetainExactPins(unittest.TestCase):
    """Exclude filtering must not turn a solved pin into an open range."""

    def _greenlet_exclude(self) -> set:
        return {
            Requirement('greenlet>3.0.0; python_version >= "3.13"'),
            Requirement('greenlet; python_version < "3.13"'),
        }

    def test_python_313_drops_the_excluded_pin_and_keeps_the_exact_newer_one(self):
        pins = {Requirement("greenlet==3.1.0"), Requirement("greenlet==3.0.0")}
        kept = retain_exact_pins(pins, self._greenlet_exclude(), print_requirements=False, env=_marker_env("3.13"))
        self.assertEqual({str(pin) for pin in kept}, {"greenlet==3.1.0"})

    def test_older_python_keeps_both_exact_pins(self):
        pins = {Requirement("greenlet==3.1.0"), Requirement("greenlet==3.0.0")}
        kept = retain_exact_pins(pins, self._greenlet_exclude(), print_requirements=False, env=_marker_env("3.11"))
        self.assertEqual({str(pin) for pin in kept}, {"greenlet==3.0.0", "greenlet==3.1.0"})

    def test_unversioned_exclude_drops_the_pin(self):
        kept = retain_exact_pins(
            {Requirement("gdbgui==0.13.2.0")},
            {Requirement("gdbgui")},
            print_requirements=False,
        )
        self.assertEqual(kept, set())

    def test_production_exclude_rules_keep_platform_scope(self):
        from yaml_list_adapter import load_exclude_rules

        rules = load_exclude_rules("exclude_list.yaml")
        windows = retain_exact_pins(
            {Requirement("pygobject==3.48.0")},
            rules,
            print_requirements=False,
            env={"sys_platform": "win32", "python_version": "3.13"},
        )
        x86 = retain_exact_pins(
            {Requirement("cryptography==42.0.8")},
            rules,
            print_requirements=False,
            env={"sys_platform": "linux_x86_64", "python_version": "3.8"},
        )
        self.assertEqual(windows, set())
        self.assertEqual({str(pin) for pin in x86}, {"cryptography==42.0.8"})


class TestStripPipOptionsInBranchResolve(unittest.TestCase):
    """Dry-run solves must keep UPPER BOUNDS caps but drop ``--only-binary`` flags."""

    def test_resolve_lines_omits_only_binary_from_written_files(self):
        captured = {}

        def fake_run(cmd, stdout=None, stderr=None, **_kwargs):
            req_idx = cmd.index("-r") + 1
            cons_idx = cmd.index("-c") + 1
            report_idx = cmd.index("--report") + 1
            captured["req"] = Path(cmd[req_idx]).read_text(encoding="utf-8")
            captured["cons"] = Path(cmd[cons_idx]).read_text(encoding="utf-8")
            Path(cmd[report_idx]).write_text(
                '{"install": [{"metadata": {"name": "cryptography", "version": "42.0.8"}}, '
                '{"metadata": {"name": "gdbgui", "version": "0.13.2.0"}}]}',
                encoding="utf-8",
            )
            return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

        with patch("idf_branch_resolve.subprocess.run", side_effect=fake_run):
            pins = _resolve_lines(
                ["esp-idf-nvs-partition-gen", "--only-binary cryptography"],
                ["cryptography>=2.1.4,<43", "--only-binary cryptography", 'pillow <=10.4.0; python_version < "3.9"'],
                "release/v5.3",
            )

        self.assertNotIn("--only-binary", captured["req"])
        self.assertNotIn("--only-binary", captured["cons"])
        self.assertIn("cryptography<43,>=2.1.4", captured["cons"])
        self.assertIn('pillow<=10.4.0; python_version < "3.9"', captured["cons"])
        # Post-resolution exclusions drop gdbgui==0.13.2.0 and ARMv7 cryptography without pre-solve conflict
        win_excludes = YAMLListAdapter("exclude_list.yaml", exclude=True, current_platform="windows").requirements
        kept_win = retain_exact_pins(
            pins,
            win_excludes,
            print_requirements=False,
            env={"python_version": "3.8", "sys_platform": "win32"},
        )
        self.assertEqual({str(p) for p in kept_win}, {"cryptography==42.0.8"})

        armv7_excludes = YAMLListAdapter("exclude_list.yaml", exclude=True, current_platform="linux_armv7").requirements
        kept_armv7 = retain_exact_pins(
            pins,
            armv7_excludes,
            print_requirements=False,
            env={"python_version": "3.8", "sys_platform": "linux"},
        )
        self.assertEqual(kept_armv7, set())


class TestConstraintDownloadAndCollect(unittest.TestCase):
    def test_release_constraint_404_aborts(self):
        response = type("Response", (), {"status_code": 404, "text": "missing", "content": b"missing"})()
        with patch("build_wheels._requests_get", return_value=response):
            with self.assertRaises(SystemExit):
                _download_branch_constraints("https://example.invalid/c.txt", "release/v5.4", "v5.4")

    def test_master_constraint_404_resolves_without_constraints(self):
        response = type("Response", (), {"status_code": 404, "text": "missing", "content": b"missing"})()
        with patch("build_wheels._requests_get", return_value=response):
            lines = _download_branch_constraints("https://example.invalid/c.txt", "master", "None")
        self.assertEqual(lines, [])

    def test_esptool_is_one_group_and_caps_are_not_packages(self):
        body = b'{"features": [{"name": "core", "requirement_path": "tools/requirements/requirements.core.txt"}]}'
        response = type("Response", (), {"status_code": 200, "text": body.decode(), "content": body})()
        with patch("build_wheels._requests_get", return_value=response), patch(
            "build_wheels._download_branch_requirements", return_value=["esp-idf-req>=1"]
        ), patch(
            "build_wheels._download_branch_constraints",
            return_value=["cffi<2.0", "--only-binary cryptography"],
        ), patch("build_wheels._download_esptool_requirements", return_value=["esptool-dep>=1"]):
            requirements, branches = collect_idf_inputs(["release/v5.4", "master"], ["v5.4", "v5.5"], False)
        self.assertEqual([item["branch"] for item in branches], ["release/v5.4", "master", "esptool"])
        self.assertEqual(branches[0]["requirements"], ["esp-idf-req>=1"])
        self.assertEqual(branches[0]["constraints"], ["cffi<2.0", "--only-binary cryptography"])
        self.assertNotIn("esptool-dep>=1", branches[0]["requirements"])
        self.assertEqual(
            branches[2],
            {"branch": "esptool", "idf_major": None, "requirements": ["esptool-dep>=1"], "constraints": []},
        )
        names = {req.name for req in requirements}
        self.assertIn("esp-idf-req", names)
        self.assertIn("esptool-dep", names)
        self.assertNotIn("cffi", names)


class TestPythonDependentWheels(unittest.TestCase):
    """Other Python versions rebuild cp wheels, not python_version markers."""

    def test_python_version_marker_is_not_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "pillow-11.3.0-cp39-cp39-manylinux_2_28_x86_64.whl").write_bytes(b"")
            (links / "idna-3.10-py3-none-any.whl").write_bytes(b"")
            requirements = {Requirement('pillow<=13; python_version >= "3.11"')}
            result = get_python_dependent_wheels(tmp, requirements)
            self.assertEqual({str(pin) for pin in result}, {"pillow==11.3.0"})
            self.assertTrue(all(pin.marker is None for pin in result))

    def test_stale_wheel_outside_the_pin_set_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "pillow-11.3.0-cp39-cp39-manylinux_2_28_x86_64.whl").write_bytes(b"")
            (links / "pillow-9.5.0-cp39-cp39-manylinux_2_28_x86_64.whl").write_bytes(b"")
            result = get_python_dependent_wheels(tmp, {Requirement("pillow==11.3.0")})
            self.assertEqual({str(pin) for pin in result}, {"pillow==11.3.0"})


class TestMergeRequirements(unittest.TestCase):
    """Test the merge_requirements function."""

    def test_merge_specifiers(self):
        """Test merging two requirements with version specifiers."""
        req1 = Requirement("numpy>=1.0")
        req2 = Requirement("numpy<2.0")
        result = merge_requirements(req1, req2)
        self.assertEqual(result.name, "numpy")
        self.assertIn(">=1.0", str(result.specifier))
        self.assertIn("<2.0", str(result.specifier))

    def test_merge_markers(self):
        """Test merging two requirements with markers."""
        req1 = Requirement("numpy; sys_platform == 'win32'")
        req2 = Requirement("numpy; python_version >= '3.8'")
        result = merge_requirements(req1, req2)
        self.assertEqual(result.name, "numpy")
        self.assertIn("sys_platform", str(result.marker))
        self.assertIn("python_version", str(result.marker))

    def test_merge_preserves_name(self):
        """Test that package name is preserved after merge."""
        req1 = Requirement("requests>=2.0")
        req2 = Requirement("requests; sys_platform == 'linux'")
        result = merge_requirements(req1, req2)
        self.assertEqual(result.name, "requests")


class TestGetNoBinaryArgs(unittest.TestCase):
    """Test the get_no_binary_args function."""

    @patch("_helper_functions.platform.system", return_value="Linux")
    def test_returns_args_for_source_build_packages_on_linux(self, mock_system):
        """Test that --no-binary args are returned for specified packages on Linux."""
        result = get_no_binary_args("cffi")
        self.assertEqual(result, ["--no-binary", "cffi"])

    @patch("_helper_functions.platform.system", return_value="Linux")
    def test_handles_requirement_with_version(self, mock_system):
        """Test that package name is extracted from requirement string."""
        result = get_no_binary_args("cffi>=1.0")
        self.assertEqual(result, ["--no-binary", "cffi"])

    @patch("_helper_functions.platform.system", return_value="Windows")
    def test_returns_empty_on_windows(self, mock_system):
        """Test that empty list is returned on Windows."""
        result = get_no_binary_args("cffi")
        self.assertEqual(result, [])

    @patch("_helper_functions.platform.system", return_value="Darwin")
    def test_returns_empty_on_macos(self, mock_system):
        """Test that empty list is returned on macOS."""
        result = get_no_binary_args("cffi")
        self.assertEqual(result, [])

    @patch("_helper_functions.platform.system", return_value="Linux")
    def test_returns_empty_for_non_source_build_package(self, mock_system):
        """Test that empty list is returned for packages not in source build list."""
        result = get_no_binary_args("requests")
        self.assertEqual(result, [])

    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    @patch("_helper_functions.platform.system", return_value="Linux")
    def test_returns_no_binary_for_armv7_native_guard_packages(self, mock_system, mock_armv7):
        """ARMv7 uses piwheels except native stacks that must be built in-lineage."""
        self.assertEqual(get_no_binary_args("cffi"), ["--no-binary", "cffi"])
        self.assertEqual(get_no_binary_args("cryptography"), [])
        self.assertEqual(get_no_binary_args("argon2-cffi-bindings"), ["--no-binary", "argon2-cffi-bindings"])
        self.assertEqual(get_no_binary_args("requests"), [])


class TestForceInterpreterBinarySkip(unittest.TestCase):
    """--force-interpreter-binary skip policy for dependent wheel builds."""

    def test_skip_armv7_cffi_backed_packages(self):
        for name in (
            "argon2-cffi-bindings",
            "pynacl",
            "tibs",
            "rpds-py",
            "cryptography",
            "psutil",
            "pycryptodome",
            "tree-sitter-c",
        ):
            self.assertTrue(force_interpreter_skip_package(canonicalize_name(name)))

    def test_requests_not_in_skip_set(self):
        self.assertFalse(force_interpreter_skip_package(canonicalize_name("requests")))

    def test_find_links_superseded_obsolete_cryptography_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-48.0.0-cp38-abi3-linux_armv7l.whl").write_bytes(b"")
            req = Requirement("cryptography<43,>=2.1.4")
            skip, reason = find_links_wheel_build_skip(req, links)
            self.assertTrue(skip)
            self.assertIn("48.0.0", reason)

    def test_find_links_skip_when_matching_wheel_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-42.0.8-cp37-abi3-manylinux_2_28_x86_64.whl").write_bytes(b"")
            req = Requirement("cryptography<45,>=2.1.4")
            skip, reason = find_links_wheel_build_skip(req, links)
            self.assertTrue(skip)
            self.assertIn("already has", reason)
            self.assertIn("42.0.8", reason)

    @patch("_helper_functions.sys.version_info", MagicMock(major=3, minor=8))
    def test_find_links_uses_pep425_cpython_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "pkg-1.0-cp38-cp38-manylinux_2_28_x86_64.whl").write_bytes(b"")
            (links / "pkg-1.1-cp39-abi3-manylinux_2_28_x86_64.whl").write_bytes(b"")
            skip_exact, _ = find_links_wheel_build_skip(Requirement("pkg==1.0"), links)
            skip_abi3, _ = find_links_wheel_build_skip(Requirement("pkg==1.1"), links)
            self.assertTrue(skip_exact)
            self.assertFalse(skip_abi3)

    def test_find_links_no_skip_without_wheels_in_find_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            req = Requirement("cryptography<49,>=2.1.4")
            skip, _ = find_links_wheel_build_skip(req, links)
            self.assertFalse(skip)

    def test_find_links_no_skip_when_newer_wheel_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-47.0.0-cp311-abi3-win_amd64.whl").write_bytes(b"")
            req = Requirement("cryptography>=49.0.0")
            skip, reason = find_links_wheel_build_skip(req, links)
            self.assertFalse(skip)
            self.assertEqual(reason, "")

    def test_find_links_keeps_older_exact_pin(self):
        """Another release's exact pin is still built when a newer wheel is present."""
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cffi-2.1.0-cp314-cp314-manylinux2014_x86_64.whl").write_bytes(b"")
            req = Requirement("cffi==1.17.1")
            skip, reason = find_links_wheel_build_skip(req, links)
            self.assertFalse(skip)
            self.assertEqual(reason, "")

    @patch("_helper_functions.platform.system", return_value="Linux")
    def test_skip_bounded_pin_without_find_links_wheel(self, _mock_system):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            req = Requirement("cryptography<46.1,>=2.1.4")
            skip, reason = bounded_pin_without_find_links_skip(req, links)
            self.assertTrue(skip)
            self.assertIn("bounded pin", reason)

    @patch("_helper_functions.platform.system", return_value="Linux")
    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_skip_bounded_pin_without_find_links_wheel(self, _mock_armv7, _mock_system):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            req = Requirement("cryptography<43,>=2.1.4")
            skip, reason = bounded_pin_without_find_links_skip(req, links)
            self.assertTrue(skip)
            self.assertIn("bounded pin", reason)

    @patch("_helper_functions.platform.system", return_value="Linux")
    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_no_skip_exact_pin_without_find_links_wheel(self, _mock_armv7, _mock_system):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            req = Requirement("cryptography==47.0.0")
            skip, _ = bounded_pin_without_find_links_skip(req, links)
            self.assertFalse(skip)

    @patch("_helper_functions.platform.system", return_value="Linux")
    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_no_skip_bounded_pin_when_find_links_has_wheel(self, _mock_armv7, _mock_system):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-49.0.0-cp313-abi3-linux_armv7l.whl").write_bytes(b"")
            req = Requirement("cryptography<43,>=2.1.4")
            skip, _ = bounded_pin_without_find_links_skip(req, links)
            self.assertFalse(skip)

    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_rebuild_instead_of_find_links_skip_matching_wheel(self, _mock_armv7):
        reason = "find-links already has cffi 2.0.0 matching >=1.15"
        self.assertTrue(
            armv7_rebuild_instead_of_find_links_skip("cffi", reason),
        )

    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_no_rebuild_for_cryptographypiwheels(self, _mock_armv7):
        reason = "find-links already has cryptography 49.0.0 matching >=2.1.4"
        self.assertFalse(armv7_rebuild_instead_of_find_links_skip("cryptography", reason))

    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_no_rebuild_for_superseded_pin_skip(self, _mock_armv7):
        reason = "find-links has cryptography up to 49.0.0 but none match <43"
        self.assertFalse(armv7_rebuild_instead_of_find_links_skip("cryptography", reason))

    def test_remove_find_links_wheels_for_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cffi-2.0.0-cp313-cp313-linux_armv7l.whl").write_bytes(b"")
            (links / "requests-2.31.0-py3-none-any.whl").write_bytes(b"")
            removed = remove_find_links_wheels_for_package("cffi", links)
            self.assertEqual(removed, 1)
            self.assertFalse((links / "cffi-2.0.0-cp313-cp313-linux_armv7l.whl").exists())
            self.assertTrue((links / "requests-2.31.0-py3-none-any.whl").exists())

    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_pip_wheel_env_extends_no_binary_for_cffi(self, _mock_armv7):
        env = armv7_pip_wheel_subprocess_env("cffi>=1.15")
        self.assertEqual(env["PIP_NO_BINARY"], "cffi,argon2-cffi-bindings")

    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_pip_wheel_env_leaves_global_for_other_packages(self, _mock_armv7):
        with patch.dict(os.environ, {"PIP_NO_BINARY": "cffi,argon2-cffi-bindings"}, clear=False):
            env = armv7_pip_wheel_subprocess_env("requests>=2.28")
            self.assertEqual(env["PIP_NO_BINARY"], "cffi,argon2-cffi-bindings")


class TestRunPipWheelPreferBinary(unittest.TestCase):
    def test_without_only_binary_all_strips_flag(self) -> None:
        stripped = without_only_binary_all(["pip", "wheel", "pkg", "--only-binary", ":all:", "--no-deps"])
        self.assertEqual(stripped, ["pip", "wheel", "pkg", "--no-deps"])
        self.assertIsNone(without_only_binary_all(["pip", "wheel", "pkg"]))

    @patch("wheel_compat_policy.pip_wheel_invocation_args", return_value=["--only-binary", ":all:"])
    @patch("wheel_compat_policy.subprocess.run")
    def test_retries_without_only_binary_when_first_pip_fails(self, mock_run, _args) -> None:
        fail = type("R", (), {"returncode": 1, "stdout": b"", "stderr": b"no binary"})()
        ok = type("R", (), {"returncode": 0, "stdout": b"sdist ok", "stderr": b""})()
        mock_run.side_effect = [fail, ok]
        out = run_pip_wheel("sphinx_selective_exclude==1.0.3")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(mock_run.call_count, 2)
        first_cmd = mock_run.call_args_list[0][0][0]
        retry_cmd = mock_run.call_args_list[1][0][0]
        self.assertIn("--only-binary", first_cmd)
        self.assertNotIn("--only-binary", retry_cmd)

    @patch("wheel_compat_policy.pip_wheel_invocation_args", return_value=["--find-links", "."])
    @patch("wheel_compat_policy.subprocess.run")
    def test_no_retry_when_only_binary_absent(self, mock_run, _args) -> None:
        fail = type("R", (), {"returncode": 1, "stdout": b"", "stderr": b"fail"})()
        mock_run.return_value = fail
        out = run_pip_wheel("cffi")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(mock_run.call_count, 1)


class TestPipWheelOrMirrorSuccess(unittest.TestCase):
    @patch("wheel_compat_policy.mirror_pypi_manylinux228_wheel", return_value=True)
    def test_pip_success_still_mirrors_and_returns_true(self, mock_mirror):
        self.assertTrue(pip_wheel_or_mirror_success("greenlet>3.0.0", 0))
        mock_mirror.assert_called_once()

    @patch("wheel_compat_policy.mirror_pypi_manylinux228_wheel", return_value=True)
    def test_pip_failure_succeeds_when_mirror_works(self, mock_mirror):
        self.assertTrue(pip_wheel_or_mirror_success("pillow!=9.5.0", 1))
        mock_mirror.assert_called_once()

    @patch("wheel_compat_policy.mirror_pypi_manylinux228_wheel", return_value=False)
    def test_pip_failure_fails_when_mirror_fails(self, mock_mirror):
        self.assertFalse(pip_wheel_or_mirror_success("cffi==1.17.1", 1))
        mock_mirror.assert_called_once()


class TestMirrorPypiManylinux228(unittest.TestCase):
    @patch("subprocess.run")
    @patch("platform.machine", return_value="x86_64")
    @patch("platform.system", return_value="Linux")
    def test_runs_pip_download_for_force_no_binary_package(self, _sys, _machine, mock_run):
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        self.assertTrue(mirror_pypi_manylinux228_wheel("cryptography==49.0.0"))
        cmd = mock_run.call_args[0][0]
        self.assertIn("download", cmd)
        self.assertIn("manylinux_2_28_x86_64", cmd)

    @patch("subprocess.run")
    @patch("platform.machine", return_value="aarch64")
    @patch("platform.system", return_value="Linux")
    def test_uses_aarch64_platform(self, _sys, _machine, mock_run):
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        self.assertTrue(mirror_pypi_manylinux228_wheel("cffi>=1.15.0"))
        cmd = mock_run.call_args[0][0]
        self.assertIn("manylinux_2_28_aarch64", cmd)

    @patch("subprocess.run")
    @patch("platform.system", return_value="Linux")
    @patch("platform.machine", return_value="x86_64")
    def test_skips_packages_outside_force_no_binary_list(self, _machine, _sys, mock_run):
        self.assertFalse(mirror_pypi_manylinux228_wheel("requests==2.31.0"))
        mock_run.assert_not_called()

    @patch("subprocess.run")
    @patch("platform.machine", return_value="x86_64")
    @patch("platform.system", return_value="Linux")
    def test_prunes_ci_manylinux_234_after_successful_mirror(self, _sys, _machine, mock_run):
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-49.0.0-cp312-abi3-manylinux_2_34_x86_64.whl").write_bytes(b"")
            (links / "cryptography-49.0.0-cp312-abi3-manylinux_2_28_x86_64.whl").write_bytes(b"")
            self.assertTrue(mirror_pypi_manylinux228_wheel("cryptography==49.0.0", wheel_dir=links))
            self.assertFalse((links / "cryptography-49.0.0-cp312-abi3-manylinux_2_34_x86_64.whl").exists())
            self.assertTrue((links / "cryptography-49.0.0-cp312-abi3-manylinux_2_28_x86_64.whl").exists())

    @patch("subprocess.run")
    @patch("platform.machine", return_value="aarch64")
    @patch("platform.system", return_value="Linux")
    def test_strips_pep508_markers_for_pip_download(self, _sys, _machine, mock_run):
        mock_run.return_value = type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        line = 'greenlet==3.0.0; python_version < "3.13"'
        self.assertTrue(mirror_pypi_manylinux228_wheel(line))
        cmd = mock_run.call_args[0][0]
        marker_idx = cmd.index(line) if line in cmd else -1
        self.assertEqual(marker_idx, -1)
        self.assertIn("greenlet==3.0.0", cmd)
        self.assertNotIn("python_version", cmd)


class TestPruneCiManylinux228(unittest.TestCase):
    def test_removes_only_newer_manylinux_for_same_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep = links / "cryptography-49.0.0-cp312-abi3-manylinux_2_28_x86_64.whl"
            drop = links / "cryptography-49.0.0-cp312-abi3-manylinux_2_34_x86_64.whl"
            other = links / "cryptography-48.0.0-cp312-abi3-manylinux_2_34_x86_64.whl"
            keep.write_bytes(b"")
            drop.write_bytes(b"")
            other.write_bytes(b"")
            removed = prune_ci_manylinux_newer_than_228("cryptography", "49.0.0", wheel_dir=links)
            self.assertEqual(removed, 1)
            self.assertTrue(keep.exists())
            self.assertFalse(drop.exists())
            self.assertTrue(other.exists())

    def test_detects_compound_pypi_mirror_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            compound = links / "cryptography-49.0.0-cp311-abi3-manylinux_2_28_x86_64.manylinux_2_34_x86_64.whl"
            compound.write_bytes(b"")
            self.assertEqual(_mirrored_manylinux228_version("cryptography", links), "49.0.0")

    def test_prune_when_228_mirror_present_drops_newer_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            compound = links / "cryptography-49.0.0-cp311-abi3-manylinux_2_28_x86_64.manylinux_2_34_x86_64.whl"
            ci_only = links / "cryptography-49.0.0-cp312-abi3-manylinux_2_34_x86_64.whl"
            compound.write_bytes(b"")
            ci_only.write_bytes(b"")
            removed = prune_ci_manylinux_newer_than_228_when_228_mirror_present(wheel_dir=links)
            self.assertEqual(removed, 1)
            self.assertTrue(compound.exists())
            self.assertFalse(ci_only.exists())

    def test_prune_when_228_mirror_keeps_other_interpreter_exclusive_abi(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep_228 = links / "cffi-1.17.1-cp311-cp311-manylinux_2_28_x86_64.whl"
            keep_234 = links / "cffi-1.17.1-cp312-cp312-manylinux_2_34_x86_64.whl"
            keep_228.write_bytes(b"")
            keep_234.write_bytes(b"")
            removed = prune_ci_manylinux_newer_than_228_when_228_mirror_present(wheel_dir=links)
            self.assertEqual(removed, 0)
            self.assertTrue(keep_228.exists())
            self.assertTrue(keep_234.exists())

    def test_prune_named_version_keeps_other_interpreter_exclusive_abi(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            keep_228 = links / "cffi-1.17.1-cp311-cp311-manylinux_2_28_x86_64.whl"
            keep_234 = links / "cffi-1.17.1-cp312-cp312-manylinux_2_34_x86_64.whl"
            keep_228.write_bytes(b"")
            keep_234.write_bytes(b"")
            removed = prune_ci_manylinux_newer_than_228("cffi", "1.17.1", wheel_dir=links)
            self.assertEqual(removed, 0)
            self.assertTrue(keep_228.exists())
            self.assertTrue(keep_234.exists())


class TestSkipAuditwheelForPypiMirror(unittest.TestCase):
    @patch("platform.machine", return_value="x86_64")
    def test_skips_cryptography_manylinux_228_mirror(self, _machine):
        name = "cryptography-49.0.0-cp311-abi3-manylinux_2_28_x86_64.whl"
        self.assertTrue(should_skip_linux_auditwheel_for_pypi_mirror(name))

    @patch("platform.machine", return_value="x86_64")
    def test_does_not_skip_ci_manylinux_234_only(self, _machine):
        name = "cryptography-49.0.0-cp312-abi3-manylinux_2_34_x86_64.whl"
        self.assertFalse(should_skip_linux_auditwheel_for_pypi_mirror(name))

    @patch("platform.machine", return_value="x86_64")
    def test_skips_tree_sitter_c_manylinux_228_wheel(self, _machine):
        name = "tree_sitter_c-0.24.2-cp310-abi3-manylinux1_x86_64.manylinux_2_28_x86_64.manylinux_2_5_x86_64.whl"
        self.assertTrue(should_skip_linux_auditwheel_for_pypi_mirror(name))


class TestCryptographyMacosIntelBuild(unittest.TestCase):
    """macOS Intel builds cryptography from sdist with OpenSSL 4 (no PyPI x86_64 wheel for 49+)."""

    def setUp(self):
        self._saved_platform = get_current_platform
        import _helper_functions as hf

        hf.get_current_platform = lambda: "macos_x86_64"

    def tearDown(self):
        import _helper_functions as hf

        hf.get_current_platform = self._saved_platform

    def test_no_binary_for_cryptography_on_intel(self):
        args = get_cryptography_macos_intel_pip_wheel_args("cryptography>=49")
        self.assertEqual(args, ["--no-binary", "cryptography"])
        self.assertEqual(get_cryptography_macos_intel_pip_wheel_args("cryptography"), ["--no-binary", "cryptography"])
        self.assertEqual(
            get_cryptography_macos_intel_pip_wheel_args("cryptography>=50"), ["--no-binary", "cryptography"]
        )

    def test_no_binary_skipped_for_pre_49_cryptography_pins(self):
        self.assertEqual(get_cryptography_macos_intel_pip_wheel_args("cryptography<43,>=2.1.4"), [])
        self.assertEqual(get_cryptography_macos_intel_pip_wheel_args("cryptography<45,>=2.1.4"), [])
        self.assertEqual(get_cryptography_macos_intel_pip_wheel_args("cryptography<49,>=2.1.4"), [])

    def test_no_binary_only_for_cryptography(self):
        self.assertEqual(get_cryptography_macos_intel_pip_wheel_args("requests"), [])

    @patch("_helper_functions.get_current_platform", return_value="linux_x86_64")
    def test_noop_off_macos_intel(self, _mock_plat):
        self.assertEqual(get_cryptography_macos_intel_pip_wheel_args("cryptography"), [])

    @patch("_helper_functions.matching_release_version_strings", return_value=["50.0.1"])
    @patch("_helper_functions.get_current_platform", return_value="macos_x86_64")
    def test_rebuild_when_find_links_intel_wheel_is_older(self, _mock_plat, _mock_pypi):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-49.0.0-cp313-abi3-macosx_10_12_x86_64.whl").write_bytes(b"")
            req = Requirement("cryptography>=2.1.4")
            self.assertTrue(
                macos_intel_cryptography_rebuild_instead_of_find_links_skip(
                    req,
                    "find-links already has cryptography 49.0.0 matching >=2.1.4",
                    links,
                )
            )

    @patch("_helper_functions.matching_release_version_strings", return_value=["50.0.1"])
    @patch("_helper_functions.get_current_platform", return_value="macos_x86_64")
    def test_skip_when_find_links_intel_wheel_is_latest(self, _mock_plat, _mock_pypi):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-50.0.1-cp313-abi3-macosx_10_12_x86_64.whl").write_bytes(b"")
            req = Requirement("cryptography>=2.1.4")
            self.assertFalse(
                macos_intel_cryptography_rebuild_instead_of_find_links_skip(
                    req,
                    "find-links already has cryptography 50.0.1 matching >=2.1.4",
                    links,
                )
            )

    @patch("_helper_functions.matching_release_version_strings", return_value=["50.0.1"])
    @patch("_helper_functions.get_current_platform", return_value="macos_x86_64")
    def test_rebuild_when_find_links_only_has_other_platform_wheel(self, _mock_plat, _mock_pypi):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-50.0.1-cp311-abi3-manylinux_2_34_x86_64.whl").write_bytes(b"")
            req = Requirement("cryptography>=2.1.4")
            self.assertTrue(
                macos_intel_cryptography_rebuild_instead_of_find_links_skip(
                    req,
                    "find-links already has cryptography 50.0.1 matching >=2.1.4",
                    links,
                )
            )

    @patch("_helper_functions.get_current_platform", return_value="macos_x86_64")
    def test_still_skip_obsolete_cryptography_pin(self, _mock_plat):
        req = Requirement("cryptography<43,>=2.1.4")
        self.assertFalse(
            macos_intel_cryptography_rebuild_instead_of_find_links_skip(
                req,
                "find-links has cryptography up to 49.0.0 but none match <43,>=2.1.4",
            )
        )

    @patch("_helper_functions.matching_release_version_strings", return_value=["42.0.8"])
    @patch("_helper_functions.get_current_platform", return_value="macos_x86_64")
    def test_do_not_rebuild_pre_49_pin_behind_pypi(self, _mock_plat, _mock_pypi):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-41.0.7-cp38-abi3-macosx_10_12_x86_64.whl").write_bytes(b"")
            req = Requirement("cryptography<43,>=2.1.4")
            self.assertFalse(
                macos_intel_cryptography_rebuild_instead_of_find_links_skip(
                    req,
                    "find-links already has cryptography 41.0.7 matching <43,>=2.1.4",
                    links,
                )
            )

    @patch("_helper_functions.matching_release_version_strings", return_value=["44.0.3"])
    @patch("_helper_functions.get_current_platform", return_value="macos_x86_64")
    def test_do_not_rebuild_cryptography_lt_45_behind_pypi(self, _mock_plat, _mock_pypi):
        req = Requirement("cryptography<45,>=2.1.4")
        self.assertFalse(
            macos_intel_cryptography_rebuild_instead_of_find_links_skip(
                req,
                "find-links already has cryptography 41.0.7 matching <45,>=2.1.4",
            )
        )

    @patch("_helper_functions.matching_release_version_strings", return_value=["50.0.1"])
    @patch("_helper_functions.get_current_platform", return_value="linux_x86_64")
    def test_linux_does_not_rebuild_for_older_find_links(self, _mock_plat, _mock_pypi):
        req = Requirement("cryptography>=2.1.4")
        self.assertFalse(
            macos_intel_cryptography_rebuild_instead_of_find_links_skip(
                req,
                "find-links already has cryptography 49.0.0 matching >=2.1.4",
            )
        )


class TestPypiRequiresPythonPreflight(unittest.TestCase):
    """PyPI Requires-Python preflight (specifier + project index)."""

    def setUp(self):
        import _helper_functions

        _helper_functions._PYPI_REQUIRES_PYTHON_CACHE.clear()
        _helper_functions._PYPI_PROJECT_JSON_CACHE.clear()
        self._saved_skip_check = os.environ.pop("SKIP_PYPI_REQUIRES_PYTHON_CHECK", None)

    def tearDown(self):
        if self._saved_skip_check is not None:
            os.environ["SKIP_PYPI_REQUIRES_PYTHON_CHECK"] = self._saved_skip_check

    def test_current_interpreter_satisfies_requires_python(self):
        self.assertTrue(current_interpreter_satisfies_requires_python(None))
        self.assertTrue(current_interpreter_satisfies_requires_python(""))
        self.assertTrue(current_interpreter_satisfies_requires_python(">=3.8"))
        self.assertFalse(current_interpreter_satisfies_requires_python(">999.0.0"))

    @patch.dict(os.environ, {"SKIP_PYPI_REQUIRES_PYTHON_CHECK": "1"}, clear=False)
    def test_preflight_disabled_by_env(self):
        req = Requirement("idf-component-manager==3.0.0")
        skip, reason = pypi_requires_python_preflight_skip(req)
        self.assertFalse(skip)
        self.assertEqual(reason, "")

    @patch.dict(os.environ, {"SKIP_PYPI_REQUIRES_PYTHON_CHECK": "1"}, clear=False)
    @patch("_helper_functions.print_color")
    def test_filter_noop_when_env_disabled(self, _mock_print):
        s = {Requirement("a==1"), Requirement("b==2")}
        self.assertEqual(filter_requirements_by_pypi_requires_python(s), s)

    @patch("_helper_functions.fetch_pypi_project_json", return_value={"releases": {"3.0.0": []}})
    @patch("_helper_functions.current_interpreter_satisfies_requires_python", return_value=False)
    @patch("_helper_functions.fetch_pypi_release_requires_python", return_value=">=3.10")
    def test_preflight_skips_when_requires_python_excludes(self, _mock_rel, _mock_sat, _mock_proj):
        req = Requirement("idf-component-manager==3.0.0")
        skip, reason = pypi_requires_python_preflight_skip(req)
        self.assertTrue(skip)
        self.assertIn("3.0.0", reason)

    @patch("_helper_functions.fetch_pypi_project_json", return_value={"releases": {"3.0.0": []}})
    @patch("_helper_functions.current_interpreter_satisfies_requires_python", return_value=True)
    @patch("_helper_functions.fetch_pypi_release_requires_python", return_value=">=3.10")
    def test_preflight_keeps_when_compatible(self, _mock_rel, _mock_sat, _mock_proj):
        req = Requirement("idf-component-manager==3.0.0")
        skip, _ = pypi_requires_python_preflight_skip(req)
        self.assertFalse(skip)

    @patch("_helper_functions.fetch_pypi_project_json", return_value=None)
    @patch("_helper_functions.fetch_pypi_release_requires_python")
    def test_preflight_no_skip_when_project_json_unavailable(self, mock_release, _mock_proj):
        skip, _ = pypi_requires_python_preflight_skip(Requirement("idf-component-manager>=2"))
        self.assertFalse(skip)
        mock_release.assert_not_called()

    @patch("_helper_functions.fetch_pypi_project_json", return_value={"releases": {"3.0.0": [], "2.4.9": []}})
    @patch("_helper_functions.current_interpreter_satisfies_requires_python", return_value=False)
    @patch("_helper_functions.fetch_pypi_release_requires_python", return_value=">=3.10")
    def test_preflight_skips_compatible_release_spec(self, _mock_rel, _mock_sat, _mock_proj):
        skip, reason = pypi_requires_python_preflight_skip(Requirement("idf-component-manager~=3.0"))
        self.assertTrue(skip)
        self.assertIn("3.0.0", reason)

    @patch("_helper_functions.fetch_pypi_project_json", return_value={"releases": {"1.0.0": []}})
    @patch("_helper_functions.fetch_pypi_release_requires_python", return_value=None)
    def test_preflight_keeps_when_pypi_has_no_requires_python(self, _mock_fetch, _mock_proj):
        skip, _ = pypi_requires_python_preflight_skip(Requirement("somepkg==1.0.0"))
        self.assertFalse(skip)

    @patch("_helper_functions.print_color")
    def test_filter_requirements_drops_one(self, _mock_print):
        r_bad = Requirement("idf-component-manager==3.0.0")
        r_good = Requirement("requests==2.0.0")

        def _skip(req):
            if req.name == "idf-component-manager":
                return (True, "incompatible")
            return (False, "")

        with patch("_helper_functions.pypi_requires_python_preflight_skip", side_effect=_skip):
            out = filter_requirements_by_pypi_requires_python({r_bad, r_good})
        self.assertEqual(out, {r_good})


if __name__ == "__main__":
    unittest.main()
