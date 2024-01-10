#!/bin/bash
# PyGObject needs build dependecies https://pygobject.readthedocs.io/en/latest/getting_started.html
sudo apt install libgirepository1.0-dev gcc libcairo2-dev pkg-config python3-dev gir1.2-gtk-4.0 -y

# dbus-python needs build dependecies
sudo apt-get install cmake build-essential libdbus-1-dev libdbus-glib-1-dev -y
