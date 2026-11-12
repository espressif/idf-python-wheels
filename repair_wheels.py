#
# SPDX-FileCopyrightText: 2025 Espressif Systems (Shanghai) CO LTD
#
# SPDX-License-Identifier: Apache-2.0
#
"""
Repairs wheels for dynamically linked libraries on all platforms to ensure broad compatibility.
If this is not able to achieve the broken wheel is deleted and not published to Espressif's PyPI.
See: https://github.com/espressif/idf-python-wheels/blob/main/README.md#universal-wheel-tag---linking-of-dynamic-libraries
- Windows: delvewheel (bundles DLLs)
- macOS: delocate (bundles dylibs)
- Linux: auditwheel (bundles SOs)
"""

from __future__ import annotations

import base64
import hashlib
import os
import platform
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import List
from typing import NamedTuple
from typing import Set
from typing import Tuple
from typing import Union

from colorama import Fore

from _helper_functions import _REPO_ROOT
from _helper_functions import _force_no_binary_linux_normalized
from _helper_functions import _is_linux_tag_armv7_wheel_name as _is_linux_tag_wheel
from _helper_functions import armv7_wheel_matches_forced_plat
from _helper_functions import manylinux_glibc_tags_in_name
from _helper_functions import parse_wheel_name
from _helper_functions import print_color
from _helper_functions import wheel_archive_is_readable
from wheel_compat_policy import prune_ci_macos_newer_than_pypi_mirror
from wheel_compat_policy import prune_ci_manylinux_newer_than_228_when_228_mirror_present
from wheel_compat_policy import should_skip_linux_auditwheel_for_pypi_mirror
from wheel_compat_policy import should_skip_macos_delocate_for_pypi_mirror


def _stderr_indicates_bad_zip(error_msg: str) -> bool:
    """True if repair tool output indicates an unreadable/corrupt zip archive."""
    if not error_msg:
        return False
    return (
        "BadZipFile" in error_msg
        or "Bad magic number for central directory" in error_msg
        or "File is not a zip file" in error_msg
        or "zlib.error" in error_msg
        or "invalid literal/length/distance code" in error_msg
    )


def _wheel_record_sha256(payload: bytes) -> str:
    """PEP 427 RECORD hash: url-safe base64 of SHA-256, no padding."""
    return base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode("ascii").rstrip("=")


def _dedupe_wheel_paths(wheels_dir: Path) -> List[Path]:
    """Collect *.whl under wheels_dir once per inode (rglob can list the same file twice via symlinks)."""
    wheels: List[Path] = []
    seen: Set[Tuple[int, int]] = set()
    for p in sorted(wheels_dir.rglob("*.whl")):
        try:
            if not p.is_file():
                continue
            st = p.stat()
            key = (st.st_dev, st.st_ino)
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        wheels.append(p)
    return wheels


def get_platform() -> str:
    return platform.system()


def is_pure_python_wheel(wheel_name: str) -> bool:
    return "py3-none-any" in wheel_name


def is_platform_wheel(wheel_name: str, target_platform: str, current_arch: Union[str, None] = None) -> bool:
    """Check if wheel is for the target platform and architecture."""
    if target_platform == "Windows":
        return "win" in wheel_name
    elif target_platform in ["Darwin", "macOS ARM", "macOS Intel"]:
        if "macosx" not in wheel_name:
            return False
        if target_platform == "macOS ARM" or (target_platform == "Darwin" and current_arch == "arm64"):
            return "arm64" in wheel_name or "universal2" in wheel_name
        elif target_platform == "macOS Intel" or (target_platform == "Darwin" and current_arch == "x86_64"):
            return "x86_64" in wheel_name or "universal2" in wheel_name
        return True  # If no specific arch check needed
    elif target_platform == "Linux":
        return "linux" in wheel_name
    return False


def get_wheel_arch(wheel_name: str) -> Union[str, None]:
    """Extract architecture from wheel filename."""
    # {name}-{version}-{python_tag}-{abi_tag}-{platform_tag}.whl
    parts = wheel_name.replace(".whl", "").split("-")
    if len(parts) >= 5:
        platform_tag = parts[-1]
        for arch in ["x86_64", "aarch64", "armv7l"]:
            if arch in platform_tag:
                return arch
    return None


def _only_plat_env_enabled() -> bool:
    return os.environ.get("AUDITWHEEL_ONLY_PLAT", "").strip().lower() in ("1", "true", "yes")


def _allow_linux_tag_env_enabled() -> bool:
    """When true, allow keeping linux-tag wheels on ARMv7 even if --plat is set.

    This is useful when resolution prefers piwheels, which may provide wheels
    tagged as ``linux_armv7l`` that are not repairable to the desired manylinux
    tag in our repair containers.
    """
    return os.environ.get("AUDITWHEEL_ALLOW_LINUX_TAG", "").strip().lower() in ("1", "true", "yes")


def _should_skip_armv7_auditwheel(wheel_name: str, current_arch: str) -> bool:
    """Keep ``linux_armv7l`` wheels (piwheels / local) — do not retag with auditwheel manylinux on ARMv7.

    When ``AUDITWHEEL_PLAT`` is set, ``linux_armv7l`` wheels are retagged via
    ``_retag_linux_armv7_wheel_to_plat`` instead (see main loop).
    """
    return current_arch == "armv7l" and _is_linux_tag_wheel(wheel_name)


_LINUX_ARMV7L_WHL_SUFFIX = "-linux_armv7l.whl"
_LINUX_ARMV7L_TAG_RE = re.compile(r"-linux_armv7l$", re.IGNORECASE)


def _armv7_retag_stage_path() -> Path:
    """Stage retagged wheels on local FS, not the bind-mounted wheels dir (QEMU ARMv7 Docker)."""
    fd, staged = tempfile.mkstemp(prefix="retag-", suffix=".whl", dir=tempfile.gettempdir())
    os.close(fd)
    return Path(staged)


def _rewrite_wheel_tags(wheel_text: str, plat: str) -> bytes:
    new_lines: list[str] = []
    for line in wheel_text.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("Tag:") and _LINUX_ARMV7L_TAG_RE.search(stripped):
            line = _LINUX_ARMV7L_TAG_RE.sub(f"-{plat}", stripped)
        new_lines.append(line)
    return ("\n".join(new_lines) + "\n").encode("utf-8")


