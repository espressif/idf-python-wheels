#!/bin/bash

arch=$(uname -m)

# PyGObject needs build dependecies https://pygobject.readthedocs.io/en/latest/getting_started.html
brew install pygobject3 gtk4


# Only MacOS x86_64 additional dependencies
if [ "$arch" == "x86_64" ]; then
    echo "x86_64 additional dependencies"
fi

# Only MacOS M1 additional dependencies
if [ "$arch" == "arm64" ]; then
    echo "M1 additional dependencies"
fi
