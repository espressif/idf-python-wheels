# ESP idf-python-wheels

Build of Windows Python Wheels required by IDF tools.

The build contains all wheels required by branches:
* release/v4.2.1
* release/v4.1.1
* master


Architectures specifies in .github/workflows:
* ubuntu-latest - x64
* macos-latest - x64
* macos-self-hosted - arm64
* windows-latest - x64

## Usage

```
.\Build-Wheels.ps1 -Branch "master" -CompileWheels @("greenlet", "gevent<2.0,>=1.2.2", "cryptography", "windows-curses") -Python python3.9
.\Test-Wheels.ps1 -Branch "master"
```

