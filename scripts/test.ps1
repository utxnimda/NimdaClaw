[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipJavaScript
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
. (Join-Path $scriptRoot "lib\workspace.ps1")

$testRoots = @(
    (Join-Path $repoRoot "apps\framework\tests"),
    (Join-Path $repoRoot "apps\features")
) | Where-Object { Test-Path -LiteralPath $_ -PathType Container }
$testFiles = @($testRoots | ForEach-Object {
    Get-ChildItem -LiteralPath $_ -Recurse -File -Filter "test_*.py"
}) | Sort-Object FullName

Invoke-NimdaWorkspace -RepositoryRoot $repoRoot -Action {
    foreach ($testFile in $testFiles) {
        & $Python $testFile.FullName -v
        if ($LASTEXITCODE -ne 0) {
            throw "Python tests failed: $($testFile.FullName)"
        }
    }

    if (-not $SkipJavaScript) {
        $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
        if ($null -eq $nodeCommand) {
            Write-Warning "Node.js is unavailable; JavaScript syntax checks were skipped."
        } else {
            $javascriptFiles = Get-ChildItem -LiteralPath (Join-Path $repoRoot "apps") `
                -Recurse -File -Filter "*.js" |
                Where-Object { $_.FullName -notlike "*\vendor\*" }
            foreach ($javascriptFile in $javascriptFiles) {
                & $nodeCommand.Source --check $javascriptFile.FullName
                if ($LASTEXITCODE -ne 0) {
                    throw "JavaScript syntax check failed: $($javascriptFile.FullName)"
                }
            }
            Write-Host "JavaScript syntax checks passed: $($javascriptFiles.Count) files"

            $javascriptTestFiles = Get-ChildItem -LiteralPath (Join-Path $repoRoot "apps") `
                -Recurse -File -Filter "test_*.js" |
                Sort-Object FullName
            foreach ($javascriptTestFile in $javascriptTestFiles) {
                & $nodeCommand.Source --test $javascriptTestFile.FullName
                if ($LASTEXITCODE -ne 0) {
                    throw "JavaScript tests failed: $($javascriptTestFile.FullName)"
                }
            }
            Write-Host "JavaScript test files passed: $($javascriptTestFiles.Count) files"
        }
    }
}
