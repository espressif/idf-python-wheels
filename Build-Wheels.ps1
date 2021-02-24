#!/usr/bin/env pwsh

param (
    [string]$Branch="main",
    [string]$Python="python3",
    [string[]]$CompileWheels=@()
)

"Using Python: $Python"
$env:IDF_PATH=(Get-Location).Path
$RequirementsUrl="https://raw.githubusercontent.com/espressif/esp-idf/${Branch}/requirements.txt"
$FileBranch = ${Branch}.Replace('/', '_')
$RequirementsTxt="requirements-${FileBranch}.txt"
$OnlyBinary = ""
"Processing: $RequirementsUrl"
Invoke-WebRequest $RequirementsUrl -OutFile $RequirementsTxt

# Iterate over binaries which should be compiled.
# The build of next binary will receive list of previously build of binaries to avoid download
foreach ($wheelPrefix in $CompileWheels) {
    $wheel=Get-Content $RequirementsTxt | % { if($_ -match "^$wheelPrefix") {$_}}

    # If wheel is not defined in requirements.txt use the prefix as name
    if ("$wheel" -eq "") {
        $wheel = $wheelPrefix
    }

    "Processing: $wheel"
    &$Python -m pip wheel --find-links download --wheel-dir download $OnlyBinary.Split(' ') $wheel
    #$cache=pip cache dir
    #Get-ChildItem -Path $cache "{$wheel}*.whl" -Recurse | % {Copy-Item -Path $_.FullName -Destination download -Container }
    #ls download
    $OnlyBinary += " --only-binary $wheel "
}


&$Python -m pip download $OnlyBinary.Split(' ') --find-links download --dest download -r $RequirementsTxt

