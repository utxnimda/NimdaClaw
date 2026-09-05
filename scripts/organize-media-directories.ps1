[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$OrganizerArguments,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptRoot
. (Join-Path $scriptRoot "lib\workspace.ps1")

Invoke-NimdaWorkspace -RepositoryRoot $repoRoot -Utf8PythonIO -Action {
    & $Python -m media_directory_organizer @OrganizerArguments
}
exit $LASTEXITCODE
