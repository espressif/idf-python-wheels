#!/usr/bin/env bash
# Standalone cryptography 49+ build smoke test (local / debugging).
# Scheduled CI uses the main pipeline + validate_cryptography_macos_intel_wheel.sh instead.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

if [ "$(uname -m)" != "x86_64" ] || [ "$(uname -s)" != "Darwin" ]; then
  echo "This script requires macOS x86_64" >&2
  exit 1
fi

# shellcheck source=os_dependencies/macos_openssl4_intel.sh
source "${ROOT}/os_dependencies/macos_openssl4_intel.sh"

python -m pip install --upgrade pip
python -m pip install -r build_requirements.txt
python -m pip install delocate

rm -rf downloaded_wheels spike_dist spike_build
mkdir -p downloaded_wheels spike_dist spike_build

echo "========== pip wheel cryptography 49+ =========="
python -m pip wheel 'cryptography>=49,<50' \
  --no-binary cryptography \
  --wheel-dir downloaded_wheels \
  --no-cache-dir

WHEEL="$(ls -1 downloaded_wheels/cryptography-49*.whl downloaded_wheels/cryptography-5*.whl 2>/dev/null | head -1)"
if [ -z "${WHEEL}" ]; then
  echo "ERROR: no cryptography 49+ wheel produced" >&2
  exit 1
fi

echo "========== delocate-wheel =========="
delocate-wheel -w spike_dist -v "${WHEEL}"
cp -f spike_dist/cryptography-*.whl downloaded_wheels/

bash "${ROOT}/scripts/validate_cryptography_macos_intel_wheel.sh" downloaded_wheels

echo "========== SPIKE SUCCEEDED =========="
