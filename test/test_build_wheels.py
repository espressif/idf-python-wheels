# ruff: noqa: E501
# line too long skip in ruff for whole file (formatting would be worst than long lines)
#
# SPDX-FileCopyrightText: 2023-2024 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
import sys
import unittest

from pathlib import Path
from unittest.mock import patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from packaging.requirements import Requirement

from _helper_functions import get_no_binary_args
from _helper_functions import merge_requirements
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


class TestWheelCompatibility(unittest.TestCase):
    """Test the is_wheel_compatible function from test_wheels_install.py."""

    def setUp(self):
        """Import the function to test."""
        sys.path.insert(0, str(Path(__file__).parent))
        from test_wheels_install import is_wheel_compatible

        self.is_wheel_compatible = is_wheel_compatible

    def test_exact_python_version_match(self):
        """Test that cpXY wheels match the exact Python version."""
        self.assertTrue(self.is_wheel_compatible("numpy-1.0.0-cp311-cp311-linux_x86_64.whl", "311"))
        self.assertFalse(self.is_wheel_compatible("numpy-1.0.0-cp310-cp310-linux_x86_64.whl", "311"))

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
        self.assertTrue(self.is_wheel_compatible("cryptography-41.0.0-cp39-abi3-linux_x86_64.whl", "311"))
        self.assertTrue(self.is_wheel_compatible("cryptography-41.0.0-cp39-abi3-linux_x86_64.whl", "39"))


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
        """Test parsing wheel name with underscores (preserved, canonicalization done later)."""
        result = self.parse_wheel_name("ruamel_yaml_clib-0.2.8-cp311-cp311-linux_x86_64.whl")
        self.assertEqual(result, ("ruamel_yaml_clib", "0.2.8"))

    def test_parse_wheel_with_pre_release(self):
        """Test parsing wheel name with pre-release version."""
        result = self.parse_wheel_name("package-1.0.0a1-py3-none-any.whl")
        self.assertEqual(result, ("package", "1.0.0a1"))

    def test_parse_universal_wheel(self):
        """Test parsing universal wheel name."""
        result = self.parse_wheel_name("six-1.16.0-py2.py3-none-any.whl")
        self.assertEqual(result, ("six", "1.16.0"))


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


if __name__ == "__main__":
    unittest.main()
