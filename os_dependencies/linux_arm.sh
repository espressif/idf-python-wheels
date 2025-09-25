#!/bin/bash

arch=$(uname -m)

sudo apt-get update

# AWS
sudo apt-get install -y -q --no-install-recommends awscli

sudo apt-get install -y cmake build-essential

# PyGObject needs build dependecies https://pygobject.readthedocs.io/en/latest/getting_started.html
sudo apt-get install -y libgirepository1.0-dev gcc libcairo2-dev pkg-config python3-dev gir1.2-gtk-4.0 libglib2.0-dev
# Try to install girepository-2.0-dev if available (may not exist in older distros)
sudo apt-get install -y libgirepository-2.0-dev

# dbus-python needs build dependecies
sudo apt-get install -y dbus libdbus-1-dev libdbus-glib-1-dev libdbus-1-3
sudo apt-get install -y --no-install-recommends dbus-tests

# Pillow needs comprehensive image processing libraries
sudo apt-get install -y \
    libjpeg-dev \
    libpng-dev \
    libtiff5-dev \
    zlib1g-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libwebp-dev \
    libopenjp2-7-dev \
    libfribidi-dev \
    libharfbuzz-dev \
    libxcb1-dev

# Set PKG_CONFIG_PATH to include system directories for all ARM builds
# Include both ARM64 and ARMv7 paths plus standard locations
export PKG_CONFIG_PATH="/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/lib/arm-linux-gnueabihf/pkgconfig:/usr/lib/pkgconfig:/usr/share/pkgconfig:${PKG_CONFIG_PATH:-}"
echo "export PKG_CONFIG_PATH=\"$PKG_CONFIG_PATH\"" >> ~/.bashrc

# Export to GitHub Actions environment if available
if [ -n "$GITHUB_ENV" ]; then
    echo "PKG_CONFIG_PATH=$PKG_CONFIG_PATH" >> $GITHUB_ENV
fi

#Only ARMv7
if [ "$arch" == "armv7l" ]; then
    # pip cache permissions to avoid warnings
    sudo mkdir -p /github/home/.cache/pip || true
    sudo chown -R $USER:$USER /github/home/.cache/pip || true

    # ARMv7 specific packages (not already installed globally)
    sudo apt-get install -y gobject-introspection

    # Install additional GObject introspection packages if available
    sudo apt-get install -y gobject-introspection-dev

    # Install GIR (GObject Introspection Repository) packages that might provide girepository-2.0
    sudo apt-get install -y gir1.2-glib-2.0 gir1.2-gtk-3.0

    # Try alternative package names for girepository-2.0 that might exist in newer repos
    sudo apt-get install -y libgirepository-dev
    sudo apt-get install -y gobject-introspection-1.0-dev


    # Additional dbus packages for ARMv7
    sudo apt-get install -y --reinstall dbus-1-dev dbus-1-doc libdbus-1-dev pkg-config

    # Try to install additional dbus development packages
    sudo apt-get install -y libdbus-glib-1-dev

    # Force update pkg-config cache
    sudo ldconfig

    # cryptography needs Rust
    # clean the container Rust installation to be sure right interpreter is used
    sudo apt remove --auto-remove --purge rust-gdb rustc libstd-rust-dev libstd-rust-1.48
    # install Rust dependencies
    sudo apt-get install -y libssl-dev libffi-dev gcc musl-dev
    # install Rust
    curl --proto '=https' --tlsv1.3 -sSf https://sh.rustup.rs | bash -s -- -y
    . $HOME/.cargo/env
fi