def _rewrite_wheel_record(record_text: str, wheel_info_path: str, updated_wheel: bytes) -> bytes:
    record_lines: list[str] = []
    for line in record_text.splitlines():
        if not line.strip():
            continue
        rec_name = line.split(",", 1)[0]
        if rec_name.lower() == wheel_info_path.lower():
            digest = _wheel_record_sha256(updated_wheel)
            line = f"{rec_name},sha256={digest},{len(updated_wheel)}"
        record_lines.append(line)
    return ("\n".join(record_lines) + "\n").encode("utf-8")


def _dist_info_paths(zin: zipfile.ZipFile) -> tuple[str | None, str | None]:
    names = [info.filename for info in zin.infolist()]
    wheel_info_path = next((name for name in names if name.lower().endswith(".dist-info/wheel")), None)
    record_path = next((name for name in names if name.lower().endswith(".dist-info/record")), None)
    return wheel_info_path, record_path


def _member_payload(
    zin: zipfile.ZipFile,
    name: str,
    wheel_info_path: str,
    updated_wheel: bytes,
    record_path: str | None,
    updated_record: bytes | None,
) -> bytes:
    if name == wheel_info_path:
        return updated_wheel
    if record_path and name == record_path and updated_record is not None:
        return updated_record
    return zin.read(name)


def _so_basename(filename: str) -> str | None:
    base = filename.rsplit("/", 1)[-1]
    if ".so" not in base:
        return None
    return base


def _note_so(filename: str, data: bytes, needed: set[str], glibc: set[tuple[int, int]]) -> str | None:
    """Record one shared object's dependencies. A reason means the wheel must not be published."""
    base = _so_basename(filename)
    if base is None:
        return None
    try:
        parsed = _elf_needed_and_glibc(data)
    except ValueError:
        return f"could not read ELF dynamic segment in {base}"
    if parsed is None:
        return None
    got_needed, got_glibc = parsed
    needed.update(got_needed)
    glibc.update(got_glibc)
    return None


def _write_retagged_wheel(wheel_path: Path, plat: str, staged: Path) -> str | None:
    """Write ``staged``. Return a policy reason, or None when the wheel may be published.

    Raises when the archive cannot be retagged. The caller leaves the original file in place.
    """
    with zipfile.ZipFile(wheel_path, "r") as zin:
        wheel_info_path, record_path = _dist_info_paths(zin)
        if not wheel_info_path:
            raise ValueError("missing WHEEL")
        updated_wheel = _rewrite_wheel_tags(zin.read(wheel_info_path).decode("utf-8"), plat)
        updated_record = None
        if record_path:
            updated_record = _rewrite_wheel_record(
                zin.read(record_path).decode("utf-8"),
                wheel_info_path,
                updated_wheel,
            )
        bundled = {info.filename.rsplit("/", 1)[-1] for info in zin.infolist()}
        return _copy_members_and_scan(
            zin,
            staged,
            plat,
            bundled,
            wheel_info_path,
            updated_wheel,
            record_path,
            updated_record,
        )


def _copy_members_and_scan(
    zin: zipfile.ZipFile,
    staged: Path,
    plat: str,
    bundled: set[str],
    wheel_info_path: str,
    updated_wheel: bytes,
    record_path: str | None,
    updated_record: bytes | None,
) -> str | None:
    needed: set[str] = set()
    glibc: set[tuple[int, int]] = set()
    with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.is_dir():
                continue
            data = _member_payload(
                zin,
                info.filename,
                wheel_info_path,
                updated_wheel,
                record_path,
                updated_record,
            )
            reason = _note_so(info.filename, data, needed, glibc)
            if reason:
                return reason
            zout.writestr(info, data)
    return _armv7_plat_violation(needed, glibc, bundled, plat)


def _publish_staged_wheel(staged: Path, new_path: Path, wheel_path: Path) -> Path | None:
    if not wheel_archive_is_readable(staged):
        return None
    new_path.parent.mkdir(parents=True, exist_ok=True)
    # ``/tmp`` and the bind-mounted ``/work/downloaded_wheels`` are different filesystems in
    # ARMv7 Docker; ``os.replace`` raises EXDEV there. Copy instead of rename.
    shutil.copy2(staged, new_path)
    if not wheel_archive_is_readable(new_path):
        new_path.unlink(missing_ok=True)
        return None
    wheel_path.unlink(missing_ok=True)
    return new_path


def _retag_linux_armv7_wheel_to_plat(wheel_path: Path, plat: str) -> tuple[Path | None, str | None]:
    """Retag a piwheels ``linux_armv7l`` wheel to ``plat`` without auditwheel relink.

    Returns ``(new_path, None)`` when the retag is published, ``(None, reason)`` when the
    wheel must not be published, or ``(None, None)`` when the archive could not be rewritten.
    The original file is removed only after the new archive is in place and meets ``plat``.
    A crash before that leaves ``linux_armv7l`` so a rerun repeats the same retag.

    Current and legacy builds both produce ``*-linux_armv7l.whl``. The platform tag
    is the repair container: Bookworm ``manylinux_2_36`` for current, Bullseye
    ``manylinux_2_31`` for legacy.

    Writes to a container-local staging file first (many-entry wheels such as pycryptodome
    can fail when rebuilt directly on the QEMU bind-mounted ``downloaded_wheels`` tree).
    """
    if not _is_linux_tag_wheel(wheel_path.name) or not wheel_path.name.lower().endswith(_LINUX_ARMV7L_WHL_SUFFIX):
        return None, None
    new_name = wheel_path.name[: -len(_LINUX_ARMV7L_WHL_SUFFIX)] + f"-{plat}.whl"
    if new_name == wheel_path.name:
        return None, None
    new_path = wheel_path.parent / new_name
    staged = _armv7_retag_stage_path()
    try:
        reason = _write_retagged_wheel(wheel_path, plat, staged)
        if reason:
            return None, reason
        published = _publish_staged_wheel(staged, new_path, wheel_path)
        if published is None:
            return None, None
        return published, None
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError, ValueError):
        return None, None
    finally:
        staged.unlink(missing_ok=True)


def _retag_with_one_retry(wheel_path: Path, plat: str) -> tuple[Path | None, str | None]:
    retagged, reason = _retag_linux_armv7_wheel_to_plat(wheel_path, plat)
    if retagged is None and reason is None:
        return _retag_linux_armv7_wheel_to_plat(wheel_path, plat)
    return retagged, reason


def _retag_failure_message(wheel: Path, retagged: Path | None, plat: str) -> str:
    if retagged is not None and not _armv7_forced_plat_filename_ok(retagged.name, plat):
        return f"Retagged wheel filename does not match forced {_forced_plat_phrase(plat, plat)}: {retagged.name}"
    return f"Failed to retag linux_armv7l wheel to {plat!r}: {wheel.name}"


