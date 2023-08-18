#!/usr/bin/env pwsh

param (
    [string]$Branch="main",
    [string]$Python="python3",
    [string]$Arch="",
    [string[]]$BuildEnv=@(),
    [string[]]$CompileWheels=@(),
    [switch]$NoReq=$false
)

"Using Python: $Python"
$env:IDF_PATH=(Get-Location).Path

$BranchNum = ($Branch -replace "\D+[^0-9][^0-9]" , '')

if ($BranchNum -eq "") {
    $BranchNum = "5.1" # master
}

if ($BranchNum -ge "5.0") {
    $RequirementsUrl="https://dl.espressif.com/dl/esp-idf/espidf.constraints.v${BranchNum}.txt"
} else {
    $RequirementsUrl="https://raw.githubusercontent.com/espressif/esp-idf/${Branch}/requirements.txt"
}

$FileBranch = ${Branch}.Replace('/', '_')
$RequirementsTxt="requirements-${FileBranch}.txt"
$OnlyBinary = ""
"Processing: $RequirementsUrl"
Invoke-WebRequest $RequirementsUrl -OutFile $RequirementsTxt

# If specific build environment is requested then build isolation must be disable in order to use the build environment.
# It might be necessary to clean or separate this build environment in the future.
if ($BuildEnv.count -ne 0) {
    $ExtraPipArgs = "--no-build-isolation"
}

# Install packages requested for the build environment
foreach ($build_req in $BuildEnv) {
    if ("$Arch" -eq "") {
        &$Python -m pip install $build_req
    } else {
        arch $Arch $Python -m pip install $build_req
    }
}

# Iterate over binaries which should be compiled.
# The build of next binary will receive list of previously build of binaries to avoid download
foreach ($wheelPrefix in $CompileWheels) {
    $wheel=Get-Content $RequirementsTxt | % { if($_ -match "^$wheelPrefix") {$_}}

    # If wheel is not defined in requirements.txt use the prefix as name
    if ("$wheel" -eq "") {
        $wheel = $wheelPrefix
    }

    if ($wheel.startswith('windows-curses')) {
        $wheel = "windows-curses"
    }

    "Processing: $wheel"
    # Split by default splits on each space, so empty args are passed as
    # requirements to pip-wheel, which complains with
    # ERROR: Invalid requirement: ''
    # "   ".Split() vs. "   ".split() | where {$_}
    $OnlyBinarySplitted = $OnlyBinary.Split(' ') | where {$_}
    if ("$Arch" -eq "") {
        &$Python -m pip wheel --find-links download --wheel-dir download $ExtraPipArgs $OnlyBinarySplitted $wheel
    } else {
        arch $Arch $Python -m pip wheel --find-links download --wheel-dir download $ExtraPipArgs $OnlyBinarySplitted $wheel
    }
    if ($LASTEXITCODE -ne 0) {
	exit 1
    }
    #$cache=pip cache dir
    #Get-ChildItem -Path $cache "{$wheel}*.whl" -Recurse | % {Copy-Item -Path $_.FullName -Destination download -Container }
    #ls download
    $OnlyBinary += " --only-binary $wheel"
}

if ($NoReq) {
    exit
}

$OnlyBinarySplitted = $OnlyBinary.Split(' ') | where {$_}
if ("$Arch" -eq "") {
    &$Python -m pip download $OnlyBinarySplitted --find-links download --dest download -r $RequirementsTxt
} else {
    arch $Arch $Python -m pip download $OnlyBinarySplitted --find-links download --dest download -r $RequirementsTxt
}
if ($LASTEXITCODE -ne 0) {
    exit 1
}

&$Python -m pip wheel --wheel-dir download --find-links download -r $RequirementsTxt
if ($LASTEXITCODE -ne 0) {
    exit 1
}
