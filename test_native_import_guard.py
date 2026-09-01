#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

from __future__ import annotations

import tempfile
import unittest
import zipfile

from pathlib import Path

from native_import_guard import DEFAULT_SKIP_TOP_LEVEL_IMPORTS
from native_import_guard import NativeImportGuardConfig
from native_import_guard import NativeImportGuardEntry
from native_import_guard import default_native_import_statements
from native_import_guard import is_pure_any_wheel_name
from native_import_guard import load_native_import_guard
from native_import_guard import native_import_guard_by_name
from native_import_guard import package_import_statements
from native_import_guard import resolve_native_import_statements


def _cfg(
    *packages: NativeImportGuardEntry,
    probe_unlisted: bool = True,
    skip_pure_any: bool = True,
) -> NativeImportGuardConfig:
    return NativeImportGuardConfig(
        probe_unlisted=probe_unlisted,
        skip_pure_any=skip_pure_any,
        skip_top_level=DEFAULT_SKIP_TOP_LEVEL_IMPORTS,
        packages=packages,
    )


def _write_wheel(tmp: str, filename: str, top_level: str = "") -> Path:
    wheel = Path(tmp) / filename
    if top_level:
        with zipfile.ZipFile(wheel, "w") as zf:
            info = filename.replace(".whl", "").rsplit("-", 3)[0]
            zf.writestr(f"{info}.dist-info/top_level.txt", top_level)
    else:
        wheel.write_bytes(b"not-a-zip")
    return wheel


