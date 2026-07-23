#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import unittest

from packaging.requirements import Requirement

from _helper_functions import build_requires_python_map
from _helper_functions import parse_sdist_filename
from _helper_functions import pep503_file_link
from _helper_functions import sdist_allowed_for_upload


class TestPep503FileLink(unittest.TestCase):
    def test_matches_original_format_without_metadata(self) -> None:
        original = '<a href="/pypi/cryptography/cryptography-49.0.0.tar.gz">cryptography-49.0.0.tar.gz</a><br/>'
        link = pep503_file_link("cryptography", "cryptography-49.0.0.tar.gz")
        self.assertEqual(link, original)

    def test_includes_data_requires_python_when_metadata_provided(self) -> None:
        link = pep503_file_link(
            "cryptography",
            "cryptography-49.0.0.tar.gz",
            requires_python=">=3.9",
        )
        self.assertIn('data-requires-python="&gt;=3.9"', link)
        self.assertIn('href="/pypi/cryptography/cryptography-49.0.0.tar.gz"', link)

    def test_omits_attribute_when_requires_python_is_none(self) -> None:
        link = pep503_file_link("cryptography", "cryptography-49.0.0.tar.gz")
        self.assertNotIn("data-requires-python", link)
        self.assertIn('href="/pypi/cryptography/cryptography-49.0.0.tar.gz"', link)

    def test_escapes_html_in_specifier_strings(self) -> None:
        link = pep503_file_link(
            "pkg",
            "pkg-1.0.0.tar.gz",
            requires_python='>=3.9,">=3.9"',
        )
        self.assertIn("&gt;=3.9", link)
        self.assertIn("&quot;", link)
        self.assertNotIn('">=3.9"', link)


class TestParseSdistFilename(unittest.TestCase):
    def test_tar_gz(self) -> None:
        self.assertEqual(
            parse_sdist_filename("cryptography-49.0.0.tar.gz"),
            ("cryptography", "49.0.0"),
        )

    def test_zip(self) -> None:
        self.assertEqual(
            parse_sdist_filename("dbus_python-1.3.2.zip"),
            ("dbus-python", "1.3.2"),
        )

    def test_tar_bz2_fallback(self) -> None:
        self.assertEqual(
            parse_sdist_filename("example-1.2.3.tar.bz2"),
            ("example", "1.2.3"),
        )


class TestBuildRequiresPythonMap(unittest.TestCase):
    def test_deduplicates_pypi_lookups(self) -> None:
        filenames = [
            "index.html",
            "cryptography-49.0.0-cp39-cp39-linux_x86_64.whl",
            "cryptography-49.0.0.tar.gz",
        ]
        calls: list[tuple[str, str]] = []

        def fake_fetch(name: str, version: str) -> str | None:
            calls.append((name, version))
            return ">=3.9"

        result = build_requires_python_map(filenames, fetcher=fake_fetch)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result[("cryptography", "49.0.0")], ">=3.9")


class TestSdistUploadGuard(unittest.TestCase):
    def test_sdist_not_allowed_without_allowlist(self) -> None:
        self.assertFalse(sdist_allowed_for_upload("cryptography", "49.0.0", []))

    def test_sdist_allowed_when_listed_in_allowlist(self) -> None:
        allowlist = [Requirement("cryptography==49.0.0")]
        self.assertTrue(sdist_allowed_for_upload("cryptography", "49.0.0", allowlist))

    def test_sdist_not_allowed_when_version_mismatch(self) -> None:
        allowlist = [Requirement("cryptography==41.0.7")]
        self.assertFalse(sdist_allowed_for_upload("cryptography", "49.0.0", allowlist))


if __name__ == "__main__":
    unittest.main()
