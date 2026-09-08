#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0
#
# Debian 11 (bullseye) LTS ended 2026-08-31. The project no longer refreshes
# bullseye-security InRelease, so ``apt-get update`` exits 100:
#   Release file ... is expired (invalid since ...)
#
# Do not rewrite security URLs to archive.debian.org yet — that suite is not
# copied there. Ignore Valid-Until so the last published snapshot still works.
# Call before the first ``apt-get update``. Bookworm and later are unchanged.

debian_allow_expired_release_files() {
  local root="${1:-/}"
  mkdir -p "${root}/etc/apt/apt.conf.d"
  cat > "${root}/etc/apt/apt.conf.d/99no-check-valid-until" <<'EOF'
Acquire::Check-Valid-Until "false";
EOF
}

debian_repoint_eol_archive() {
  local root="${1:-/}"
  # Archived releases: archive.debian.org has no separate "-security" suite —
  # the archived main suite already reflects the final EOL state. Comment out
  # any *-security source and repoint the main debian.org source at the archive.
  local list="${root}/etc/apt/sources.list"
  if [ -f "$list" ]; then
    sed -i \
      -e '/-security/ s/^deb /# deb /' \
      -e 's|http://deb.debian.org/debian |http://archive.debian.org/debian |' \
      "$list"
  fi
  # Handle split sources.list.d/*.list if the bullseye image uses deb822 or split files
  for f in "${root}"/etc/apt/sources.list.d/*.list; do
    [ -e "$f" ] || continue
    sed -i \
      -e '/-security/ s/^deb /# deb /' \
      -e 's|http://deb.debian.org/debian |http://archive.debian.org/debian |' \
      "$f"
  done
}

debian_prepare_eol_apt() {
  local root="${1:-/}"
  local codename="${2:-}"
  if [ -z "${codename}" ] && [ -f "${root}/etc/os-release" ]; then
    # shellcheck disable=SC1090
    . "${root}/etc/os-release"
    codename="${VERSION_CODENAME:-}"
  fi
  case "${codename}" in
    bullseye)
      debian_allow_expired_release_files "${root}"
      debian_repoint_eol_archive "${root}"
      ;;
    *) return 0 ;;
  esac
}
