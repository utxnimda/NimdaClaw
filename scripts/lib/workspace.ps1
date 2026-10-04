# Shared source-workspace setup for launch, benchmark and test entrypoints.
# Dot-source this file; no environment or working-directory mutation occurs here.

function Get-NimdaBackendRoots {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$RepositoryRoot)

    $resolvedRoot = (Resolve-Path -LiteralPath $RepositoryRoot -ErrorAction Stop).ProviderPath
    $frameworkRoot = Join-Path $resolvedRoot "apps\framework\backend"
    if (-not (Test-Path -LiteralPath $frameworkRoot -PathType Container)) {
        throw "Nimda framework backend is missing: $frameworkRoot"
    }
    $frameworkRoot
    $featuresRoot = Join-Path $resolvedRoot "apps\features"
    if (Test-Path -LiteralPath $featuresRoot -PathType Container) {
        Get-ChildItem -LiteralPath $featuresRoot -Directory |
            ForEach-Object { Join-Path $_.FullName "backend" } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
            Sort-Object
    }
}

function Invoke-NimdaWorkspace {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepositoryRoot,
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [switch]$Utf8PythonIO
    )

    $resolvedRoot = (Resolve-Path -LiteralPath $RepositoryRoot -ErrorAction Stop).ProviderPath
    $backendRoots = @(Get-NimdaBackendRoots -RepositoryRoot $resolvedRoot)
    $workspaceEnvironment = @{
        PYTHONDONTWRITEBYTECODE = "1"
        PYTHONPATH = [string]::Join([IO.Path]::PathSeparator, $backendRoots)
        NIMDA_APPLICATION_ROOT = $resolvedRoot
        NIMDA_WORKSPACE_ROOT = $resolvedRoot
    }
    if ($Utf8PythonIO) { $workspaceEnvironment.PYTHONIOENCODING = "utf-8" }
    $previousEnvironment = @{}
    foreach ($name in $workspaceEnvironment.Keys) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }

    Push-Location -LiteralPath $resolvedRoot
    try {
        foreach ($name in $workspaceEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $workspaceEnvironment[$name], "Process")
        }
        & $Action
    } finally {
        foreach ($name in $previousEnvironment.Keys) {
            if ($null -eq $previousEnvironment[$name]) {
                Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
            } else {
                [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
            }
        }
        Pop-Location
    }
}
