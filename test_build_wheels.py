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
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from _helper_functions import _mirrored_manylinux228_version
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
from _helper_functions import mirror_pypi_manylinux228_wheel
from _helper_functions import pip_wheel_or_mirror_success
from _helper_functions import prune_ci_manylinux_newer_than_228
from _helper_functions import prune_ci_manylinux_newer_than_228_when_228_mirror_present
from _helper_functions import pypi_requires_python_preflight_skip
from _helper_functions import remove_find_links_wheels_for_package
from _helper_functions import should_skip_linux_auditwheel_for_pypi_mirror
from build_wheels import _add_into_requirements
from build_wheels import get_used_idf_branches
from yaml_list_adapter import YAMLListAdapter


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
    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_cffi_uses_build_isolation(self, _armv7: object) -> None:
        from _helper_functions import pip_wheel_invocation_args

        args = pip_wheel_invocation_args("cffi>=1.15.0")
        self.assertNotIn("--no-build-isolation", args)

    @patch("_helper_functions.is_linux_armv7_runner", return_value=True)
    def test_armv7_any_package_uses_build_isolation(self, _armv7: object) -> None:
        from _helper_functions import pip_wheel_invocation_args

        args = pip_wheel_invocation_args("esptool~=4.9")
        self.assertNotIn("--no-build-isolation", args)

    @patch("_helper_functions.is_linux_armv7_runner", return_value=False)
    def test_non_armv7_keeps_no_build_isolation(self, _armv7: object) -> None:
        from _helper_functions import pip_wheel_invocation_args

        args = pip_wheel_invocation_args("cffi>=1.15.0")
        self.assertIn("--no-build-isolation", args)


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
    """Test the should_exclude_wheel function from _helper_functions.py.

    Note: The function expects requirements created with exclude=True from YAMLListAdapter,
    which inverts the logic (e.g., ==1.5.0 becomes !=1.5.0).
    """

    def setUp(self):
        """Import the function to test."""
        from _helper_functions import should_exclude_wheel

        self.should_exclude_wheel = should_exclude_wheel

    def test_exclude_by_package_name_only(self):
        """Test excluding a package by name only (no inversion needed)."""
        # Package name only - same for both exclude=True and exclude=False
        exclude_requirements = {Requirement("esptool")}
        result, reason = self.should_exclude_wheel("esptool-4.0.0-py3-none-any.whl", exclude_requirements)
        self.assertTrue(result)
        self.assertIn("esptool", reason)

    def test_exclude_by_version(self):
        """Test excluding a package by version constraint (inverted specifier)."""
        # With exclude=True, ==1.5.0 becomes !=1.5.0
        # So version 1.5.0 is NOT in !=1.5.0 -> should EXCLUDE
        # And version 2.0.0 IS in !=1.5.0 -> should KEEP
        exclude_requirements = {Requirement("gevent!=1.5.0")}
        # Should exclude 1.5.0 (not in !=1.5.0)
        result, _ = self.should_exclude_wheel("gevent-1.5.0-cp311-cp311-linux_x86_64.whl", exclude_requirements)
        self.assertTrue(result)
        # Should not exclude 2.0.0 (is in !=1.5.0)
        result, _ = self.should_exclude_wheel("gevent-2.0.0-cp311-cp311-linux_x86_64.whl", exclude_requirements)
        self.assertFalse(result)

    def test_no_match_returns_false(self):
        """Test that non-matching packages return False."""
        exclude_requirements = {Requirement("esptool")}
        result, _ = self.should_exclude_wheel("numpy-1.24.0-cp311-cp311-linux_x86_64.whl", exclude_requirements)
        self.assertFalse(result)

    def test_exclude_with_version_range(self):
        """Test excluding a package with version range (inverted specifier)."""
        # With exclude=True, ==9.5.0 becomes !=9.5.0
        exclude_requirements = {Requirement("pillow!=9.5.0")}
        # Should exclude 9.5.0 (not in !=9.5.0)
        result, _ = self.should_exclude_wheel("Pillow-9.5.0-cp311-cp311-linux_x86_64.whl", exclude_requirements)
        self.assertTrue(result)
        # Should not exclude 10.0.0 (is in !=9.5.0)
        result, _ = self.should_exclude_wheel("Pillow-10.0.0-cp311-cp311-linux_x86_64.whl", exclude_requirements)
        self.assertFalse(result)


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
        ):
            self.assertTrue(force_interpreter_skip_package(canonicalize_name(name)))

    def test_requests_not_in_skip_set(self):
        self.assertFalse(force_interpreter_skip_package(canonicalize_name("requests")))

    def test_find_links_superseded_obsolete_cryptography_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cryptography-48.0.0-cp313-abi3-linux_armv7l.whl").write_bytes(b"")
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

    def test_find_links_skip_superseded_exact_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            links = Path(tmp)
            (links / "cffi-2.1.0-cp314-cp314-manylinux2014_x86_64.whl").write_bytes(b"")
            req = Requirement("cffi==1.17.1")
            skip, reason = find_links_wheel_build_skip(req, links)
            self.assertTrue(skip)
            self.assertIn("newer than obsolete pin", reason)
            self.assertIn("1.17.1", reason)

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


class TestPipWheelOrMirrorSuccess(unittest.TestCase):
    @patch("_helper_functions.mirror_pypi_manylinux228_wheel", return_value=True)
    def test_pip_success_still_mirrors_and_returns_true(self, mock_mirror):
        self.assertTrue(pip_wheel_or_mirror_success("greenlet>3.0.0", 0))
        mock_mirror.assert_called_once()

    @patch("_helper_functions.mirror_pypi_manylinux228_wheel", return_value=True)
    def test_pip_failure_succeeds_when_mirror_works(self, mock_mirror):
        self.assertTrue(pip_wheel_or_mirror_success("pillow!=9.5.0", 1))
        mock_mirror.assert_called_once()

    @patch("_helper_functions.mirror_pypi_manylinux228_wheel", return_value=False)
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


class TestSkipAuditwheelForPypiMirror(unittest.TestCase):
    @patch("platform.machine", return_value="x86_64")
    def test_skips_cryptography_manylinux_228_mirror(self, _machine):
        name = "cryptography-49.0.0-cp311-abi3-manylinux_2_28_x86_64.whl"
        self.assertTrue(should_skip_linux_auditwheel_for_pypi_mirror(name))

    @patch("platform.machine", return_value="x86_64")
    def test_does_not_skip_ci_manylinux_234_only(self, _machine):
        name = "cryptography-49.0.0-cp312-abi3-manylinux_2_34_x86_64.whl"
        self.assertFalse(should_skip_linux_auditwheel_for_pypi_mirror(name))


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
