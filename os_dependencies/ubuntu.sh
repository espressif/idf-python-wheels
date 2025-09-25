#!/bin/bash
sudo apt-get update -y

# PyGObject needs build dependecies https://pygobject.readthedocs.io/en/latest/getting_started.html
sudo apt install libgirepository1.0-dev libgirepository-2.0-dev gcc libcairo2-dev pkg-config python3-dev gir1.2-gtk-4.0 -y

# dbus-python needs build dependecies
sudo apt-get install cmake build-essential libdbus-1-dev libdbus-glib-1-dev -y

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
