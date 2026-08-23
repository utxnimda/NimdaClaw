[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$ListenHost = "127.0.0.1",
    [int]$Port = 8765,
    [switch]$StrictPort,
    [switch]$AllowRemote
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

$runArguments = @(
    "-m", "work_catalog_yaml",
    "jp-tv", "browse",
    "--host", $ListenHost,
    "--port", [string]$Port
)
if ($StrictPort) {
    $runArguments += "--strict-port"
}
if ($AllowRemote) {
    $runArguments += "--allow-remote"
}

Push-Location $repoRoot
try {
    & $Python @runArguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
