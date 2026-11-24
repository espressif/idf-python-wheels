from __future__ import annotations

import unittest

from _helper_functions import parse_requirement_lines
from yaml_list_adapter import is_excluded
from yaml_list_adapter import load_exclude_rules


class TestExcludeRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_exclude_rules("exclude_list.yaml")

    def test_python_list_is_an_or_condition(self) -> None:
        self.assertTrue(is_excluded("tree-sitter", "0.23.0", "linux_x86_64", "3.8", self.rules))
        self.assertTrue(is_excluded("tree-sitter", "0.23.0", "linux_x86_64", "3.9", self.rules))
        self.assertFalse(is_excluded("tree-sitter", "0.23.0", "linux_x86_64", "3.10", self.rules))

    def test_platform_specific_rule(self) -> None:
        self.assertTrue(is_excluded("cryptography", "42.0.8", "linux_armv7", "3.8", self.rules))
        self.assertFalse(is_excluded("cryptography", "42.0.8", "linux_x86_64", "3.8", self.rules))

    def test_independent_rows_are_alternatives(self) -> None:
        self.assertTrue(is_excluded("pygobject", "3.48.0", "windows", "3.13", self.rules))
        self.assertTrue(is_excluded("pygobject", "3.48.0", "linux_x86_64", "3.13", self.rules))
        self.assertTrue(is_excluded("dbus-python", "1.3.2", "macos_arm64", "3.12", self.rules))
        self.assertFalse(is_excluded("dbus-python", "1.3.2", "macos_arm64", "3.10", self.rules))

    def test_platform_and_python_lists_keep_their_precedence(self) -> None:
        self.assertTrue(is_excluded("dbus-python", "1.3.2", "linux_x86_64", "3.8", self.rules))
        self.assertFalse(is_excluded("dbus-python", "1.3.2", "linux_x86_64", "3.9", self.rules))

    def test_unversioned_rule_excludes_all_versions(self) -> None:
        self.assertTrue(is_excluded("esptool", "5.0.0", "windows", "3.13", self.rules))

    def test_versioned_rule_only_excludes_matching_versions(self) -> None:
        self.assertTrue(is_excluded("gdbgui", "0.13.2.0", "linux", "3.11", self.rules))
        self.assertFalse(is_excluded("gdbgui", "0.15.2.0", "linux", "3.11", self.rules))


class TestRequirementLines(unittest.TestCase):
    def test_drops_pip_options_and_canonicalizes(self) -> None:
        parsed = parse_requirement_lines(
            ["# comment", "--only-binary cryptography", "esptool<6 and >=5.3.0.dev0"],
            source="test",
        )
        self.assertEqual([str(item) for item in parsed], ["esptool<6,>=5.3.0.dev0"])

    def test_rejects_unsafe_input(self) -> None:
        for line in ("pkg @ https://example.invalid/pkg.whl", "pkg==${VERSION}", "pkg \\\\"):
            with self.subTest(line=line):
                with self.assertRaises(ValueError):
                    parse_requirement_lines([line], source="test")

    def test_ignores_backslashes_in_comments_and_normalizes_marked_legacy_specs(self) -> None:
        parsed = parse_requirement_lines(
            ["pkg>=1 and <2; python_version >= '3.8' # C:\\tmp"],
            source="test",
        )
        self.assertEqual(str(parsed[0]), 'pkg<2,>=1; python_version >= "3.8"')


if __name__ == "__main__":
    unittest.main()
