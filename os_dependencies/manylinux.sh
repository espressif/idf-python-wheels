#!/bin/bash
# Equivalent to ubuntu.sh and linux_arm.sh but using yum package manager

# Update package manager
yum update -y

# PyGObject needs build dependencies and runtime libraries (for auditwheel repair)
yum install -y \
    cairo-devel \
    cairo-gobject \
    cairo-gobject-devel \
    pkg-config \
    gobject-introspection \
    gobject-introspection-devel \
    glib2-devel

# Try to install girepository 2.0 packages (may not exist in older manylinux images)
# libgirepository-2.0.so.0 is provided by different packages depending on the distro version
yum install -y girepository2 girepository2-devel || true
yum install -y libgirepository || true

# dbus-python needs build dependencies
yum install -y cmake dbus-devel dbus-glib-devel

# Pillow needs image processing libraries
yum install -y \
    libjpeg-devel \
    libpng-devel \
    libtiff-devel \
    zlib-devel \
    freetype-devel \
    lcms2-devel \
    libwebp-devel \
    openjpeg2-devel \
    fribidi-devel \
    harfbuzz-devel \
    libxcb-devel \
    libXau-devel \
    brotli \
    brotli-devel

# Additional libraries that wheels might depend on
yum install -y \
    gcc \
    gcc-c++ \
    make
