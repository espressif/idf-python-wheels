param (
    [string]$Branch="main"
)

$env:IDF_PATH=(Get-Location).Path
$FileBranch = ${Branch}.Replace('/', '_')
$RequirementsTxt=".\requirements-${FileBranch}.txt"

python -m pip install --no-index --find-links download -r $RequirementsTxt