def _discard_failed_retag(wheel: Path, retagged: Path | None, plat: str, errors: list[str]) -> None:
    message = _retag_failure_message(wheel, retagged, plat)
    print_color(f"  -> ERROR: {message}", Fore.RED)
    errors.append(message)
    if retagged is not None:
        retagged.unlink(missing_ok=True)
    wheel.unlink(missing_ok=True)


def _take_retagged_armv7_wheel(wheel: Path, plat: str, errors: list[str]) -> str:
    """Publish, delete, or record an error. Returns ``repaired``, ``deleted``, or ``error``."""
    retagged, reason = _retag_with_one_retry(wheel, plat)
    if reason:
        print_color(f"  -> Deleting {wheel.name} ({reason})", Fore.RED)
        wheel.unlink(missing_ok=True)
        return "deleted"
    if retagged is not None and _armv7_forced_plat_filename_ok(retagged.name, plat):
        print_color(f"  -> Retagged linux_armv7l to {plat}: {retagged.name}", Fore.GREEN)
        return "repaired"
    _discard_failed_retag(wheel, retagged, plat, errors)
    return "error"


# External OpenSSL 3 is not in the manylinux baseline before glibc 2.35.
# Bullseye (``manylinux_2_31``) ships OpenSSL 1.1 only; Bookworm (``2_36``) ships OpenSSL 3.
_OPENSSL3_SONAMES = frozenset({"libcrypto.so.3", "libssl.so.3"})
_OPENSSL3_EXTERNAL_MAX_GLIBC = (2, 34)
_GLIBC_VERSION_NAME = re.compile(r"GLIBC_(\d+)\.(\d+)$")
_DT_NULL = 0
_DT_NEEDED = 1
_DT_STRTAB = 5
_DT_VERNEED = 0x6FFFFFFE
_DT_VERNEEDNUM = 0x6FFFFFFF


class _ElfView(NamedTuple):
    phoff: int
    phentsize: int
    phnum: int
    is32: bool


def _looks_like_elf(blob: bytes) -> bool:
    return len(blob) >= 4 and blob[:4] == b"\x7fELF"


