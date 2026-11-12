# Static libyaml for PyYAML sdists. The MSYS2 package is MinGW and does not link with MSVC.
# YAML_DECLARE_STATIC matches that static lib. CL is read by cl.exe, so every native
# build in this job sees it; other packages ignore the macro.
#
# WHEEL_PACKAGES unset: full requirements are chosen later (platform builds).
# WHEEL_PACKAGES set: defined-wheels input or a requirements file, including a
# one-package value such as "pyyaml~=6.0.3". Skip unless that text names PyYAML.
$ErrorActionPreference = "Stop"
if ($env:WHEEL_PACKAGES -and $env:WHEEL_PACKAGES -notmatch '(?i)(^|[^A-Za-z0-9_.-])pyyaml([^A-Za-z0-9_.-]|$)') {
    Write-Host "PyYAML is not in this build; skipping libyaml"
    exit 0
}
$version = "0.2.5"
$root = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { $env:TEMP }
$prefix = Join-Path $root "libyaml-$version"
$install = Join-Path $prefix "install"
$include = Join-Path $install "include"
$lib = Join-Path $install "lib"

if (-not (Test-Path "$include\yaml.h")) {
    $src = Join-Path $prefix "src"
    New-Item -ItemType Directory -Force -Path $src | Out-Null
    Invoke-WebRequest "https://github.com/yaml/libyaml/archive/refs/tags/$version.zip" -OutFile "$prefix\src.zip" -UseBasicParsing
    Expand-Archive "$prefix\src.zip" $src -Force
    # libyaml 0.2.5 requires CMake 3.0. CMake 4 rejects that unless this floor is set.
    # Quote values that contain '.': PowerShell parses -DNAME=3.5 as (-DNAME=3).5.
    cmake -S "$src\libyaml-$version" -B "$prefix\build" -G "Visual Studio 17 2022" -A x64 "-DCMAKE_INSTALL_PREFIX=$install" "-DCMAKE_POLICY_VERSION_MINIMUM=3.5" -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF
    if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }
    cmake --build "$prefix\build" --config Release --target install
    if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }
}
if (-not (Test-Path "$lib\yaml.lib")) { throw "yaml.lib missing under $lib" }

$env:INCLUDE = "$include;$env:INCLUDE"
$env:LIB = "$lib;$env:LIB"
if ($env:CL -notlike "*YAML_DECLARE_STATIC*") { $env:CL = "/DYAML_DECLARE_STATIC $($env:CL)".Trim() }
if ($env:GITHUB_ENV) {
    [System.IO.File]::AppendAllText($env:GITHUB_ENV, "INCLUDE=$env:INCLUDE`nLIB=$env:LIB`nCL=$env:CL`n")
}
