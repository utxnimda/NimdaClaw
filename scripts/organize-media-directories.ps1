[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$OrganizerArguments,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
$backendRoot = Join-Path $repoRoot "apps\framework\backend"
$featureBackendRoot = Join-Path $repoRoot "apps\features\media-directory-organizer\backend"

$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = [string]::Join(
    [IO.Path]::PathSeparator,
    @($backendRoot, $featureBackendRoot)
)

Push-Location $repoRoot
try {
    & $Python -m media_directory_organizer @OrganizerArguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
