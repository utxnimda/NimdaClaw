[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$SharedWorkspace = "",
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
$venvRoot = Join-Path $repoRoot "build\desktop-venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$requirements = Join-Path $repoRoot "packaging\requirements-desktop.txt"
$backendProject = Join-Path $repoRoot "apps\framework\backend"
$spec = Join-Path $repoRoot "packaging\nimda-desktop.spec"
$sharedWorkspaceCandidate = if ([string]::IsNullOrWhiteSpace($SharedWorkspace)) {
    $repoRoot
} else {
    $SharedWorkspace
}
$sharedWorkspaceRoot = (Resolve-Path -LiteralPath $sharedWorkspaceCandidate).Path
foreach ($requiredDirectory in @("config", "data")) {
    $requiredPath = Join-Path $sharedWorkspaceRoot $requiredDirectory
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Container)) {
        throw "Shared workspace is missing '$requiredDirectory': $sharedWorkspaceRoot"
    }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create desktop build environment: $venvRoot"
    }
}

if (-not $SkipDependencyInstall) {
    & $venvPython -m pip install --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install desktop build dependencies."
    }
    & $venvPython -m pip install --disable-pip-version-check "${backendProject}[web,desktop]"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to install Nimda desktop dependencies."
    }
}

Push-Location $repoRoot
try {
    & $venvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath (Join-Path $repoRoot "dist") `
        --workpath (Join-Path $repoRoot "build\pyinstaller") `
        $spec
    if ($LASTEXITCODE -ne 0) {
        throw "Nimda desktop build failed."
    }
} finally {
    Pop-Location
}

$exe = Join-Path $repoRoot "dist\Nimda\Nimda.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Desktop executable was not generated: $exe"
}
$packageRoot = Split-Path -Parent $exe
$bundledConfigRoot = Join-Path $packageRoot "config"
$bundledFeatureDataRoot = Join-Path $packageRoot "data\features"
if (Test-Path -LiteralPath $bundledConfigRoot) {
    throw "Desktop package unexpectedly contains an internal config directory: $bundledConfigRoot"
}
if (Test-Path -LiteralPath $bundledFeatureDataRoot) {
    throw "Desktop package unexpectedly contains an internal feature database: $bundledFeatureDataRoot"
}

$desktopConfig = Join-Path $packageRoot "nimda-desktop.yaml"
$workspaceYaml = $sharedWorkspaceRoot.Replace("\", "/").Replace("'", "''")
$desktopConfigLines = @(
    "# Nimda desktop bootstrap configuration."
    "# The desktop package contains no application database or normal feature config."
    "version: 1"
    "paths:"
    "  workspace_root: '$workspaceYaml'"
)
$desktopConfigText = [string]::Join([Environment]::NewLine, $desktopConfigLines) + [Environment]::NewLine
[IO.File]::WriteAllText($desktopConfig, $desktopConfigText, [Text.UTF8Encoding]::new($false))

$archive = Join-Path $repoRoot "dist\Nimda-Windows-x64.zip"
Compress-Archive -LiteralPath $packageRoot -DestinationPath $archive -Force
& $venvPython (Join-Path $scriptRoot "verify-desktop.py") `
    --package $packageRoot --archive $archive --workspace $sharedWorkspaceRoot
if ($LASTEXITCODE -ne 0) {
    throw "Desktop package verification failed; do not distribute this build."
}
Write-Host "Nimda desktop package: $exe"
Write-Host "Nimda desktop config: $desktopConfig"
Write-Host "Nimda shared workspace: $sharedWorkspaceRoot"
Write-Host "Nimda portable archive: $archive"
