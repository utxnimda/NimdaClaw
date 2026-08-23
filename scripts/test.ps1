[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipJavaScript
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
$backendRoot = Join-Path $repoRoot "apps\framework\backend"
$featureBackendRoots = @(
    Get-ChildItem -LiteralPath (Join-Path $repoRoot "apps\features") -Directory |
        ForEach-Object { Join-Path $_.FullName "backend" } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Container }
)

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = [string]::Join(
    [IO.Path]::PathSeparator,
    @($backendRoot) + $featureBackendRoots
)

$testFiles = Get-ChildItem -LiteralPath (Join-Path $repoRoot "apps\features") `
    -Recurse -File -Filter "test_*.py" |
    Sort-Object FullName

Push-Location $repoRoot
try {
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
        }
    }
} finally {
    Pop-Location
}
