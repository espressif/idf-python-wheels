#!/usr/bin/env pwsh

param (
    [string]$Branch="main",
    [string]$Python="python3",
    [string]$Arch=""
)

$env:IDF_PATH=(Get-Location).Path
$FileBranch = ${Branch}.Replace('/', '_')
$RequirementsTxt="requirements-${FileBranch}.txt"

if ("$Arch" -eq "") {
    &$Python -m pip install --no-index --find-links download -r $RequirementsTxt
} else {
    arch $Arch $Python -m pip install --no-index --find-links download -r $RequirementsTxt
}

