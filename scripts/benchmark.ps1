[CmdletBinding()]
param(
    [string]$Python = "python",
    [ValidateRange(1, 10)][int]$Rounds = 3,
    [switch]$SkipJavaScript
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
. (Join-Path $scriptRoot "lib\workspace.ps1")

# API measurements read the shared database; media/cache fixtures are temporary.
Invoke-NimdaWorkspace -RepositoryRoot $repoRoot -Utf8PythonIO -Action {
    & $Python (Join-Path $scriptRoot "benchmark-api.py") --rounds $Rounds
    if ($LASTEXITCODE -ne 0) { throw "API benchmark failed." }

    & $Python (Join-Path $repoRoot "apps\features\collection-detail\tests\benchmark_resource_cache.py")
    if ($LASTEXITCODE -ne 0) { throw "Resource-cache benchmark failed." }

    if (-not $SkipJavaScript) {
        $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
        if ($null -eq $nodeCommand) {
            Write-Warning "Node.js is unavailable; frontend benchmark was skipped."
        } else {
            & $nodeCommand.Source (Join-Path $scriptRoot "benchmark-frontend.js")
            if ($LASTEXITCODE -ne 0) { throw "Frontend benchmark failed." }
        }
    }
}
