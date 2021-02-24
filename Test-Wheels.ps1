#!/usr/bin/env pwsh

param (
    [string]$Branch="main",
    [string]$Python="python3.9"
)

$env:IDF_PATH=(Get-Location).Path
$FileBranch = ${Branch}.Replace('/', '_')
$RequirementsTxt="requirements-${FileBranch}.txt"

&$Python -m pip install --no-index --find-links download -r $RequirementsTxt

