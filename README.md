# ESP idf-python-wheels

The goal of this project is to automate build and upload process of required Python Wheels by IDF tools using GitHub Actions. We are able to build wheels for multiple OSes and architectures with multiple versions of Python.

Supported architectures:
* ubuntu-latest - x64
* macos-latest - x64
* macos-self-hosted - arm64
* windows-latest - x64
* linux-armv7-self-hosted - arm32
* linux-aarch64-self-hosted - arm64

Each architecture has it's own workflow in .github/workflows.

For each architecture the user can select Python version to built wheels. On self-hosted runners can handle multiple versions of Python with pyenv.

The build contains all wheels required by branches:
* release/v4.3
* release/v4.4
* master

## Configuration
- user can set `IDF_branch` input parameter to add wheels for another branch.
- currently, it is possible to run a workflow for the whole requirements.txt
- to add new architecture, set up GitHub runner, and create new GitHub Action
- workflows need to be started manually


## Usage for x64 build

```
.\Build-Wheels.ps1 -Branch "master" -Arch "" -CompileWheels @("greenlet", "gevent<2.0,>=1.2.2", "cryptography", "windows-curses", "python-pkcs11") -Python python3.9
.\Test-Wheels.ps1 -Branch "master"
```

## Usage for arm64 build

```
.\Build-Wheels.ps1 -Branch "master" -Arch "-arm64" -CompileWheels @("greenlet", "gevent<2.0,>=1.2.2", "cryptography", "windows-curses", "python-pkcs11") -Python python3.9
.\Test-Wheels.ps1 -Branch "master"
```

## Requirements lists
These lists are files for requirements that should be added or excluded from the main requirements list which is automatically assembled.

### exclude_list.yaml
File for excluded Python packages in the **main requirements** list.

This YAML file is converted to Requirement from packaging.requirements because pip can handle this format, so the function for converting is designed to be compatible with [PEP508](https://peps.python.org/pep-0508/) scheme.
The opposite logic of exclude_list is handled by the function itself, which means it is supposed to be easy to use for developers, this is also the reason YAML format is used.

For every `package_name` there are options:
* `version` - supports all logic operators defined by PEP508 for versions (<, >, !=, etc.)
* `platform`

which could be a string or a list of strings.

exclude_list template:

    - package_name: '<name_of_package>'
        version: '<package_version_with_operator>' / ['<package_version_with_operator>', '<package_version_with_operator>']     # optional
        platform: '<platform>' / ['<platform>', '<platform>', '<platform>']                                                     # optional

The syntax can be converted into a sentence: "From assembled **main requirements** exclude `package_name` with `version` on `platform`".

example:

    - package_name: 'pyserial'
        version: ['>=3.3', '<3.6']
        platform: ['win32', 'linux', 'darwin']

This would mean: "From assembled **main requirements** exclude `pyserial` with version `>=3.3` and `<3.6` on platform `win32`, `linux`, `darwin`".

From the example above is clear that the `platform` could be left out (because all main platforms are specified) so the options `platform` or `version` are optional, one of them or both can be not specified and the key can be erased. When only `package_name` is given the package will be excluded from **main requirements**.


### include_list.yaml
File for additional Python packages to the **main requirements** list. Built separately to not restrict the **main requirements** list.

This YAML file uses the same mechanism such as **exclude_list** but without the opposite logic.

The syntax can be also converted into a sentence: "For assembled **main requirements** additionally include `package_name` with `version` on `platform`".


### build_requirements.txt
File for the requirements needed for the build process and the build script.
