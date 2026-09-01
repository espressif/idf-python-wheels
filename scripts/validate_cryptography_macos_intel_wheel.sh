#!/usr/bin/env bash
# Validate cryptography 49+ macOS Intel wheels (OpenSSL 4 linkage + import probe).
# Used after build_wheels / repair_wheels on macos-15-intel CI and for local checks.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WHEELS_DIR="${1:-${ROOT}/downloaded_wheels}"
WORKDIR="${ROOT}/cryptography_validate_build"

if [ "$(uname -m)" != "x86_64" ] || [ "$(uname -s)" != "Darwin" ]; then
  echo "cryptography macOS Intel validation requires macOS x86_64" >&2
  exit 1
fi

rm -rf "${WORKDIR}"
mkdir -p "${WORKDIR}"

CRYPTO_LIST="${WORKDIR}/cryptography_wheels.tsv"
python - <<'PY' "${WHEELS_DIR}" > "${CRYPTO_LIST}"
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import Version


def _python_version(path: str) -> tuple[int, int] | None:
    try:
        out = subprocess.check_output(
            [path, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        major_s, minor_s = out.split()
        return int(major_s), int(minor_s)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _discover_pythons() -> dict[tuple[int, int], str]:
    seen: dict[tuple[int, int], str] = {}
    candidates: set[str] = set()
    if default := shutil.which("python"):
        candidates.add(default)
    if default3 := shutil.which("python3"):
        candidates.add(default3)
    for pattern in (
        "/usr/local/bin/python3.*",
        "/opt/homebrew/bin/python3.*",
        "/Library/Frameworks/Python.framework/Versions/*/bin/python3*",
    ):
        candidates.update(glob.glob(pattern))
    for dirname in os.environ.get("PATH", "").split(os.pathsep):
        if not dirname:
            continue
        for path in Path(dirname).glob("python3.*"):
            candidates.add(str(path))
    for cmd in sorted(candidates):
        version = _python_version(cmd)
        if version and version not in seen:
            seen[version] = cmd
    return seen


def _min_python_for_wheel(path: Path) -> tuple[int, int]:
    _name, _ver, _build, tags = parse_wheel_filename(path.name)
    tag = min(tags, key=lambda item: (item.interpreter, item.abi, item.platform))
    match = re.fullmatch(r"cp(\d)(\d+)", tag.interpreter)
    if not match:
        raise ValueError(f"cannot parse interpreter tag from {path.name!r}")
    return int(match.group(1)), int(match.group(2))


def _pick_python(
    min_version: tuple[int, int], available: dict[tuple[int, int], str]
) -> str | None:
    viable = [(version, cmd) for version, cmd in available.items() if version >= min_version]
    if not viable:
        return None
    viable.sort(key=lambda item: item[0])
    return viable[0][1]


wheels_dir = Path(sys.argv[1])
available_pythons = _discover_pythons()
missing: list[tuple[Path, tuple[int, int]]] = []

for path in sorted(wheels_dir.glob("cryptography-*.whl")):
    try:
        _name, ver, _build, tags = parse_wheel_filename(path.name)
    except InvalidWheelFilename:
        continue
    if Version(str(ver)) < Version("49"):
        continue
    if not any("x86_64" in tag.platform for tag in tags):
        continue
    min_version = _min_python_for_wheel(path)
    wheel_python = _pick_python(min_version, available_pythons)
    if wheel_python is None:
        missing.append((path, min_version))
        continue
    print(f"{path.resolve()}\t{wheel_python}")

if missing:
    for path, (major, minor) in missing:
        print(
            f"ERROR: need Python >={major}.{minor} to validate {path.name}; "
            "install it (e.g. actions/setup-python) or add it to PATH",
            file=sys.stderr,
        )
    raise SystemExit(1)
PY

if [ ! -s "${CRYPTO_LIST}" ]; then
  echo "No cryptography>=49 wheels in ${WHEELS_DIR}; skipping macOS Intel OpenSSL 4 validation"
  exit 0
fi

WHEEL_COUNT="$(wc -l < "${CRYPTO_LIST}" | tr -d ' ')"
echo "Validating ${WHEEL_COUNT} cryptography 49+ wheel(s) in ${WHEELS_DIR}"

LAST_WHEEL=""
LAST_PYTHON=""
while IFS=$'\t' read -r WHEEL WHEEL_PYTHON; do
  [ -n "${WHEEL}" ] || continue
  LAST_WHEEL="${WHEEL}"
  LAST_PYTHON="${WHEEL_PYTHON}"
  echo "========== ${WHEEL} (python: ${WHEEL_PYTHON}) =========="
  rm -rf "${WORKDIR}/wheel"
  unzip -q -o "${WHEEL}" -d "${WORKDIR}/wheel"
  SO="$(find "${WORKDIR}/wheel" -name '_rust.abi3.so' | head -1)"
  if [ -z "${SO}" ]; then
    echo "ERROR: _rust.abi3.so not found in ${WHEEL}" >&2
    exit 1
  fi
  echo "--- otool -L _rust.abi3.so ---"
  otool -L "${SO}" || true
  if otool -L "${SO}" | grep -q '/usr/local/opt/openssl@3/'; then
    echo "ERROR: _rust.abi3.so links to Homebrew openssl@3 in ${WHEEL}" >&2
    exit 1
  fi

  rm -rf "${WORKDIR}/venv"
  "${WHEEL_PYTHON}" -m venv "${WORKDIR}/venv"
  # shellcheck disable=SC1091
  source "${WORKDIR}/venv/bin/activate"
  pip install --upgrade pip
  # Probe loads native_import_guard -> _helper_functions (colorama, packaging, PyYAML).
  pip install -r "${ROOT}/build_requirements.txt"
  pip install --no-index --find-links "${WHEELS_DIR}" "${WHEEL}"
  python "${ROOT}/scripts/run_native_import_probe.py" cryptography
  deactivate 2>/dev/null || true
done < "${CRYPTO_LIST}"

if [ "${RUN_PYINSTALLER_SMOKE:-0}" = "1" ] && [ -n "${LAST_WHEEL}" ] && [ -n "${LAST_PYTHON}" ]; then
  echo "========== PyInstaller smoke (${LAST_WHEEL}) =========="
  rm -rf "${WORKDIR}/venv"
  "${LAST_PYTHON}" -m venv "${WORKDIR}/venv"
  # shellcheck disable=SC1091
  source "${WORKDIR}/venv/bin/activate"
  pip install --upgrade pip
  pip install --no-index --find-links "${WHEELS_DIR}" "${LAST_WHEEL}"
  pip install pyinstaller
  cat > "${WORKDIR}/crypto_smoke.py" <<'PY'
from cryptography.hazmat.primitives import hashes
print(hashes.SHA256.name)
PY
  pyinstaller --onefile --distpath "${WORKDIR}/dist" --workpath "${WORKDIR}/pyi_work" \
    --specpath "${WORKDIR}" --name crypto_smoke "${WORKDIR}/crypto_smoke.py"
  env -u DYLD_LIBRARY_PATH -u DYLD_FALLBACK_LIBRARY_PATH \
    "${WORKDIR}/dist/crypto_smoke"
fi

echo "cryptography macOS Intel validation succeeded"