def _require_elf(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _elf32_view(blob: bytes) -> _ElfView:
    _require_elf(len(blob) >= 52, "could not read ELF dynamic segment")
    (
        _e_type,
        _e_machine,
        _e_version,
        _e_entry,
        e_phoff,
        _e_shoff,
        _e_flags,
        _e_ehsize,
        e_phentsize,
        e_phnum,
        _e_shentsize,
    ) = struct.unpack_from("<HHIIIIIHHHH", blob, 16)
    return _ElfView(e_phoff, e_phentsize, e_phnum, True)


def _elf64_view(blob: bytes) -> _ElfView:
    _require_elf(len(blob) >= 64, "could not read ELF dynamic segment")
    (
        _e_type,
        _e_machine,
        _e_version,
        _e_entry,
        e_phoff,
        _e_shoff,
        _e_flags,
        _e_ehsize,
        e_phentsize,
        e_phnum,
        _e_shentsize,
    ) = struct.unpack_from("<HHIQQQIHHHH", blob, 16)
    return _ElfView(e_phoff, e_phentsize, e_phnum, False)


def _elf_view(blob: bytes) -> _ElfView:
    _require_elf(len(blob) >= 6 and blob[5] == 1, "could not read ELF dynamic segment")
    if blob[4] == 1:
        return _elf32_view(blob)
    if blob[4] == 2:
        return _elf64_view(blob)
    raise ValueError("could not read ELF dynamic segment")


def _phdr_fields(blob: bytes, offset: int, is32: bool) -> tuple[int, int, int, int]:
    if is32:
        _require_elf(offset + 32 <= len(blob), "could not read ELF dynamic segment")
        p_type, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _flags, _align = struct.unpack_from(
            "<IIIIIIII", blob, offset
        )
        return p_type, p_offset, p_vaddr, p_filesz
    _require_elf(offset + 56 <= len(blob), "could not read ELF dynamic segment")
    p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _align = struct.unpack_from("<IIQQQQQQ", blob, offset)
    return p_type, p_offset, p_vaddr, p_filesz


def _program_segments(blob: bytes, view: _ElfView) -> tuple[list[tuple[int, int, int]], tuple[int, int] | None]:
    usable = 0 < view.phnum <= 128 and view.phentsize >= 32 and view.phoff >= 0
    _require_elf(usable, "could not read ELF dynamic segment")
    loads: list[tuple[int, int, int]] = []
    dynamic: tuple[int, int] | None = None
    for index in range(view.phnum):
        p_type, p_offset, p_vaddr, p_filesz = _phdr_fields(blob, view.phoff + index * view.phentsize, view.is32)
        if p_type == 1:
            loads.append((p_offset, p_vaddr, p_filesz))
        elif p_type == 2:
            dynamic = (p_offset, p_filesz)
    return loads, dynamic


def _vaddr_to_offset(loads: list[tuple[int, int, int]], vaddr: int) -> int | None:
    for p_offset, p_vaddr, p_filesz in loads:
        if p_vaddr <= vaddr < p_vaddr + p_filesz:
            return p_offset + (vaddr - p_vaddr)
    return None


def _record_dynamic_tag(tag: int, val: int, needed_offs: list[int], tables: dict[str, int]) -> bool:
    """Return True when the dynamic table ends."""
    if tag == _DT_NULL:
        return True
    if tag == _DT_NEEDED:
        needed_offs.append(val)
        return False
    name = {_DT_STRTAB: "strtab", _DT_VERNEED: "verneed", _DT_VERNEEDNUM: "count"}.get(tag)
    if name is not None:
        tables[name] = val
    return False


def _dynamic_table(
    blob: bytes, dynamic_off: int, dynamic_filesz: int, is32: bool
) -> tuple[list[int], int | None, int | None, int]:
    dyn_fmt = "<II" if is32 else "<QQ"
    dyn_size = 8 if is32 else 16
    needed_offs: list[int] = []
    tables: dict[str, int] = {}
    pos = dynamic_off
    end = min(len(blob), dynamic_off + dynamic_filesz)
    while pos + dyn_size <= end:
        tag, val = struct.unpack_from(dyn_fmt, blob, pos)
        pos += dyn_size
        if _record_dynamic_tag(tag, val, needed_offs, tables):
            break
    return needed_offs, tables.get("strtab"), tables.get("verneed"), tables.get("count", 0)


def _cstr(blob: bytes, strtab_off: int, rel: int) -> str:
    start = strtab_off + rel
    if start < 0 or start >= len(blob):
        return ""
    end = blob.find(b"\x00", start)
    if end < 0:
        return ""
    return blob[start:end].decode("ascii", "replace")


def _needed_names(blob: bytes, strtab_off: int, needed_offs: list[int]) -> set[str]:
    return {name for name in (_cstr(blob, strtab_off, off) for off in needed_offs) if name}


def _remember_glibc(blob: bytes, strtab_off: int, vna_name: int, found: set[tuple[int, int]]) -> None:
    match = _GLIBC_VERSION_NAME.match(_cstr(blob, strtab_off, vna_name))
    if not match:
        return
    version = (int(match.group(1)), int(match.group(2)))
    if version[0] == 2 and version[1] <= 80:
        found.add(version)


def _glibc_aux(blob: bytes, strtab_off: int, aux: int, count: int) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for _aux_i in range(min(int(count), 64)):
        if aux < 0 or aux + 16 > len(blob):
            break
        _vna_hash, _vna_flags, _vna_other, vna_name, vna_next = struct.unpack_from("<IHHII", blob, aux)
        _remember_glibc(blob, strtab_off, vna_name, found)
        if vna_next == 0:
            break
        aux += vna_next
    return found


def _glibc_versions(blob: bytes, strtab_off: int, verneed_off: int, verneed_num: int) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    vn_off = verneed_off
    for _entry in range(min(int(verneed_num), 64)):
        if vn_off < 0 or vn_off + 16 > len(blob):
            break
        _vn_version, vn_cnt, _vn_file, vn_aux, vn_next = struct.unpack_from("<HHIII", blob, vn_off)
        found.update(_glibc_aux(blob, strtab_off, vn_off + vn_aux, vn_cnt))
        if vn_next == 0:
            break
        vn_off += vn_next
    return found


def _glibc_from_verneed(
    blob: bytes,
    loads: list[tuple[int, int, int]],
    strtab_off: int,
    verneed_vaddr: int | None,
    verneed_num: int,
) -> set[tuple[int, int]]:
    if verneed_vaddr is None or not verneed_num:
        return set()
    verneed_off = _vaddr_to_offset(loads, verneed_vaddr)
    _require_elf(verneed_off is not None, "could not read ELF dynamic segment")
    assert verneed_off is not None
    return _glibc_versions(blob, strtab_off, verneed_off, verneed_num)


def _parse_elf_dynamic(blob: bytes) -> tuple[set[str], set[tuple[int, int]]]:
    view = _elf_view(blob)
    loads, dynamic = _program_segments(blob, view)
    if dynamic is None or dynamic[1] <= 0:
        return set(), set()
    needed_offs, strtab_vaddr, verneed_vaddr, verneed_num = _dynamic_table(blob, dynamic[0], dynamic[1], view.is32)
    _require_elf(strtab_vaddr is not None, "could not read ELF dynamic segment")
    assert strtab_vaddr is not None
    strtab_off = _vaddr_to_offset(loads, strtab_vaddr)
    _require_elf(strtab_off is not None, "could not read ELF dynamic segment")
    assert strtab_off is not None
    needed = _needed_names(blob, strtab_off, needed_offs)
    glibc = _glibc_from_verneed(blob, loads, strtab_off, verneed_vaddr, verneed_num)
    return needed, glibc


def _elf_needed_and_glibc(blob: bytes) -> tuple[set[str], set[tuple[int, int]]] | None:
    """Return ``DT_NEEDED`` sonames and required ``GLIBC_M.N`` versions.

    ``None`` when ``blob`` is not ELF. ``ValueError`` when it is ELF but the dynamic
    segment cannot be read. An ELF with no dynamic segment returns empty sets.
    """
    if not _looks_like_elf(blob):
        return None
    return _parse_elf_dynamic(blob)


def _glibc_too_new_reason(glibc: set[tuple[int, int]], plat_glibc: tuple[int, int], plat: str) -> str | None:
    too_new = {ver for ver in glibc if ver > plat_glibc}
    if not too_new:
        return None
    major, minor = max(too_new)
    return f"needs glibc {major}.{minor}, {plat} allows {plat_glibc[0]}.{plat_glibc[1]}"


def _openssl3_reason(needed: set[str], bundled: set[str], plat_glibc: tuple[int, int], plat: str) -> str | None:
    if plat_glibc > _OPENSSL3_EXTERNAL_MAX_GLIBC:
        return None
    external = sorted(name for name in needed if name in _OPENSSL3_SONAMES and name not in bundled)
    if not external:
        return None
    libs = ", ".join(external)
    return f"links unpackaged {libs} (absent from the {plat} baseline)"


def _armv7_plat_violation(
    needed: set[str],
    glibc: set[tuple[int, int]],
    bundled: set[str],
    plat: str,
) -> str | None:
    """Why these dependencies must not be published as ``plat``, or None.

    Piwheels ``linux_armv7l`` wheels are retagged to the repair image's manylinux
    tag without relinking. That tag is a glibc/OpenSSL promise: Bullseye is glibc
    2.31 and OpenSSL 1.1. A binary that needs glibc 2.34 or unpackaged
    ``libssl.so.3`` / ``libcrypto.so.3`` is the Bookworm build under a Bullseye name.
    """
    plat_tags = manylinux_glibc_tags_in_name(plat)
    if not plat_tags:
        return None
    plat_glibc = max(plat_tags)
    reasons = [
        reason
        for reason in (
            _glibc_too_new_reason(glibc, plat_glibc, plat),
            _openssl3_reason(needed, bundled, plat_glibc, plat),
        )
        if reason
    ]
    if not reasons:
        return None
    return "; ".join(reasons)


def _prune_manylinux_armv7_when_linux_tag_present(wheels_dir: Path) -> int:
    """Remove manylinux armv7l wheels when a linux_armv7l sibling exists (pip prefers manylinux)."""
    removed = 0
    by_dist_version: dict[tuple[str, str], list[Path]] = {}
    for path in wheels_dir.rglob("*.whl"):
        if "armv7l" not in path.name.lower():
            continue
        parsed = parse_wheel_name(path.name)
        if not parsed:
            continue
        by_dist_version.setdefault((parsed[0], parsed[1]), []).append(path)

    for paths in by_dist_version.values():
        linux_paths = [p for p in paths if _is_linux_tag_wheel(p.name)]
        manylinux_paths = [p for p in paths if "manylinux" in p.name.lower() and "armv7l" in p.name.lower()]
        if not linux_paths or not manylinux_paths:
            continue
        for path in manylinux_paths:
            print_color(
                f"Removing manylinux armv7l wheel (linux_armv7l kept; pip would prefer manylinux): {path.name}",
                Fore.YELLOW,
            )
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _armv7_forced_plat_filename_ok(wheel_name: str, plat: str) -> bool:
    """True if ``wheel_name`` matches ``AUDITWHEEL_PLAT`` for ARMv7 / ARMv7 Legacy splits."""
    wn = wheel_name.lower()
    if _is_linux_tag_wheel(wn):
        if not plat:
            return _allow_linux_tag_env_enabled()
        return False
    return armv7_wheel_matches_forced_plat(
        wheel_name,
        plat,
        only_plat=_only_plat_env_enabled(),
        repair=True,
    )


def repair_wheel_windows(wheel_path: Path, temp_dir: Path) -> subprocess.CompletedProcess[str]:
    """Repair Windows wheel using delvewheel."""
    result = subprocess.run(
        ["delvewheel", "repair", str(wheel_path), "-w", str(temp_dir), "--no-mangle-all"],
        capture_output=True,
        text=True,
    )
    return result


def fix_universal2_wheel_name(wheel_path: Path, error_msg: str) -> Union[Path, str, None]:
    """Fix incorrectly tagged universal2 wheel by renaming to actual architecture.

    Returns:
        - New wheel path if successfully renamed
        - None if not a fixable case
        - "delete" if wheel is corrupted (missing all architectures) and should be deleted
    """
    if (
        "universal2" not in str(wheel_path)
        or "Failed to find any binary with the required architecture" not in error_msg
    ):
        return None

    # Parse which architecture is missing from the error
    # "Failed to find any binary with the required architecture: 'x86_64'"
    # means the wheel only has arm64
    if "'arm64,x86_64'" in error_msg or "'x86_64,arm64'" in error_msg:
        # Missing BOTH architectures - wheel is corrupted, delete it
        print_color("  -> Deleting corrupted wheel (missing native binaries for all architectures)", Fore.RED)
        wheel_path.unlink(missing_ok=True)
        return "delete"
    elif "'x86_64'" in error_msg:
        # Missing x86_64, so it only has arm64
        actual_arch = "arm64"
    elif "'arm64'" in error_msg:
        # Missing arm64, so it only has x86_64
        actual_arch = "x86_64"
    else:
        return None

    # Rename the wheel
    new_name = str(wheel_path.name).replace("_universal2.whl", f"_{actual_arch}.whl")
    new_path = wheel_path.parent / new_name

    print(f"  -> Renaming: {wheel_path.name} -> {new_name}")
    wheel_path.rename(new_path)

    return new_path


def repair_wheel_macos(wheel_path: Path, temp_dir: Path) -> subprocess.CompletedProcess[str]:
    """Repair macOS wheel using delocate."""
    cmd = ["delocate-wheel", "-w", str(temp_dir), "-v", str(wheel_path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def _forced_plat_phrase(wheel_plat: str, plat_env: str) -> str:
    """Describe the tag the filename check required, and the container tag when they differ."""
    if not plat_env or wheel_plat == plat_env:
        return f"AUDITWHEEL_PLAT={wheel_plat!r}"
    return f"build-image plat {wheel_plat!r} (repair container AUDITWHEEL_PLAT={plat_env!r})"


def repair_wheel_linux(
    wheel_path: Path,
    temp_dir: Path,
    plat: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Repair Linux wheel using auditwheel.

    Uses --strip option to strip debugging symbols which can help with
    ELF alignment issues on ARM (fixes "ELF load command address/offset not properly aligned" errors).

    ``plat`` is the tag the filename check will require. When omitted, ``AUDITWHEEL_PLAT``
    is used. Passing it explicitly keeps ``auditwheel repair --plat`` and that check on one value.
    """
    if plat is None:
        plat = os.environ.get("AUDITWHEEL_PLAT", "").strip()
    else:
        plat = plat.strip()
    only_plat = os.environ.get("AUDITWHEEL_ONLY_PLAT", "").strip().lower() in ("1", "true", "yes")

    cmd = ["auditwheel", "repair", str(wheel_path), "-w", str(temp_dir), "--strip"]
    if plat:
        cmd.extend(["--plat", plat])
    if only_plat:
        cmd.append("--only-plat")

    result = subprocess.run(cmd, capture_output=True, text=True)

    # Older auditwheel versions may not support --only-plat. If requested, retry once without it.
    combined_err = (result.stderr or "") + (result.stdout or "")
    if only_plat and result.returncode != 0 and "unrecognized arguments: --only-plat" in combined_err:
        cmd_no_only = [c for c in cmd if c != "--only-plat"]
        result = subprocess.run(cmd_no_only, capture_output=True, text=True)

    return result


@dataclass
class _RepairStats:
    repaired: int = 0
    skipped: int = 0
    deleted: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)

    def skip(self, message: str, color: str | None = None) -> None:
        if color is None:
            print_color(message)
        else:
            print_color(message, color)
        self.skipped += 1

    def delete(self, message: str, wheel: Path) -> None:
        print_color(message, Fore.RED)
        wheel.unlink(missing_ok=True)
        self.deleted += 1

    def fail(self, message: str, stored: str) -> None:
        print_color(f"  -> ERROR: {message}", Fore.RED)
        self.errors.append(stored)
        self.error_count += 1


def _report_no_wheels(wheels_dir: Path) -> None:
    print_color(f"No wheels found in {wheels_dir} - nothing to repair", Fore.YELLOW)
    print("Exiting successfully (no wheels to process)")


def _prune_host_mirrors(wheels_dir: Path, platform_name: str, arch: str, wheels: list[Path]) -> list[Path]:
    if platform_name == "Linux" and arch in ("x86_64", "aarch64"):
        pruned = prune_ci_manylinux_newer_than_228_when_228_mirror_present(
            wheel_dir=wheels_dir,
            package_names=_force_no_binary_linux_normalized(_REPO_ROOT),
        )
        if pruned:
            print_color(
                f"Pre-repair: pruned {pruned} manylinux wheel(s) newer than 2_28 (PyPI 2_28 mirror kept)",
                Fore.YELLOW,
            )
            return _dedupe_wheel_paths(wheels_dir)
    if platform_name == "Darwin":
        pruned = prune_ci_macos_newer_than_pypi_mirror(wheel_dir=wheels_dir)
        if pruned:
            print_color(
                f"Pre-repair: pruned {pruned} macOS wheel(s) newer than the PyPI macosx tag",
                Fore.YELLOW,
            )
            return _dedupe_wheel_paths(wheels_dir)
    return wheels


def _prune_linux_mirrors_after(wheels_dir: Path, platform_name: str, arch: str) -> None:
    if platform_name != "Linux" or arch not in ("x86_64", "aarch64"):
        return
    pruned = prune_ci_manylinux_newer_than_228_when_228_mirror_present(
        wheel_dir=wheels_dir,
        package_names=_force_no_binary_linux_normalized(_REPO_ROOT),
    )
    if pruned:
        print_color(
            f"Pruned {pruned} manylinux wheel(s) newer than 2_28 (PyPI 2_28 mirror kept)",
            Fore.YELLOW,
        )


def _prune_macos_after(wheels_dir: Path, platform_name: str) -> None:
    if platform_name != "Darwin":
        return
    pruned = prune_ci_macos_newer_than_pypi_mirror(wheel_dir=wheels_dir)
    if pruned:
        print_color(
            f"Pruned {pruned} macOS wheel(s) newer than the PyPI macosx tag",
            Fore.YELLOW,
        )


def _prune_armv7_linux_tags_after(wheels_dir: Path, platform_name: str, arch: str) -> None:
    if platform_name != "Linux" or arch != "armv7l":
        return
    pruned = _prune_manylinux_armv7_when_linux_tag_present(wheels_dir)
    if pruned:
        print_color(f"Pruned {pruned} manylinux armv7l wheel(s) superseded by linux_armv7l", Fore.YELLOW)


def _prune_after_repair(wheels_dir: Path, platform_name: str, arch: str) -> None:
    _prune_linux_mirrors_after(wheels_dir, platform_name, arch)
    _prune_macos_after(wheels_dir, platform_name)
    _prune_armv7_linux_tags_after(wheels_dir, platform_name, arch)


def _unrelated_wheel_reason(name: str, platform_name: str, arch: str) -> str | None:
    if is_pure_python_wheel(name):
        return "Skipping pure Python wheel"
    if platform_name == "Windows" and name.startswith("pywin32"):
        return "Skipping pywin32 wheel (DLLs are internal)"
    if not is_platform_wheel(name, platform_name, arch):
        return f"Skipping (not a {platform_name} wheel)"
    if platform_name != "Linux":
        return None
    wheel_arch = get_wheel_arch(name)
    if wheel_arch and wheel_arch != arch:
        return f"Skipping incompatible architecture ({wheel_arch} wheel on {arch} platform)"
    return None


def _skip_unrelated_wheel(wheel: Path, platform_name: str, arch: str, stats: _RepairStats) -> bool:
    reason = _unrelated_wheel_reason(wheel.name, platform_name, arch)
    if reason is None:
        return False
    stats.skip(f"  -> {reason}")
    return True


def _finish_armv7_linux_tag(wheel: Path, plat_env: str, platform_name: str, arch: str, stats: _RepairStats) -> bool:
    if platform_name != "Linux" or arch != "armv7l" or not plat_env or not _is_linux_tag_wheel(wheel.name):
        return False
    outcome = _take_retagged_armv7_wheel(wheel, plat_env, stats.errors)
    if outcome == "deleted":
        stats.deleted += 1
    elif outcome == "repaired":
        stats.repaired += 1
    else:
        stats.error_count += 1
    return True


def _repair_tool_skip_reason(name: str, platform_name: str, arch: str) -> str | None:
    if _should_skip_armv7_auditwheel(name, arch):
        return "Skipping auditwheel (linux_armv7l; keep piwheels/local wheel on ARMv7)"
    if platform_name == "Linux" and should_skip_linux_auditwheel_for_pypi_mirror(name):
        return "Skipping auditwheel (PyPI manylinux_2_28 mirror; repair would retag to host 2_34)"
    if platform_name == "Darwin" and should_skip_macos_delocate_for_pypi_mirror(name):
        return "Skipping delocate (keep PyPI macOS tag; local retag can steal pip preference)"
    return None


def _clear_temp_wheels(temp_dir: Path) -> None:
    for old_wheel in temp_dir.glob("*.whl"):
        old_wheel.unlink()


def _run_repair_tool(
    wheel: Path,
    temp_dir: Path,
    platform_name: str,
    plat_env: str,
) -> subprocess.CompletedProcess[str] | None:
    if platform_name == "Windows":
        return repair_wheel_windows(wheel, temp_dir)
    if platform_name == "Darwin":
        return repair_wheel_macos(wheel, temp_dir)
    if platform_name == "Linux":
        return repair_wheel_linux(wheel, temp_dir, plat=plat_env)
    return None


def _repair_output_text(result: subprocess.CompletedProcess[str]) -> str:
    return ((result.stderr or "") + "\n" + (result.stdout or "")).strip()


def _print_repair_output(result: subprocess.CompletedProcess[str]) -> None:
    if result.stdout:
        print(f"  {result.stdout.strip()}")
    if result.stderr:
        print_color(f"  {result.stderr.strip()}", Fore.RED)


def _delete_corrupt_zip(wheel: Path, temp_dir: Path, stats: _RepairStats) -> None:
    print_color("  -> Deleting file (repair tool reported corrupt zip archive)", Fore.RED)
    _clear_temp_wheels(temp_dir)
    wheel.unlink(missing_ok=True)
    stats.deleted += 1


def _needs_universal2_fix(platform_name: str, wheel_name: str, error_msg: str) -> bool:
    if platform_name != "Darwin" or "universal2" not in wheel_name:
        return False
    return "Failed to find any binary with the required architecture" in error_msg


def _maybe_retry_universal2(
    wheel: Path,
    temp_dir: Path,
    platform_name: str,
    error_msg: str,
    result: subprocess.CompletedProcess[str],
) -> tuple[Path, subprocess.CompletedProcess[str], str, bool]:
    if not _needs_universal2_fix(platform_name, wheel.name, error_msg):
        return wheel, result, error_msg, False
    renamed = fix_universal2_wheel_name(wheel, error_msg)
    if renamed == "delete":
        return wheel, result, error_msg, True
    if not renamed:
        return wheel, result, error_msg, False
    _clear_temp_wheels(temp_dir)
    print_color("  -> Retrying delocate with corrected wheel name", Fore.CYAN)
    retried = repair_wheel_macos(Path(renamed), temp_dir)
    _print_repair_output(retried)
    return Path(renamed), retried, _repair_output_text(retried), False


def _finish_not_platform_wheel(
    error_msg: str,
    wheel: Path,
    platform_name: str,
    arch: str,
    stats: _RepairStats,
) -> bool:
    if platform_name != "Linux" or "This does not look like a platform wheel, no ELF executable" not in error_msg:
        return False
    if arch == "armv7l":
        stats.delete("  -> Deleting corrupted wheel", wheel)
    else:
        stats.skip(
            "  -> Keeping original wheel (manylinux tag but no native extensions; auditwheel N/A)",
            Fore.YELLOW,
        )
    return True


def _finish_armv7_too_recent(
    error_msg: str,
    wheel: Path,
    platform_name: str,
    arch: str,
    plat_env: str,
    stats: _RepairStats,
) -> bool:
    if platform_name != "Linux" or arch != "armv7l" or not plat_env:
        return False
    if "too-recent versioned symbols" not in error_msg:
        return False
    print_color(
        f"  -> Deleting wheel (auditwheel: too-recent versioned symbols for {plat_env}; not publishing that tag)",
        Fore.RED,
    )
    wheel.unlink(missing_ok=True)
    stats.deleted += 1
    return True


def _noncritical_too_recent(error_msg: str, wheel_name: str, plat_env: str, platform_name: str, arch: str) -> bool:
    return "too-recent versioned symbols" in error_msg


def _noncritical_already_bundled(error_msg: str, wheel_name: str, plat_env: str, platform_name: str, arch: str) -> bool:
    # A manylinux wheel that cannot find its libraries was already repaired.
    return "manylinux" in wheel_name and "could not be located" in error_msg and not plat_env


def _noncritical_hashed_vendor(error_msg: str, wheel_name: str, plat_env: str, platform_name: str, arch: str) -> bool:
    # Hashed vendored DT_NEEDED names (for example libbrotlicommon-<hash>.so.1) are kept on
    # x86_64/aarch64. ARMv7 uses the forced-plat policy instead.
    if platform_name != "Linux" or arch not in ("x86_64", "aarch64"):
        return False
    if "manylinux" not in wheel_name:
        return False
    return "Cannot repair wheel, because required library" in error_msg and "could not be located" in error_msg


def _noncritical_linux_tag_libs(error_msg: str, wheel_name: str, plat_env: str, platform_name: str, arch: str) -> bool:
    if plat_env or not _allow_linux_tag_env_enabled() or not _is_linux_tag_wheel(wheel_name):
        return False
    return "Cannot repair wheel, because required library" in error_msg or "could not be located" in error_msg


def _noncritical_armv7_libc(error_msg: str, wheel_name: str, plat_env: str, platform_name: str, arch: str) -> bool:
    # QEMU often makes auditwheel fail libc detection. With AUDITWHEEL_PLAT set, that skip
    # would leave identical filenames across ARMv7 and ARMv7 Legacy, so it is not non-critical.
    if platform_name != "Linux" or arch != "armv7l" or plat_env:
        return False
    return "InvalidLibc" in error_msg or "couldn't detect libc" in error_msg


def _repair_error_is_noncritical(error_msg: str, wheel_name: str, plat_env: str, platform_name: str, arch: str) -> bool:
    checks = (
        _noncritical_too_recent,
        _noncritical_already_bundled,
        _noncritical_hashed_vendor,
        _noncritical_linux_tag_libs,
        _noncritical_armv7_libc,
    )
    return any(check(error_msg, wheel_name, plat_env, platform_name, arch) for check in checks)


def _has_hard_repair_error(error_msg: str) -> bool:
    markers = (
        "ValueError:",
        "FileNotFoundError:",
        "Cannot repair wheel",
        "could not be located",
        "DelocationError:",
    )
    return any(marker in error_msg for marker in markers)


def _announce_armv7_libc(error_msg: str, plat_env: str, platform_name: str, arch: str) -> None:
    if platform_name != "Linux" or arch != "armv7l" or plat_env:
        return
    if "InvalidLibc" not in error_msg and "couldn't detect libc" not in error_msg:
        return
    print_color(
        "  -> Keeping original wheel (auditwheel libc detection failed on ARMv7 runner; often QEMU)",
        Fore.YELLOW,
    )


def _announce_noncritical(error_msg: str, wheel_name: str, plat_env: str, platform_name: str, arch: str) -> None:
    if "too-recent versioned symbols" in error_msg:
        print_color("  -> Keeping original wheel (build issue: needs older toolchain)", Fore.YELLOW)
        return
    if "manylinux" in wheel_name and "could not be located" in error_msg:
        print_color("  -> Keeping original wheel (already bundled from PyPI)", Fore.GREEN)
        return
    if plat_env and _allow_linux_tag_env_enabled() and _is_linux_tag_wheel(wheel_name):
        print_color(
            "  -> Keeping original wheel (linux-tag wheel; not forcing manylinux under current policy)",
            Fore.YELLOW,
        )
        return
    _announce_armv7_libc(error_msg, plat_env, platform_name, arch)


def _armv7_filename_must_match(name: str, plat_env: str, platform_name: str, arch: str, wheel_plat: str) -> bool:
    if not plat_env or platform_name != "Linux" or arch != "armv7l":
        return False
    if _armv7_forced_plat_filename_ok(name, wheel_plat):
        return False
    if _allow_linux_tag_env_enabled() and _is_linux_tag_wheel(name):
        return False
    return True


def _reject_forced_filename(
    wheel: Path,
    plat_env: str,
    platform_name: str,
    arch: str,
    wheel_plat: str,
    message: str,
    stats: _RepairStats,
) -> bool:
    if not _armv7_filename_must_match(wheel.name, plat_env, platform_name, arch, wheel_plat):
        return False
    stats.fail(message, f"{wheel.name}: {message}")
    wheel.unlink(missing_ok=True)
    return True


def _keep_noncritical_wheel(
    wheel: Path,
    error_msg: str,
    plat_env: str,
    platform_name: str,
    arch: str,
    wheel_plat: str,
    stats: _RepairStats,
) -> None:
    _announce_noncritical(error_msg, wheel.name, plat_env, platform_name, arch)
    message = (
        "Wheel filename does not match forced "
        f"{_forced_plat_phrase(wheel_plat, plat_env)} "
        f"after non-fatal repair path: {wheel.name}"
    )
    if _reject_forced_filename(wheel, plat_env, platform_name, arch, wheel_plat, message, stats):
        return
    stats.skipped += 1


def _fail_hard_repair(wheel: Path, temp_dir: Path, error_msg: str, stats: _RepairStats) -> None:
    _clear_temp_wheels(temp_dir)
    stats.fail(error_msg, f"{wheel.name}: {error_msg}")


def _install_repaired_wheel(wheel: Path, repaired: Path) -> Path:
    if repaired.name != wheel.name:
        wheel.unlink(missing_ok=True)
        final_path = wheel.parent / repaired.name
        repaired.rename(final_path)
        print_color(f"  -> Replaced with repaired wheel: {repaired.name}", Fore.GREEN)
        return final_path
    wheel.unlink(missing_ok=True)
    repaired.rename(wheel)
    print_color(f"  -> Repaired successfully: {repaired.name}", Fore.GREEN)
    return wheel


def _accept_installed_wheel(
    final_path: Path,
    plat_env: str,
    platform_name: str,
    arch: str,
    wheel_plat: str,
    stats: _RepairStats,
) -> None:
    if not wheel_archive_is_readable(final_path):
        print_color("  -> Deleting repaired output (not a valid / readable zip archive)", Fore.RED)
        final_path.unlink(missing_ok=True)
        stats.deleted += 1
        return
    message = (
        f"Repaired wheel filename does not match forced {_forced_plat_phrase(wheel_plat, plat_env)}: {final_path.name}"
    )
    if _reject_forced_filename(final_path, plat_env, platform_name, arch, wheel_plat, message, stats):
        return
    stats.repaired += 1


def _keep_already_compatible(
    wheel: Path,
    plat_env: str,
    platform_name: str,
    arch: str,
    wheel_plat: str,
    stats: _RepairStats,
) -> None:
    message = (
        "auditwheel reported success but left the wheel unchanged with a filename "
        f"that does not match {_forced_plat_phrase(wheel_plat, plat_env)}: {wheel.name}"
    )
    if _reject_forced_filename(wheel, plat_env, platform_name, arch, wheel_plat, message, stats):
        return
    print_color("  -> Keeping original wheel (already compatible)", Fore.GREEN)
    stats.skipped += 1


def _store_repaired_or_original(
    wheel: Path,
    temp_dir: Path,
    result: subprocess.CompletedProcess[str],
    error_msg: str,
    plat_env: str,
    platform_name: str,
    arch: str,
    wheel_plat: str,
    stats: _RepairStats,
) -> None:
    repaired = next(temp_dir.glob("*.whl"), None)
    if repaired is not None:
        final_path = _install_repaired_wheel(wheel, repaired)
        _accept_installed_wheel(final_path, plat_env, platform_name, arch, wheel_plat, stats)
        return
    if result.returncode == 0:
        _keep_already_compatible(wheel, plat_env, platform_name, arch, wheel_plat, stats)
        return
    stats.fail(error_msg, f"{wheel.name}: {error_msg}")


def _apply_repair_result(
    wheel: Path,
    temp_dir: Path,
    result: subprocess.CompletedProcess[str],
    platform_name: str,
    arch: str,
    plat_env: str,
    stats: _RepairStats,
) -> None:
    _print_repair_output(result)
    error_msg = _repair_output_text(result)
    if _stderr_indicates_bad_zip(error_msg):
        _delete_corrupt_zip(wheel, temp_dir, stats)
        return
    wheel, result, error_msg, deleted = _maybe_retry_universal2(wheel, temp_dir, platform_name, error_msg, result)
    if deleted:
        stats.deleted += 1
        return
    if _finish_not_platform_wheel(error_msg, wheel, platform_name, arch, stats):
        return
    if _finish_armv7_too_recent(error_msg, wheel, platform_name, arch, plat_env, stats):
        return
    if _repair_error_is_noncritical(error_msg, wheel.name, plat_env, platform_name, arch):
        _keep_noncritical_wheel(wheel, error_msg, plat_env, platform_name, arch, plat_env, stats)
        return
    if _has_hard_repair_error(error_msg):
        _fail_hard_repair(wheel, temp_dir, error_msg, stats)
        return
    _store_repaired_or_original(
        wheel,
        temp_dir,
        result,
        error_msg,
        plat_env,
        platform_name,
        arch,
        plat_env,
        stats,
    )


def _repair_one_wheel(wheel: Path, temp_dir: Path, platform_name: str, arch: str, stats: _RepairStats) -> None:
    print(f"Processing: {wheel.name}")
    if _skip_unrelated_wheel(wheel, platform_name, arch, stats):
        return
    if not wheel_archive_is_readable(wheel):
        stats.delete("  -> Deleting file (not a valid / readable zip wheel archive)", wheel)
        return
    plat_env = os.environ.get("AUDITWHEEL_PLAT", "").strip()
    if _finish_armv7_linux_tag(wheel, plat_env, platform_name, arch, stats):
        return
    skip_reason = _repair_tool_skip_reason(wheel.name, platform_name, arch)
    if skip_reason:
        stats.skip(f"  -> {skip_reason}", Fore.YELLOW)
        return
    _clear_temp_wheels(temp_dir)
    result = _run_repair_tool(wheel, temp_dir, platform_name, plat_env)
    if result is None:
        print_color(f"  -> ERROR: Unsupported platform {platform_name}", Fore.RED)
        stats.error_count += 1
        return
    _apply_repair_result(wheel, temp_dir, result, platform_name, arch, plat_env, stats)


def _report_repair_stats(stats: _RepairStats, total: int) -> None:
    print_color("---------- STATISTICS ----------")
    print_color(f"Total wheels: {total}")
    print_color(f"Deleted wheels: {stats.deleted}", Fore.RED)
    print_color(f"Kept wheels: {stats.skipped}")
    print_color(f"Repaired wheels: {stats.repaired}", Fore.GREEN)
    print_color(f"Errors: {stats.error_count}", Fore.RED)
    if not stats.errors:
        print("All wheels processed successfully")
        return
    print_color("---------- ERRORS ----------", Fore.RED)
    for index, error in enumerate(stats.errors, start=1):
        print_color(f"{index}. {error}", Fore.RED)
    raise SystemExit("One or more wheels failed to repair")


def main() -> None:
    wheels_dir = Path("./downloaded_wheels")
    temp_dir = Path("./temp_repair")
    temp_dir.mkdir(exist_ok=True)
    wheels = _dedupe_wheel_paths(wheels_dir)
    if not wheels:
        _report_no_wheels(wheels_dir)
        return
    print_color(f"Found {len(wheels)} wheels\n")
    platform_name = get_platform()
    arch = platform.machine()
    wheels = _prune_host_mirrors(wheels_dir, platform_name, arch, wheels)
    stats = _RepairStats()
    for wheel in wheels:
        _repair_one_wheel(wheel, temp_dir, platform_name, arch, stats)
    _prune_after_repair(wheels_dir, platform_name, arch)
    _report_repair_stats(stats, len(wheels))


if __name__ == "__main__":
    main()
