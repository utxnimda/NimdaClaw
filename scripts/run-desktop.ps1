[CmdletBinding()]
param(
    [string]$Python = "python",
    [int]$Port = 8765,
    [switch]$DebugWebView
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
. (Join-Path $scriptRoot "lib\workspace.ps1")

$arguments = @("-m", "work_catalog_yaml.desktop", "--workspace", $repoRoot, "--port", [string]$Port)
if ($DebugWebView) {
    $arguments += "--debug"
}

Invoke-NimdaWorkspace -RepositoryRoot $repoRoot -Action {
    & $Python @arguments
}
exit $LASTEXITCODE
