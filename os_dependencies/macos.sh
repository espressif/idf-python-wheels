#!/bin/bash

arch=$(uname -m)

# PyGObject needs build dependecies https://pygobject.readthedocs.io/en/latest/getting_started.html
brew install pygobject3 gtk4 gobject-introspection


# Only MacOS x86_64 additional dependencies
if [ "$arch" == "x86_64" ]; then
    echo "x86_64 additional dependencies"
    # cryptography 49+ has no PyPI wheel; CI builds from sdist against OpenSSL 4 (see README).
    # shellcheck source=os_dependencies/macos_openssl4_intel.sh
    source "$(dirname "$0")/macos_openssl4_intel.sh"
fi

# Only MacOS M1 additional dependencies
if [ "$arch" == "arm64" ]; then
    echo "M1 additional dependencies"
fi
