from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from work_catalog_yaml.layout import backend_roots


ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts" / "lib" / "workspace.ps1"
SHELLS = list(dict.fromkeys(path for name in ("powershell.exe", "pwsh") if (path := shutil.which(name))))


def quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(SHELLS, "PowerShell is unavailable")
class WorkspaceScriptsTest(unittest.TestCase):
    def run_script(self, source: str, *, cwd: str | Path | None = None, env=None):
        output = []
        for shell in SHELLS:
            with self.subTest(shell=shell):
                result = subprocess.run(
                    [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", "$ErrorActionPreference = 'Stop'\n. " + quoted(HELPER) + "\n" + source],
                    cwd=cwd or ROOT, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output.append(json.loads(result.stdout))
        return output

    def test_backend_discovery_matches_python_contract(self) -> None:
        outputs = self.run_script("@(Get-NimdaBackendRoots -RepositoryRoot " + quoted(ROOT) + ") | ConvertTo-Json -Compress")
        expected = [str(path) for path in backend_roots(ROOT)]
        for output in outputs:
            self.assertEqual(output, expected)

    def test_success_and_exception_restore_present_and_absent_environment(self) -> None:
        env = dict(os.environ)
        env.update({"NIMDA_APPLICATION_ROOT": "original-app", "PYTHONPATH": "original-python", "PYTHONDONTWRITEBYTECODE": "0", "PYTHONIOENCODING": "latin-1"})
        env.pop("NIMDA_WORKSPACE_ROOT", None)
        for fails in (False, True):
            with self.subTest(fails=fails), tempfile.TemporaryDirectory() as temporary:
                script = """
$originalLocation = (Get-Location).Path
$caught = $false
try {
    Invoke-NimdaWorkspace -RepositoryRoot REPO -Utf8PythonIO -Action {
        if ($env:NIMDA_WORKSPACE_ROOT -ne REPO -or (Get-Location).Path -ne REPO) { throw 'wrong workspace' }
        if ($env:PYTHONIOENCODING -ne 'utf-8' -or $env:PYTHONDONTWRITEBYTECODE -ne '1') { throw 'wrong Python settings' }
        FAILURE
    }
} catch {
    if ($_.Exception.Message -ne 'expected failure') { throw }
    $caught = $true
}
[ordered]@{
    app = $env:NIMDA_APPLICATION_ROOT
    python = $env:PYTHONPATH
    bytecode = $env:PYTHONDONTWRITEBYTECODE
    encoding = $env:PYTHONIOENCODING
    workspaceAbsent = -not (Test-Path -LiteralPath 'Env:NIMDA_WORKSPACE_ROOT')
    locationRestored = (Get-Location).Path -eq $originalLocation
    caught = $caught
} | ConvertTo-Json -Compress
""".replace("REPO", quoted(ROOT)).replace("FAILURE", "throw 'expected failure'" if fails else "")
                for output in self.run_script(script, cwd=temporary, env=env):
                    self.assertEqual(output, {"app": "original-app", "python": "original-python", "bytecode": "0", "encoding": "latin-1", "workspaceAbsent": True, "locationRestored": True, "caught": fails})

    def test_nested_invocations_restore_outer_snapshot(self) -> None:
        script = """
$before = $env:NIMDA_WORKSPACE_ROOT
Invoke-NimdaWorkspace -RepositoryRoot REPO -Action {
    $env:NIMDA_WORKSPACE_ROOT = 'outer override'
    Invoke-NimdaWorkspace -RepositoryRoot REPO -Action {
        if ($env:NIMDA_WORKSPACE_ROOT -ne REPO) { throw 'inner workspace missing' }
    }
    if ($env:NIMDA_WORKSPACE_ROOT -ne 'outer override') { throw 'outer workspace lost' }
}
@{ restored = $env:NIMDA_WORKSPACE_ROOT -eq $before } | ConvertTo-Json -Compress
""".replace("REPO", quoted(ROOT))
        for output in self.run_script(script):
            self.assertTrue(output["restored"])

    def test_invalid_workspace_never_changes_environment_or_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = """
$before = $env:PYTHONPATH
$location = (Get-Location).Path
$caught = $false
try { Invoke-NimdaWorkspace -RepositoryRoot INVALID -Action { throw 'must not execute' } }
catch { $caught = $_.Exception.Message -like '*framework backend is missing*' }
@{ caught = $caught; environmentRestored = $env:PYTHONPATH -eq $before; locationRestored = (Get-Location).Path -eq $location } | ConvertTo-Json -Compress
""".replace("INVALID", quoted(temporary))
            for output in self.run_script(script):
                self.assertEqual(output, {"caught": True, "environmentRestored": True, "locationRestored": True})

    def test_native_exit_status_survives_cleanup(self) -> None:
        script = "Invoke-NimdaWorkspace -RepositoryRoot " + quoted(ROOT) + " -Action { & " + quoted(sys.executable) + " -c 'import sys; sys.exit(7)' }\n@{ code = $LASTEXITCODE } | ConvertTo-Json -Compress\nexit 0"
        for output in self.run_script(script):
            self.assertEqual(output["code"], 7)


if __name__ == "__main__":
    unittest.main()
