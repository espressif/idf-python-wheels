# ESP idf-python-wheels

The goal of this project is to automate build and upload process of required Python Wheels by IDF tools using GitHub Actions. We are able to build wheels for multiple OSes and architectures with multiple versions of Python.  

Supported architectures:
* ubuntu-latest - x64
* macos-latest - x64
* macos-self-hosted - arm64
* windows-latest - x64
* linux-armv7-self-hosted - arm32
* linux-aarch64-self-hosted - arm64

Each architecture has it's own workflow in .github/workflows except aarch64 Linux - not ready as GitHub runner yet. 

For each architecture the user can select Python version to built wheels. On self-hosted runners can handle multiple versions of Python with pyenv. 

The build contains all wheels required by branches:
* release/v4.3
* release/v4.4
* master

## Configuration
- user can set `IDF_branch` input parameter to add wheels for another branch.
- currently, it is possible to run a workflow for the whole requirenments.txt
- to add new architecture, set up GitHub runner, and create new GitHub Action
- workflows need to be started manually


## Usage for x64 build

```
.\Build-Wheels.ps1 -Branch "master" -Arch "" -CompileWheels @("greenlet", "gevent<2.0,>=1.2.2", "cryptography", "windows-curses") -Python python3.9
.\Test-Wheels.ps1 -Branch "master"
```

## Usage for arm64 build

```
.\Build-Wheels.ps1 -Branch "master" -Arch "-arm64" -CompileWheels @("greenlet", "gevent<2.0,>=1.2.2", "cryptography", "windows-curses") -Python python3.9
.\Test-Wheels.ps1 -Branch "master"
```
