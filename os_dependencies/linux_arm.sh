#!/bin/bash

arch=$(uname -m)

apt-get update

# AWS
apt-get install -y -q --no-install-recommends awscli

# PyGObject needs build dependecies https://pygobject.readthedocs.io/en/latest/getting_started.html
apt-get install libgirepository1.0-dev libgirepository-2.0-dev gcc libcairo2-dev pkg-config python3-dev -y

# dbus-python build dependecies
apt-get install libtiff5 libjpeg-dev libopenjp2-7 cmake libdbus-1-dev -y
apt-get install -y --no-install-recommends python3-dev libdbus-glib-1-dev libgirepository1.0-dev libcairo2-dev -y
apt-get install -y --no-install-recommends dbus-tests -y

#Only ARMv7
if [ "$arch" == "armv7l" ]; then
    # cryptography needs Rust
    # clean the container Rust installation to be sure right interpreter is used
    apt remove --auto-remove --purge rust-gdb rustc libstd-rust-dev libstd-rust-1.48
    # install Rust dependencies
    apt-get install -y build-essential libssl-dev libffi-dev python3-dev pkg-config gcc musl-dev
    # install Rust
    curl --proto '=https' --tlsv1.3 -sSf https://sh.rustup.rs | bash -s -- -y
    . $HOME/.cargo/env
fi
