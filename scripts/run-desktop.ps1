[CmdletBinding()]
param(
    [string]$Python = "python",
    [int]$Port = 8765,
    [switch]$DebugWebView
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
$backendRoot = Join-Path $repoRoot "apps\framework\backend"
$featureBackendRoots = @(
    Get-ChildItem -LiteralPath (Join-Path $repoRoot "apps\features") -Directory |
        ForEach-Object { Join-Path $_.FullName "backend" } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
        Sort-Object
)

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:NIMDA_WORKSPACE_ROOT = $repoRoot
$env:PYTHONPATH = [string]::Join(
    [IO.Path]::PathSeparator,
    @($backendRoot) + $featureBackendRoots
)

$arguments = @("-m", "work_catalog_yaml.desktop", "--workspace", $repoRoot, "--port", [string]$Port)
if ($DebugWebView) {
    $arguments += "--debug"
}

Push-Location $repoRoot
try {
    & $Python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
