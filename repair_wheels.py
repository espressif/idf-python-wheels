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

from pathlib import Path
from typing import List
from typing import Optional
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


def _retag_linux_armv7_wheel_to_plat(wheel_path: Path, plat: str) -> Optional[Path]:
    """Retag a piwheels ``linux_armv7l`` wheel to ``plat`` without auditwheel relink.

    Current and legacy builds both produce ``*-linux_armv7l.whl``. The platform tag
    is the repair container: Bookworm ``manylinux_2_36`` for current, Bullseye
    ``manylinux_2_31`` for legacy. The caller drops the retagged wheel when its ELF
    needs a newer glibc than ``plat``, or still links unpackaged OpenSSL 3 on the
    Bullseye tag.

    Writes to a container-local staging file first (many-entry wheels such as pycryptodome
    can fail when rebuilt directly on the QEMU bind-mounted ``downloaded_wheels`` tree).
    """
    if not _is_linux_tag_wheel(wheel_path.name):
        return None
    if not wheel_path.name.lower().endswith(_LINUX_ARMV7L_WHL_SUFFIX):
        return None
    new_name = wheel_path.name[: -len(_LINUX_ARMV7L_WHL_SUFFIX)] + f"-{plat}.whl"
    if new_name == wheel_path.name:
        return None
    new_path = wheel_path.parent / new_name
    staged = _armv7_retag_stage_path()

    try:
        with zipfile.ZipFile(wheel_path, "r") as zin:
            wheel_info_path = next(
                (info.filename for info in zin.infolist() if info.filename.lower().endswith(".dist-info/wheel")),
                None,
            )
            record_path = next(
                (info.filename for info in zin.infolist() if info.filename.lower().endswith(".dist-info/record")),
                None,
            )
            if not wheel_info_path:
                return None

            wheel_text = zin.read(wheel_info_path).decode("utf-8")
            new_lines: list[str] = []
            for line in wheel_text.splitlines():
                stripped = line.rstrip()
                if stripped.startswith("Tag:") and _LINUX_ARMV7L_TAG_RE.search(stripped):
                    line = _LINUX_ARMV7L_TAG_RE.sub(f"-{plat}", stripped)
                new_lines.append(line)
            updated_wheel = ("\n".join(new_lines) + "\n").encode("utf-8")

            updated_record: bytes | None = None
            if record_path:
                record_lines: list[str] = []
                for line in zin.read(record_path).decode("utf-8").splitlines():
                    if not line.strip():
                        continue
                    rec_name = line.split(",", 1)[0]
                    if rec_name.lower() == wheel_info_path.lower():
                        digest = _wheel_record_sha256(updated_wheel)
                        line = f"{rec_name},sha256={digest},{len(updated_wheel)}"
                    record_lines.append(line)
                updated_record = ("\n".join(record_lines) + "\n").encode("utf-8")

            with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as zout:
                for info in zin.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if name == wheel_info_path:
                        zout.writestr(info, updated_wheel)
                    elif record_path and name == record_path and updated_record is not None:
                        zout.writestr(info, updated_record)
                    else:
                        zout.writestr(info, zin.read(name))

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
    except (OSError, zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        return None
    finally:
        staged.unlink(missing_ok=True)


# External OpenSSL 3 is not in the manylinux baseline before glibc 2.35.
# Bullseye (``manylinux_2_31``) ships OpenSSL 1.1 only; Bookworm (``2_36``) ships OpenSSL 3.
_OPENSSL3_SONAMES = frozenset({"libcrypto.so.3", "libssl.so.3"})
_OPENSSL3_EXTERNAL_MAX_GLIBC = (2, 34)
_GLIBC_VERSION_NAME = re.compile(r"GLIBC_(\d+)\.(\d+)$")


def _elf_needed_and_glibc(blob: bytes) -> tuple[set[str], set[tuple[int, int]]] | None:
    """Return ``DT_NEEDED`` sonames and required ``GLIBC_M.N`` versions.

    ``None`` when ``blob`` is not a little-endian ELF this parser understands.
    An ELF with no dynamic segment returns empty sets.
    """
    if len(blob) < 52 or blob[:4] != b"\x7fELF" or blob[5] != 1:
        return None
    ei_class = blob[4]
    if ei_class == 1:
        if len(blob) < 52:
            return None
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
        dyn_fmt = "<II"
        dyn_size = 8
        is32 = True
    elif ei_class == 2:
        if len(blob) < 64:
            return None
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
        dyn_fmt = "<QQ"
        dyn_size = 16
        is32 = False
    else:
        return None
    if e_phnum <= 0 or e_phnum > 128 or e_phentsize < 32 or e_phoff < 0:
        return None

    loads: list[tuple[int, int, int]] = []
    dynamic_off: int | None = None
    dynamic_filesz = 0
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off < 0 or off + (32 if is32 else 56) > len(blob):
            return None
        if is32:
            p_type, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _flags, _align = struct.unpack_from(
                "<IIIIIIII", blob, off
            )
        else:
            p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _align = struct.unpack_from(
                "<IIQQQQQQ", blob, off
            )
        if p_type == 1:  # PT_LOAD
            loads.append((p_offset, p_vaddr, p_filesz))
        elif p_type == 2:  # PT_DYNAMIC
            dynamic_off = p_offset
            dynamic_filesz = p_filesz
    if dynamic_off is None or dynamic_filesz <= 0:
        return set(), set()

    def vaddr_to_offset(vaddr: int) -> int | None:
        for p_offset, p_vaddr, p_filesz in loads:
            if p_vaddr <= vaddr < p_vaddr + p_filesz:
                return p_offset + (vaddr - p_vaddr)
        return None

    strtab_vaddr: int | None = None
    verneed_vaddr: int | None = None
    verneed_num = 0
    needed_offs: list[int] = []
    pos = dynamic_off
    end = min(len(blob), dynamic_off + dynamic_filesz)
    while pos + dyn_size <= end:
        tag, val = struct.unpack_from(dyn_fmt, blob, pos)
        pos += dyn_size
        if tag == 0:  # DT_NULL
            break
        if tag == 1:  # DT_NEEDED
            needed_offs.append(val)
        elif tag == 5:  # DT_STRTAB
            strtab_vaddr = val
        elif tag == 0x6FFFFFFE:  # DT_VERNEED
            verneed_vaddr = val
        elif tag == 0x6FFFFFFF:  # DT_VERNEEDNUM
            verneed_num = val
    if strtab_vaddr is None:
        return None
    strtab_off = vaddr_to_offset(strtab_vaddr)
    if strtab_off is None:
        return None

    def read_cstr(rel: int) -> str:
        start = strtab_off + rel
        if start < 0 or start >= len(blob):
            return ""
        end_s = blob.find(b"\x00", start)
        if end_s < 0:
            return ""
        return blob[start:end_s].decode("ascii", "replace")

    needed = {name for name in (read_cstr(off) for off in needed_offs) if name}
    glibc: set[tuple[int, int]] = set()
    if verneed_vaddr is not None and verneed_num:
        vn_off = vaddr_to_offset(verneed_vaddr)
        if vn_off is not None:
            for _entry in range(min(int(verneed_num), 64)):
                if vn_off < 0 or vn_off + 16 > len(blob):
                    break
                _vn_version, vn_cnt, _vn_file, vn_aux, vn_next = struct.unpack_from("<HHIII", blob, vn_off)
                aux = vn_off + vn_aux
                for _aux_i in range(min(int(vn_cnt), 64)):
                    if aux < 0 or aux + 16 > len(blob):
                        break
                    _vna_hash, _vna_flags, _vna_other, vna_name, vna_next = struct.unpack_from("<IHHII", blob, aux)
                    match = _GLIBC_VERSION_NAME.match(read_cstr(vna_name))
                    if match:
                        version = (int(match.group(1)), int(match.group(2)))
                        if version[0] == 2 and version[1] <= 80:
                            glibc.add(version)
                    if vna_next == 0:
                        break
                    aux += vna_next
                if vn_next == 0:
                    break
                vn_off += vn_next
    return needed, glibc


def _armv7_wheel_plat_violation(wheel_path: Path, plat: str) -> str | None:
    """Why ``wheel_path`` must not be published as ``plat``, or None.

    Piwheels ``linux_armv7l`` wheels are retagged to the repair image's manylinux
    tag without relinking. That tag is a glibc/OpenSSL promise: Bullseye is glibc
    2.31 and OpenSSL 1.1. A binary that needs glibc 2.34 or unpackaged
    ``libssl.so.3`` / ``libcrypto.so.3`` is the Bookworm build under a Bullseye name.
    """
    plat_tags = manylinux_glibc_tags_in_name(plat)
    if not plat_tags:
        return None
    plat_glibc = max(plat_tags)
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            bundled = {info.filename.rsplit("/", 1)[-1] for info in zf.infolist()}
            needed_glibc: set[tuple[int, int]] = set()
            openssl3: set[str] = set()
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = info.filename.rsplit("/", 1)[-1]
                if ".so" not in base:
                    continue
                parsed = _elf_needed_and_glibc(zf.read(info))
                if parsed is None:
                    continue
                needed, glibc = parsed
                needed_glibc.update(glibc)
                for soname in needed:
                    if soname in _OPENSSL3_SONAMES and soname not in bundled:
                        openssl3.add(soname)
    except (OSError, zipfile.BadZipFile):
        return None

    reasons: list[str] = []
    too_new = {ver for ver in needed_glibc if ver > plat_glibc}
    if too_new:
        major, minor = max(too_new)
        reasons.append(f"needs glibc {major}.{minor}, {plat} allows {plat_glibc[0]}.{plat_glibc[1]}")
    if openssl3 and plat_glibc <= _OPENSSL3_EXTERNAL_MAX_GLIBC:
        libs = ", ".join(sorted(openssl3))
        reasons.append(f"links unpackaged {libs} (absent from the {plat} baseline)")
    if not reasons:
        return None
    return "; ".join(reasons)


def _unlink_armv7_plat_violation(wheel_path: Path, plat: str, platform_name: str, arch: str) -> bool:
    """Delete an ARMv7 wheel whose ELF does not meet ``AUDITWHEEL_PLAT``. Returns True if deleted."""
    if platform_name != "Linux" or arch != "armv7l" or not plat:
        return False
    if "manylinux" not in wheel_path.name.lower():
        return False
    violation = _armv7_wheel_plat_violation(wheel_path, plat)
    if not violation:
        return False
    print_color(f"  -> Deleting {wheel_path.name} ({violation})", Fore.RED)
    wheel_path.unlink(missing_ok=True)
    return True


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


def main() -> None:
    wheels_dir: Path = Path("./downloaded_wheels")
    temp_dir: Path = Path("./temp_repair")
    temp_dir.mkdir(exist_ok=True)

    # Find all wheel files (dedupe: same inode can appear twice via symlinks / layout quirks)
    wheels: list[Path] = _dedupe_wheel_paths(wheels_dir)

    if not wheels:
        print_color(f"No wheels found in {wheels_dir} - nothing to repair", Fore.YELLOW)
        print("Exiting successfully (no wheels to process)")
        return

    print_color(f"Found {len(wheels)} wheels\n")

    current_platform: str = get_platform()
    current_arch: str = platform.machine()

    if current_platform == "Linux" and current_arch in ("x86_64", "aarch64"):
        pruned = prune_ci_manylinux_newer_than_228_when_228_mirror_present(
            wheel_dir=wheels_dir,
            package_names=_force_no_binary_linux_normalized(_REPO_ROOT),
        )
        if pruned:
            print_color(
                f"Pre-repair: pruned {pruned} manylinux wheel(s) newer than 2_28 (PyPI 2_28 mirror kept)",
                Fore.YELLOW,
            )
            wheels = _dedupe_wheel_paths(wheels_dir)

    if current_platform == "Darwin":
        pruned = prune_ci_macos_newer_than_pypi_mirror(wheel_dir=wheels_dir)
        if pruned:
            print_color(
                f"Pre-repair: pruned {pruned} macOS wheel(s) newer than the PyPI macosx tag",
                Fore.YELLOW,
            )
            wheels = _dedupe_wheel_paths(wheels_dir)

    repaired_count: int = 0
    skipped_count: int = 0
    deleted_count: int = 0
    error_count: int = 0
    errors: list[str] = []

    # Repair each wheel
    for wheel in wheels:
        print(f"Processing: {wheel.name}")

        # Skip pure Python wheels
        if is_pure_python_wheel(wheel.name):
            print_color("  -> Skipping pure Python wheel")
            skipped_count += 1
            continue

        # Skip pywin32 wheels on Windows (DLLs are internal to the wheel)
        if current_platform == "Windows" and wheel.name.startswith("pywin32"):
            print_color("  -> Skipping pywin32 wheel (DLLs are internal)")
            skipped_count += 1
            continue

        # Skip wheels not for the workflow platform
        if not is_platform_wheel(wheel.name, current_platform, current_arch):
            print_color(f"  -> Skipping (not a {current_platform} wheel)")
            skipped_count += 1
            continue

        # For Linux, skip wheels for different architectures
        if current_platform == "Linux":
            wheel_arch = get_wheel_arch(wheel.name)
            if wheel_arch and wheel_arch != current_arch:
                print_color(f"  -> Skipping incompatible architecture ({wheel_arch} wheel on {current_arch} platform)")
                skipped_count += 1
                continue

        # PEP 427: wheels are zip files; truncated/corrupt CI artifacts may pass is_zipfile
        # but fail on central directory (delocate: BadZipFile).
        if not wheel_archive_is_readable(wheel):
            print_color("  -> Deleting file (not a valid / readable zip wheel archive)", Fore.RED)
            wheel.unlink(missing_ok=True)
            deleted_count += 1
            continue

        plat_env = os.environ.get("AUDITWHEEL_PLAT", "").strip()
        wheel_plat = plat_env
        if current_platform == "Linux" and current_arch == "armv7l" and plat_env and _is_linux_tag_wheel(wheel.name):
            retagged = _retag_linux_armv7_wheel_to_plat(wheel, plat_env)
            if not retagged:
                retagged = _retag_linux_armv7_wheel_to_plat(wheel, plat_env)
            if retagged and _armv7_forced_plat_filename_ok(retagged.name, plat_env):
                if _unlink_armv7_plat_violation(retagged, plat_env, current_platform, current_arch):
                    deleted_count += 1
                    continue
                print_color(
                    f"  -> Retagged linux_armv7l to {plat_env}: {retagged.name}",
                    Fore.GREEN,
                )
                repaired_count += 1
            else:
                if retagged and not _armv7_forced_plat_filename_ok(retagged.name, plat_env):
                    msg = (
                        "Retagged wheel filename does not match forced "
                        f"{_forced_plat_phrase(plat_env, plat_env)}: {retagged.name}"
                    )
                else:
                    msg = f"Failed to retag linux_armv7l wheel to {plat_env!r}: {wheel.name}"
                print_color(f"  -> ERROR: {msg}", Fore.RED)
                errors.append(msg)
                if retagged:
                    retagged.unlink(missing_ok=True)
                wheel.unlink(missing_ok=True)
                error_count += 1
            continue

        if _should_skip_armv7_auditwheel(wheel.name, current_arch):
            print_color(
                "  -> Skipping auditwheel (linux_armv7l; keep piwheels/local wheel on ARMv7)",
                Fore.YELLOW,
            )
            skipped_count += 1
            continue

        if current_platform == "Linux" and should_skip_linux_auditwheel_for_pypi_mirror(wheel.name):
            print_color(
                "  -> Skipping auditwheel (PyPI manylinux_2_28 mirror; repair would retag to host 2_34)",
                Fore.YELLOW,
            )
            skipped_count += 1
            continue

        if current_platform == "Darwin" and should_skip_macos_delocate_for_pypi_mirror(wheel.name):
            print_color(
                "  -> Skipping delocate (keep PyPI macOS tag; local retag can steal pip preference)",
                Fore.YELLOW,
            )
            skipped_count += 1
            continue

        # Clean temp directory
        for old_wheel in temp_dir.glob("*.whl"):
            old_wheel.unlink()

        # Repair wheel using platform-specific tool
        if current_platform == "Windows":
            result = repair_wheel_windows(wheel, temp_dir)
        elif current_platform == "Darwin":
            result = repair_wheel_macos(wheel, temp_dir)
        elif current_platform == "Linux":
            result = repair_wheel_linux(wheel, temp_dir, plat=wheel_plat)
        else:
            print_color(f"  -> ERROR: Unsupported platform {current_platform}", Fore.RED)
            error_count += 1
            continue

        if result.stdout:
            print(f"  {result.stdout.strip()}")
        if result.stderr:
            print_color(f"  {result.stderr.strip()}", Fore.RED)

        # Check for errors
        # auditwheel may log failures on stdout or stderr depending on version / logging.
        error_msg = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()

        # Corrupt zip / bad central directory (delocate opens the wheel as a zip)
        if _stderr_indicates_bad_zip(error_msg):
            print_color("  -> Deleting file (repair tool reported corrupt zip archive)", Fore.RED)
            for old_wheel in temp_dir.glob("*.whl"):
                old_wheel.unlink()
            wheel.unlink(missing_ok=True)
            deleted_count += 1
            continue

        # Special handling for incorrectly tagged universal2 wheels on macOS
        if (
            current_platform == "Darwin"
            and "universal2" in wheel.name
            and "Failed to find any binary with the required architecture" in error_msg
        ):
            # Try to fix by renaming the wheel to the correct architecture
            renamed_wheel = fix_universal2_wheel_name(wheel, error_msg)

            if renamed_wheel == "delete":
                # Wheel was corrupted and has been deleted
                deleted_count += 1
                continue
            elif renamed_wheel:
                # Clean temp directory and retry with renamed wheel
                for old_wheel in temp_dir.glob("*.whl"):
                    old_wheel.unlink()

                print_color("  -> Retrying delocate with corrected wheel name", Fore.CYAN)
                result = repair_wheel_macos(Path(renamed_wheel), temp_dir)

                if result.stdout:
                    print(f"  {result.stdout.strip()}")
                if result.stderr:
                    print_color(f"  {result.stderr.strip()}", Fore.RED)

                # Update wheel reference and error message for subsequent checks
                wheel = Path(renamed_wheel)
                error_msg = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()

        # Special handling for auditwheel "not a platform wheel" (no ELF in archive).
        # PyPI may ship manylinux-tagged pure wheels (e.g. dbus-fast 2.39+ on x86_64/aarch64);
        # keep them on standard Linux repair hosts. ARMv7 still deletes (corrupt piwheels case).
        if current_platform == "Linux" and "This does not look like a platform wheel, no ELF executable" in error_msg:
            if current_arch == "armv7l":
                print_color("  -> Deleting corrupted wheel", Fore.RED)
                wheel.unlink(missing_ok=True)
                deleted_count += 1
            else:
                print_color(
                    "  -> Keeping original wheel (manylinux tag but no native extensions; auditwheel N/A)",
                    Fore.YELLOW,
                )
                skipped_count += 1
            continue

        allow_linux_tag = _allow_linux_tag_env_enabled()
        is_linux_tag = _is_linux_tag_wheel(wheel.name)
        is_std_linux_arch = current_platform == "Linux" and current_arch in ("x86_64", "aarch64")

        # auditwheel refused the forced ARMv7 tag because the binary needs newer
        # versioned symbols than that glibc. Keeping the wheel publishes the tag anyway.
        if (
            current_platform == "Linux"
            and current_arch == "armv7l"
            and plat_env
            and "too-recent versioned symbols" in error_msg
        ):
            print_color(
                "  -> Deleting wheel (auditwheel: too-recent versioned symbols for "
                f"{plat_env}; not publishing that tag)",
                Fore.RED,
            )
            wheel.unlink(missing_ok=True)
            deleted_count += 1
            continue

        # Check for non-critical errors (keep original wheel)
        is_noncritical = (
            "too-recent versioned symbols" in error_msg
            # manylinux wheel can't find its libraries
            # it means it was already properly repaired
            or (("manylinux" in wheel.name and "could not be located" in error_msg) and not plat_env)
            # Some upstream manylinux wheels ship with DT_NEEDED entries that reference already-hashed
            # vendored libs (e.g. libbrotlicommon-<hash>.so.1). When our repair container can't
            # locate that exact filename, we should keep the original wheel rather than failing
            # the whole repair workflow (x86_64/aarch64 only; ARMv7 uses forced plat policy).
            or (
                is_std_linux_arch
                and "manylinux" in wheel.name
                and "Cannot repair wheel, because required library" in error_msg
                and "could not be located" in error_msg
            )
            # When allowing linux-tag wheels (piwheels), treat missing graft libs as non-fatal
            # and keep the original linux-tag wheel rather than failing the whole repair job.
            or (
                not plat_env
                and allow_linux_tag
                and is_linux_tag
                and (
                    "Cannot repair wheel, because required library" in error_msg or "could not be located" in error_msg
                )
            )
            # ARMv7 CI runs under QEMU; auditwheel may fail libc detection on abi3/native .so
            # When AUDITWHEEL_PLAT is set (ARMv7 vs ARMv7 Legacy), skipping repair would keep
            # identical wheel filenames across lineages — do not treat libc detection as non-critical.
            or (
                current_platform == "Linux"
                and current_arch == "armv7l"
                and not plat_env
                and ("InvalidLibc" in error_msg or "couldn't detect libc" in error_msg)
            )
        )

        has_error = (
            any(
                [
                    "ValueError:" in error_msg,
                    "FileNotFoundError:" in error_msg,
                    "Cannot repair wheel" in error_msg,
                    "could not be located" in error_msg,
                    "DelocationError:" in error_msg,
                ]
            )
            and not is_noncritical
        )

        if is_noncritical:
            # Non-critical error - keep the wheel
            if "too-recent versioned symbols" in error_msg:
                print_color("  -> Keeping original wheel (build issue: needs older toolchain)", Fore.YELLOW)
            elif "manylinux" in wheel.name and "could not be located" in error_msg:
                print_color("  -> Keeping original wheel (already bundled from PyPI)", Fore.GREEN)
            elif plat_env and allow_linux_tag and is_linux_tag:
                print_color(
                    "  -> Keeping original wheel (linux-tag wheel; not forcing manylinux under current policy)",
                    Fore.YELLOW,
                )
            elif (
                current_platform == "Linux"
                and current_arch == "armv7l"
                and not plat_env
                and ("InvalidLibc" in error_msg or "couldn't detect libc" in error_msg)
            ):
                print_color(
                    "  -> Keeping original wheel (auditwheel libc detection failed on ARMv7 runner; often QEMU)",
                    Fore.YELLOW,
                )
            if _unlink_armv7_plat_violation(wheel, plat_env, current_platform, current_arch):
                deleted_count += 1
                continue
            if (
                plat_env
                and current_platform == "Linux"
                and current_arch == "armv7l"
                and not _armv7_forced_plat_filename_ok(wheel.name, wheel_plat)
                and not (allow_linux_tag and is_linux_tag)
            ):
                msg = (
                    "Wheel filename does not match forced "
                    f"{_forced_plat_phrase(wheel_plat, plat_env)} "
                    f"after non-fatal repair path: {wheel.name}"
                )
                print_color(f"  -> ERROR: {msg}", Fore.RED)
                errors.append(f"{wheel.name}: {msg}")
                wheel.unlink(missing_ok=True)
                error_count += 1
                continue
            skipped_count += 1
        elif has_error:
            # Actual error occurred (even if a wheel was created, it may be broken)
            # Clean up any partial wheel
            for old_wheel in temp_dir.glob("*.whl"):
                old_wheel.unlink()
            print_color(f"  -> ERROR: {error_msg}", Fore.RED)
            errors.append(f"{wheel.name}: {error_msg}")
            error_count += 1
        else:
            # Check if a repaired wheel was created
            repaired = next(temp_dir.glob("*.whl"), None)

            if repaired:
                # A repaired wheel was created successfully
                if repaired.name != wheel.name:
                    wheel.unlink(missing_ok=True)  # Remove original
                    final_path = wheel.parent / repaired.name
                    repaired.rename(final_path)
                    print_color(f"  -> Replaced with repaired wheel: {repaired.name}", Fore.GREEN)
                else:
                    # Name unchanged
                    wheel.unlink(missing_ok=True)
                    repaired.rename(wheel)
                    final_path = wheel
                    print_color(f"  -> Repaired successfully: {repaired.name}", Fore.GREEN)
                if not wheel_archive_is_readable(final_path):
                    print_color("  -> Deleting repaired output (not a valid / readable zip archive)", Fore.RED)
                    final_path.unlink(missing_ok=True)
                    deleted_count += 1
                elif (
                    plat_env
                    and current_platform == "Linux"
                    and current_arch == "armv7l"
                    and not _armv7_forced_plat_filename_ok(final_path.name, wheel_plat)
                    and not (allow_linux_tag and _is_linux_tag_wheel(final_path.name))
                ):
                    msg = (
                        "Repaired wheel filename does not match forced "
                        f"{_forced_plat_phrase(wheel_plat, plat_env)}: {final_path.name}"
                    )
                    print_color(f"  -> ERROR: {msg}", Fore.RED)
                    errors.append(f"{final_path.name}: {msg}")
                    final_path.unlink(missing_ok=True)
                    error_count += 1
                elif _unlink_armv7_plat_violation(final_path, plat_env, current_platform, current_arch):
                    deleted_count += 1
                else:
                    repaired_count += 1
            elif result.returncode == 0:
                # No repaired wheel created, but command succeeded (already compatible)
                if (
                    plat_env
                    and current_platform == "Linux"
                    and current_arch == "armv7l"
                    and not _armv7_forced_plat_filename_ok(wheel.name, wheel_plat)
                    and not (allow_linux_tag and is_linux_tag)
                ):
                    msg = (
                        "auditwheel reported success but left the wheel unchanged with a filename "
                        f"that does not match {_forced_plat_phrase(wheel_plat, plat_env)}: {wheel.name}"
                    )
                    print_color(f"  -> ERROR: {msg}", Fore.RED)
                    errors.append(f"{wheel.name}: {msg}")
                    wheel.unlink(missing_ok=True)
                    error_count += 1
                    continue
                if _unlink_armv7_plat_violation(wheel, plat_env, current_platform, current_arch):
                    deleted_count += 1
                    continue
                print_color("  -> Keeping original wheel (already compatible)", Fore.GREEN)
                skipped_count += 1
            else:
                # Command failed and no wheel created
                print_color(f"  -> ERROR: {error_msg}", Fore.RED)
                errors.append(f"{wheel.name}: {error_msg}")
                error_count += 1

    if current_platform == "Linux" and current_arch in ("x86_64", "aarch64"):
        pruned = prune_ci_manylinux_newer_than_228_when_228_mirror_present(
            wheel_dir=wheels_dir,
            package_names=_force_no_binary_linux_normalized(_REPO_ROOT),
        )
        if pruned:
            print_color(
                f"Pruned {pruned} manylinux wheel(s) newer than 2_28 (PyPI 2_28 mirror kept)",
                Fore.YELLOW,
            )

    if current_platform == "Darwin":
        pruned = prune_ci_macos_newer_than_pypi_mirror(wheel_dir=wheels_dir)
        if pruned:
            print_color(
                f"Pruned {pruned} macOS wheel(s) newer than the PyPI macosx tag",
                Fore.YELLOW,
            )

    if current_platform == "Linux" and current_arch == "armv7l":
        pruned = _prune_manylinux_armv7_when_linux_tag_present(wheels_dir)
        if pruned:
            print_color(f"Pruned {pruned} manylinux armv7l wheel(s) superseded by linux_armv7l", Fore.YELLOW)

    print_color("---------- STATISTICS ----------")
    print_color(f"Total wheels: {len(wheels)}")
    print_color(f"Deleted wheels: {deleted_count}", Fore.RED)
    print_color(f"Kept wheels: {skipped_count}")
    print_color(f"Repaired wheels: {repaired_count}", Fore.GREEN)
    print_color(f"Errors: {error_count}", Fore.RED)

    if errors:
        print_color("---------- ERRORS ----------", Fore.RED)
        for i, error in enumerate(errors, start=1):
            print_color(f"{i}. {error}", Fore.RED)
        raise SystemExit("One or more wheels failed to repair")

    print("All wheels processed successfully")


if __name__ == "__main__":
    main()
