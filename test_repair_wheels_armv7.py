#
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import tempfile
import unittest
import unittest.mock
import zipfile

from contextlib import ExitStack
from pathlib import Path

import repair_wheels as rw


def _record_sha256(payload: bytes) -> str:
    return rw._wheel_record_sha256(payload)


def _write_minimal_linux_armv7_wheel(path: Path, dist_name: str = "pkg", version: str = "1.0") -> None:
    wheel_info = f"{dist_name}-{version}.dist-info"
    wheel_path = path / f"{dist_name}-{version}-cp38-cp38-linux_armv7l.whl"
    record = f"""{dist_name}/__init__.py,sha256=2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043353845a0300,2
{wheel_info}/WHEEL,sha256=placeholder,0
{wheel_info}/RECORD,
"""
    wheel_meta = """Wheel-Version: 1.0
Generator: test
Root-Is-Purelib: false
Tag: cp38-cp38-linux_armv7l
"""
    payload = b"x\n"
    digest = _record_sha256(wheel_meta.encode())
    record = record.replace(
        f"{wheel_info}/WHEEL,sha256=placeholder,0",
        f"{wheel_info}/WHEEL,sha256={digest},{len(wheel_meta.encode())}",
    )
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_name}/__init__.py", payload)
        zf.writestr(f"{wheel_info}/WHEEL", wheel_meta)
        zf.writestr(f"{wheel_info}/RECORD", record)


