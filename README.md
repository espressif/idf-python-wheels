# ESP idf-python-wheels

Build of Windows Python Wheels required by IDF tools.

The build contains all wheels required by branches:
* release/v4.2
* release/v4.1.1
* master

## Usage

```
.\Build-Wheels.ps1 -Branch "master" -CompileWheels @("greenlet", "gevent<2.0,>=1.2.2", "cryptography", "windows-curses") -Python python3.9
.\Test-Wheels.ps1 -Branch "master"
```

