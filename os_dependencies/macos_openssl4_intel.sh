#!/usr/bin/env bash
# OpenSSL 4 toolchain for cryptography 49+ on macOS Intel (x86_64).
# Source from macos.sh or spike CI after brew is available.
#
# cryptography 49+ vendors OpenSSL 4.0.1 in upstream wheels; Intel Mac has no PyPI
# wheel. Building from sdist against Homebrew openssl@3 breaks PyInstaller/runtime.
# See README.md "macOS Intel (x86_64) and cryptography".
#
# Sourced from macos.sh / spike CI: restore caller ``set`` options so ``-u``/``pipefail``
# do not leak into the rest of that shell.

_openssl4_saved_opts="$(set +o)"
_openssl4_restore_shell() {
  eval "${_openssl4_saved_opts}"
  unset _openssl4_saved_opts prefix
  unset -f _append_github_env _brew_openssl4_prefix _install_openssl4_from_source \
    _configure_openssl4_env _openssl4_restore_shell 2>/dev/null || true
}

set -euo pipefail

if [ "$(uname -m)" != "x86_64" ]; then
  _openssl4_restore_shell
  return 0 2>/dev/null || exit 0
fi

OPENSSL4_PREFIX="${OPENSSL4_PREFIX:-/opt/openssl4}"
OPENSSL4_VERSION="${OPENSSL4_VERSION:-4.0.1}"

_append_github_env() {
  local key="$1"
  local value="$2"
  if [ -n "${GITHUB_ENV:-}" ]; then
    echo "${key}=${value}" >> "${GITHUB_ENV}"
  fi
  export "${key}=${value}"
}

_brew_openssl4_prefix() {
  local formula
  for formula in openssl@4 openssl; do
    if brew list --formula "${formula}" &>/dev/null; then
      local prefix
      prefix="$(brew --prefix "${formula}" 2>/dev/null || true)"
      if [ -n "${prefix}" ] && [ -f "${prefix}/lib/pkgconfig/openssl.pc" ]; then
        local ver
        ver="$(PKG_CONFIG_PATH="${prefix}/lib/pkgconfig" pkg-config --modversion openssl 2>/dev/null || true)"
        if [ -n "${ver}" ]; then
          case "${ver%%.*}" in
            4|5|6|7|8|9) echo "${prefix}"; return 0 ;;
          esac
        fi
      fi
    fi
  done
  return 1
}

_install_openssl4_from_source() {
  if [ -x "${OPENSSL4_PREFIX}/bin/openssl" ]; then
    local installed_ver
    installed_ver="$("${OPENSSL4_PREFIX}/bin/openssl" version 2>/dev/null | awk '{print $2}' || true)"
    if [ -n "${installed_ver}" ] && [ "${installed_ver%%.*}" -ge 4 ] 2>/dev/null; then
      echo "OpenSSL ${installed_ver} already at ${OPENSSL4_PREFIX}"
      return 0
    fi
  fi

  echo "Building OpenSSL ${OPENSSL4_VERSION} into ${OPENSSL4_PREFIX} (no Homebrew OpenSSL 4 found)"
  brew install perl make 2>/dev/null || true
  local build_dir
  build_dir="$(mktemp -d)"
  trap 'rm -rf "${build_dir}"' RETURN

  curl -fsSL "https://www.openssl.org/source/openssl-${OPENSSL4_VERSION}.tar.gz" -o "${build_dir}/openssl.tar.gz"
  tar -xzf "${build_dir}/openssl.tar.gz" -C "${build_dir}"
  (
    cd "${build_dir}/openssl-${OPENSSL4_VERSION}"
    ./Configure darwin64-x86_64-cc --prefix="${OPENSSL4_PREFIX}" --openssldir="${OPENSSL4_PREFIX}"
    make -j"$(sysctl -n hw.ncpu 2>/dev/null || echo 2)"
    sudo make install_sw
  )
}

_configure_openssl4_env() {
  local prefix="$1"
  _append_github_env OPENSSL_DIR "${prefix}"
  _append_github_env OPENSSL_STATIC 1
  _append_github_env PKG_CONFIG_PATH "${prefix}/lib/pkgconfig${PKG_CONFIG_PATH:+:${PKG_CONFIG_PATH}}"
  _append_github_env CPPFLAGS "-I${prefix}/include${CPPFLAGS:+ ${CPPFLAGS}}"
  _append_github_env LDFLAGS "-L${prefix}/lib${LDFLAGS:+ ${LDFLAGS}}"
  _append_github_env PATH "${prefix}/bin:${PATH}"

  echo "OpenSSL for cryptography build:"
  PKG_CONFIG_PATH="${prefix}/lib/pkgconfig" pkg-config --modversion openssl || true
  "${prefix}/bin/openssl" version || true
}

prefix=""
if ! prefix="$(_brew_openssl4_prefix)"; then
  echo "Trying Homebrew openssl@4"
  brew install openssl@4 2>/dev/null || true
  prefix="$(_brew_openssl4_prefix)" || prefix=""
fi

if [ -n "${prefix}" ]; then
  echo "Using Homebrew OpenSSL at ${prefix}"
fi

if [ -z "${prefix:-}" ]; then
  _install_openssl4_from_source
  prefix="${OPENSSL4_PREFIX}"
fi

_configure_openssl4_env "${prefix}"
_openssl4_restore_shell
