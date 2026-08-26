#!/usr/bin/env bash
# Minimal OS setup for ARMv7 Docker builds *before* pip installs (build_requirements / wheels).
# Expects to run as root (official python:*-bookworm / *-bullseye images).
# Must be sourced (not subprocess bash) so PIP_NO_BINARY persists for later pip / PEP 517 builds.
# Restore caller ``set`` options afterwards so ``errexit`` does not leak into that shell.

_armv7_prepare_saved_opts="$(set +o)"
set -e

export DEBIAN_FRONTEND=noninteractive

# Explicit libffi runtime matches the dev headers (bullseye: libffi7, bookworm: libffi8).
# Piwheels manylinux cffi wheels can link against a newer libffi than the image ships;
# pairing dev + runtime keeps installs predictable. Wheel builds use piwheels (see workflow
# PIP_INDEX_URL); cffi/argon2 forced to sdists globally. Cryptography is only forced on
# explicit ARMv7 wheel rebuilds (see ``armv7_pip_wheel_subprocess_env``) — globally
# blocking it breaks Python 3.8 transitive deps (maturin sdist for esptool, etc.).
export PIP_NO_BINARY=cffi,argon2-cffi-bindings
. /etc/os-release
case "${VERSION_CODENAME:-}" in
  bullseye) LIBFFI_RUNTIME=libffi7 ;;
  bookworm) LIBFFI_RUNTIME=libffi8 ;;
  *)        LIBFFI_RUNTIME= ;;
esac

apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates \
  libffi-dev \
  libssl-dev \
  patchelf
if [ -n "$LIBFFI_RUNTIME" ]; then
  apt-get install -y --no-install-recommends "$LIBFFI_RUNTIME"
fi

# Manylinux/piwheels cffi wheels on armhf still reference libffi.so.7. Debian Bookworm only
# ships libffi.so.8, so "import _cffi_backend" fails inside pip's isolated build env
# (e.g. argon2-cffi-bindings metadata). Bullseye typically already provides .so.7 via libffi7.
arch=$(uname -m)
if [ "$arch" = "armv7l" ]; then
  for libdir in /usr/lib/arm-linux-gnueabihf /usr/lib/arm-linux-gnueabi; do
    if [ -d "$libdir" ] && [ -f "$libdir/libffi.so.8" ] && [ ! -e "$libdir/libffi.so.7" ]; then
      ln -sfn libffi.so.8 "$libdir/libffi.so.7"
      ldconfig 2>/dev/null || true
      break
    fi
  done
fi

eval "${_armv7_prepare_saved_opts}"
unset _armv7_prepare_saved_opts
