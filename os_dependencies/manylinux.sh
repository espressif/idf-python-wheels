#!/bin/bash
# Equivalent to ubuntu.sh and linux_arm.sh but using yum package manager

# Update package manager
yum update -y

# PyGObject needs build dependencies
yum install -y cairo-devel pkg-config gobject-introspection-devel

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
    libxcb-devel

# Additional libraries that wheels might depend on
yum install -y \
    gcc \
    gcc-c++ \
    make \
    brotli-devel