class TestNativeImportGuard(unittest.TestCase):
    def test_loads_cryptography_fallback(self) -> None:
        guarded = native_import_guard_by_name()
        entry = guarded["cryptography"]
        code = "\n".join(entry.imports)
        self.assertIn("_rust", code)
        self.assertIn("_openssl", code)
        self.assertLess(code.index("_rust"), code.index("_openssl"))
        self.assertIn("from cryptography import x509", code)
        self.assertIn("cryptography import OK", code)

    def test_loads_psutil(self) -> None:
        guarded = native_import_guard_by_name()
        entry = guarded["psutil"]
        code = "\n".join(entry.imports)
        self.assertIn("import psutil", code)
        self.assertIn("psutil import OK", code)

    def test_yaml_uses_package_name_and_probe_unlisted(self) -> None:
        cfg = load_native_import_guard()
        self.assertTrue(cfg.probe_unlisted)
        self.assertTrue(cfg.skip_pure_any)
        self.assertEqual(cfg.skip_top_level, DEFAULT_SKIP_TOP_LEVEL_IMPORTS)
        names = {entry.name for entry in cfg.packages}
        self.assertIn("cffi", names)
        self.assertIn("psutil", names)
        self.assertIn("pillow", names)
        self.assertIn("pyyaml", names)
        self.assertIn("brotli", names)
        self.assertIn("pycryptodome", names)

    def test_loads_deeper_native_imports(self) -> None:
        guarded = native_import_guard_by_name()
        self.assertEqual(guarded["pillow"].imports, ("from PIL import Image",))
        self.assertEqual(guarded["pyyaml"].imports, ("from yaml import CSafeLoader",))
        self.assertEqual(guarded["brotli"].imports, ("import brotli",))
        self.assertEqual(guarded["pycryptodome"].imports, ("from Crypto.Cipher import AES",))

    def test_pure_any_wheel_name(self) -> None:
        self.assertTrue(is_pure_any_wheel_name("six-1.16.0-py2.py3-none-any.whl"))
        self.assertFalse(is_pure_any_wheel_name("psutil-7.2.2-cp36-abi3-macosx_10_9_x86_64.whl"))

    def test_default_imports_from_top_level_txt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "greenlet-3.1.0-cp311-cp311-macosx_10_9_x86_64.whl"
            with zipfile.ZipFile(wheel, "w") as zf:
                zf.writestr("greenlet-3.1.0.dist-info/top_level.txt", "greenlet\ntest\n")
            self.assertEqual(default_native_import_statements(wheel), ("import greenlet",))

    def test_default_imports_skip_when_top_level_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "ruamel.yaml.clib-0.2.12-cp311-cp311-macosx_10_9_x86_64.whl"
            wheel.write_bytes(b"not-a-zip")
            self.assertEqual(default_native_import_statements(wheel), ())

    def test_default_imports_prefers_public_matching_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "greenlet-3.1.0-cp311-cp311-macosx_10_9_x86_64.whl"
            with zipfile.ZipFile(wheel, "w") as zf:
                zf.writestr("greenlet-3.1.0.dist-info/top_level.txt", "_greenlet\ngreenlet\n")
            self.assertEqual(default_native_import_statements(wheel), ("import greenlet",))

    def test_resolve_custom_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = _write_wheel(tmp, "cffi-1.17.1-cp311-cp311-manylinux_2_28_x86_64.whl")
            cfg = _cfg(
                NativeImportGuardEntry(
                    name="cffi",
                    imports=("import _cffi_backend",),
                    skip=False,
                    raw={"package_name": "cffi"},
                )
            )
            self.assertEqual(
                resolve_native_import_statements(wheel, config=cfg, current_platform="linux_x86_64"),
                ("import _cffi_backend",),
            )

    def test_resolve_skip_on_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = _write_wheel(
                tmp,
                "foo-1.0.0-cp311-cp311-linux_armv7l.whl",
                top_level="foo\n",
            )
            cfg = _cfg(
                NativeImportGuardEntry(
                    name="foo",
                    imports=(),
                    skip=True,
                    raw={"package_name": "foo", "skip": True, "platform": "linux_armv7"},
                )
            )
            self.assertIsNone(resolve_native_import_statements(wheel, config=cfg, current_platform="linux_armv7"))
            self.assertEqual(
                resolve_native_import_statements(wheel, config=cfg, current_platform="macos_x86_64"),
                ("import foo",),
            )

    def test_resolve_python_and_version_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = _write_wheel(
                tmp,
                "bar-2.0.0-cp311-cp311-manylinux_2_28_x86_64.whl",
                top_level="bar\n",
            )
            cfg = _cfg(
                NativeImportGuardEntry(
                    name="bar",
                    imports=("import bar_ext",),
                    skip=False,
                    raw={"package_name": "bar", "python": ">=3.10", "version": ">=2"},
                )
            )
            self.assertEqual(
                resolve_native_import_statements(
                    wheel,
                    config=cfg,
                    current_platform="linux_x86_64",
                    python_version="3.11",
                ),
                ("import bar_ext",),
            )
            # Unmatched filters treat the package as unlisted (default top_level import).
            self.assertEqual(
                resolve_native_import_statements(
                    wheel,
                    config=cfg,
                    current_platform="linux_x86_64",
                    python_version="3.9",
                ),
                ("import bar",),
            )

    def test_resolve_probe_unlisted_false_skips_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = _write_wheel(
                tmp,
                "greenlet-3.1.0-cp311-cp311-macosx_10_9_x86_64.whl",
                top_level="greenlet\n",
            )
            cfg = _cfg(probe_unlisted=False)
            self.assertIsNone(resolve_native_import_statements(wheel, config=cfg, current_platform="macos_x86_64"))

    def test_resolve_skips_pure_any(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = _write_wheel(tmp, "six-1.16.0-py2.py3-none-any.whl", top_level="six\n")
            cfg = _cfg()
            self.assertIsNone(resolve_native_import_statements(wheel, config=cfg))

    def test_invalid_version_specifier_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = _write_wheel(tmp, "bar-2.0.0-cp311-cp311-manylinux_2_28_x86_64.whl")
            cfg = _cfg(
                NativeImportGuardEntry(
                    name="bar",
                    imports=("import bar_ext",),
                    skip=False,
                    raw={"package_name": "bar", "version": ">>>1"},
                )
            )
            with self.assertRaises(ValueError) as caught:
                resolve_native_import_statements(wheel, config=cfg, current_platform="linux_x86_64")
            self.assertIn(">>>1", str(caught.exception))
            self.assertIn("native_import_guard.yaml", str(caught.exception))

    def test_package_import_statements_applies_platform_filter(self) -> None:
        cfg = _cfg(
            NativeImportGuardEntry(
                name="foo",
                imports=("import foo_ext",),
                skip=False,
                raw={"package_name": "foo", "platform": "linux_armv7"},
            )
        )
        self.assertEqual(
            package_import_statements("foo", config=cfg, current_platform="linux_armv7"),
            ("import foo_ext",),
        )
        self.assertIsNone(package_import_statements("foo", config=cfg, current_platform="macos_x86_64"))


if __name__ == "__main__":
    unittest.main()