class TestRetagLinuxArmv7Wheel(unittest.TestCase):
    def test_retag_to_manylinux_2_36(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_minimal_linux_armv7_wheel(root)
            src = root / "pkg-1.0-cp38-cp38-linux_armv7l.whl"
            out = rw._retag_linux_armv7_wheel_to_plat(src, "manylinux_2_36_armv7l")
            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(out.name, "pkg-1.0-cp38-cp38-manylinux_2_36_armv7l.whl")
            self.assertFalse(src.exists())
            with zipfile.ZipFile(out, "r") as zf:
                wheel_txt = zf.read("pkg-1.0.dist-info/WHEEL").decode()
                record = zf.read("pkg-1.0.dist-info/RECORD").decode()
            self.assertIn("cp38-cp38-manylinux_2_36_armv7l", wheel_txt)
            wheel_line = next(line for line in record.splitlines() if line.startswith("pkg-1.0.dist-info/WHEEL"))
            digest = wheel_line.split(",")[1].split("=", 1)[1]
            self.assertNotEqual(len(digest), 64)
            self.assertEqual(digest, _record_sha256(wheel_txt.encode()))

    def test_retag_abi3_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel_info = "psutil-7.2.2.dist-info"
            src = root / "psutil-7.2.2-cp36-abi3-linux_armv7l.whl"
            record = f"""psutil/__init__.py,sha256=2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043353845a0300,2
{wheel_info}/WHEEL,sha256=placeholder,0
{wheel_info}/RECORD,
"""
            wheel_meta = """Wheel-Version: 1.0
Generator: test
Root-Is-Purelib: false
Tag: cp36-abi3-linux_armv7l
"""
            payload = b"x\n"
            digest = _record_sha256(wheel_meta.encode())
            record = record.replace(
                f"{wheel_info}/WHEEL,sha256=placeholder,0",
                f"{wheel_info}/WHEEL,sha256={digest},{len(wheel_meta.encode())}",
            )
            with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("psutil/__init__.py", payload)
                zf.writestr(f"{wheel_info}/WHEEL", wheel_meta)
                zf.writestr(f"{wheel_info}/RECORD", record)

            out = rw._retag_linux_armv7_wheel_to_plat(src, "manylinux_2_31_armv7l")
            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(out.name, "psutil-7.2.2-cp36-abi3-manylinux_2_31_armv7l.whl")

    def test_retag_many_entry_wheel(self) -> None:
        """Regression: pycryptodome-style wheels with hundreds of zip members."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel_info = "pycryptodome-3.23.0.dist-info"
            src = root / "pycryptodome-3.23.0-cp37-abi3-linux_armv7l.whl"
            wheel_meta = """Wheel-Version: 1.0
Generator: test
Root-Is-Purelib: false
Tag: cp37-abi3-linux_armv7l
"""
            digest = _record_sha256(wheel_meta.encode())
            record_lines = [
                f"Crypto/mod_{i}.py,sha256=2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043353845a0300,2"
                for i in range(200)
            ]
            record_lines.append(f"{wheel_info}/WHEEL,sha256={digest},{len(wheel_meta.encode())}")
            record_lines.append(f"{wheel_info}/RECORD,")
            record = "\n".join(record_lines) + "\n"
            with zipfile.ZipFile(src, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for i in range(200):
                    zf.writestr(f"Crypto/mod_{i}.py", b"x\n")
                zf.writestr(f"{wheel_info}/WHEEL", wheel_meta)
                zf.writestr(f"{wheel_info}/RECORD", record)

            out = rw._retag_linux_armv7_wheel_to_plat(src, "manylinux_2_31_armv7l")
            self.assertIsNotNone(out)
            assert out is not None
            self.assertEqual(out.name, "pycryptodome-3.23.0-cp37-abi3-manylinux_2_31_armv7l.whl")
            with zipfile.ZipFile(out, "r") as zf:
                self.assertEqual(len(zf.namelist()), 202)
                self.assertIn("cp37-abi3-manylinux_2_31_armv7l", zf.read(f"{wheel_info}/WHEEL").decode())

    def test_collision_resolved_after_retag(self) -> None:
        import check_wheel_collisions as cwc

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "wheels-repaired-linux-armv7").mkdir()
            (root / "wheels-repaired-linux-armv7legacy").mkdir()
            _write_minimal_linux_armv7_wheel(root / "wheels-repaired-linux-armv7", "pkg")
            _write_minimal_linux_armv7_wheel(root / "wheels-repaired-linux-armv7legacy", "pkg")
            v7 = root / "wheels-repaired-linux-armv7" / "pkg-1.0-cp38-cp38-linux_armv7l.whl"
            leg = root / "wheels-repaired-linux-armv7legacy" / "pkg-1.0-cp38-cp38-linux_armv7l.whl"
            leg.write_bytes(leg.read_bytes() + b"extra")
            rw._retag_linux_armv7_wheel_to_plat(v7, "manylinux_2_36_armv7l")
            rw._retag_linux_armv7_wheel_to_plat(leg, "manylinux_2_31_armv7l")
            self.assertEqual(cwc.collect_collision_errors(root), [])

    def test_prune_manylinux_only_when_same_version_has_linux_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_minimal_linux_armv7_wheel(root, "pkg", "1.0")
            manylinux_v2 = root / "pkg-2.0-cp38-cp38-manylinux_2_31_armv7l.whl"
            manylinux_v2.write_bytes(b"placeholder-manylinux-only")

            removed = rw._prune_manylinux_armv7_when_linux_tag_present(root)
            self.assertEqual(removed, 0)
            self.assertTrue(manylinux_v2.exists())

            linux_v1 = root / "pkg-1.0-cp38-cp38-linux_armv7l.whl"
            manylinux_v1 = root / "pkg-1.0-cp38-cp38-manylinux_2_31_armv7l.whl"
            manylinux_v1.write_bytes(linux_v1.read_bytes())
            removed = rw._prune_manylinux_armv7_when_linux_tag_present(root)
            self.assertEqual(removed, 1)
            self.assertFalse(manylinux_v1.exists())
            self.assertTrue(manylinux_v2.exists())


class TestManylinuxGlibcTags(unittest.TestCase):
    def test_parse_wheel_and_plat_tags(self) -> None:
        from _helper_functions import armv7_wheel_matches_forced_plat
        from _helper_functions import manylinux_glibc_tags_in_name

        name = "cffi-2.0.0-cp311-cp311-manylinux_2_31_armv7l.manylinux_2_36_armv7l.whl"
        self.assertEqual(manylinux_glibc_tags_in_name(name), [(2, 31), (2, 36)])
        self.assertTrue(armv7_wheel_matches_forced_plat(name, "manylinux_2_36_armv7l", only_plat=True))
        self.assertFalse(armv7_wheel_matches_forced_plat(name, "manylinux_2_31_armv7l", only_plat=True))
        self.assertTrue(
            armv7_wheel_matches_forced_plat(
                name,
                "manylinux_2_36_armv7l",
                only_plat=True,
                repair=True,
            )
        )
        self.assertTrue(
            armv7_wheel_matches_forced_plat(
                "cffi-2.0.0-cp311-cp311-manylinux_2_36_armv7l.whl",
                "manylinux_2_36_armv7l",
                only_plat=True,
            )
        )


class TestAuditwheelNotPlatformWheel(unittest.TestCase):
    def test_keeps_wheel_on_x86_64_when_no_elf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheels_dir = root / "downloaded_wheels"
            wheels_dir.mkdir()
            wheel = wheels_dir / "dbus_fast-2.24.4-cp310-cp310-manylinux_2_39_x86_64.whl"
            with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("dbus_fast/__init__.py", "x\n")

            auditwheel_msg = (
                "INFO:auditwheel.main_repair:Repairing dbus_fast-2.24.4-cp310-cp310-manylinux_2_39_x86_64.whl\n"
                "INFO:auditwheel.main_repair:This does not look like a platform wheel, "
                "no ELF executable or shared library file (including compiled Python C extension) "
                "found in the wheel archive"
            )
            fake_result = type(
                "R",
                (),
                {"returncode": 1, "stdout": auditwheel_msg, "stderr": ""},
            )()

            with ExitStack() as stack:
                stack.enter_context(unittest.mock.patch.object(rw, "get_platform", return_value="Linux"))
                stack.enter_context(unittest.mock.patch.object(rw.platform, "machine", return_value="x86_64"))
                stack.enter_context(unittest.mock.patch.object(rw, "repair_wheel_linux", return_value=fake_result))
                stack.enter_context(unittest.mock.patch.object(rw, "wheel_archive_is_readable", return_value=True))
                stack.enter_context(
                    unittest.mock.patch.object(rw, "should_skip_linux_auditwheel_for_pypi_mirror", return_value=False)
                )
                stack.enter_context(
                    unittest.mock.patch.object(
                        rw,
                        "prune_ci_manylinux_newer_than_228_when_228_mirror_present",
                        return_value=0,
                    )
                )
                orig_cwd = os.getcwd()
                try:
                    os.chdir(root)
                    rw.main()
                finally:
                    os.chdir(orig_cwd)

            self.assertTrue(wheel.exists())


if __name__ == "__main__":
    unittest.main()
