#!/usr/bin/env bash
# Minimal OS setup for ARMv7 Docker builds *before* pip installs (build_requirements / wheels).
# Expects to run as root (official python:*-bookworm / *-bullseye images).

set -e

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y --no-install-recommends \
  ca-certificates \
  libffi-dev \
  libssl-dev

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
