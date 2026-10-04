"""Windows .lnk physical I/O, with no catalog or feature dependencies."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
from typing import Any
from uuid import uuid4


def create_windows_shortcut(shortcut_path: Path, target_path: Path) -> None:
    if os.name != "nt":
        raise OSError(".lnk generation is only supported on Windows")
    if shortcut_path.exists():
        raise FileExistsError(f"快捷方式已经存在，未覆盖：{shortcut_path}")
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = shortcut_path.with_name(f".{shortcut_path.stem}.{uuid4().hex}.lnk")
    powershell_exe = os.environ.get(
        "SystemRoot",
        r"C:\Windows",
    ) + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
    script = (
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$shortcutPath = $env:NIMDA_SHORTCUT_PATH\n"
        "$targetPath = $env:NIMDA_TARGET_PATH\n"
        "$typeDefinition = @'\n"
        "using System;\n"
        "using System.Text;\n"
        "using System.Runtime.InteropServices;\n"
        "namespace NimdaShortcut {\n"
        "  [ComImport, Guid(\"00021401-0000-0000-C000-000000000046\")]\n"
        "  public class ShellLink {}\n"
        "  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid(\"000214F9-0000-0000-C000-000000000046\")]\n"
        "  public interface IShellLinkW {\n"
        "    [PreserveSig] int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cchMaxPath, IntPtr pfd, uint fFlags);\n"
        "    [PreserveSig] int GetIDList(out IntPtr ppidl);\n"
        "    [PreserveSig] int SetIDList(IntPtr pidl);\n"
        "    [PreserveSig] int GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cchMaxName);\n"
        "    [PreserveSig] int SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);\n"
        "    [PreserveSig] int GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cchMaxPath);\n"
        "    [PreserveSig] int SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);\n"
        "    [PreserveSig] int GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cchMaxPath);\n"
        "    [PreserveSig] int SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);\n"
        "    [PreserveSig] int GetHotkey(out short pwHotkey);\n"
        "    [PreserveSig] int SetHotkey(short wHotkey);\n"
        "    [PreserveSig] int GetShowCmd(out int piShowCmd);\n"
        "    [PreserveSig] int SetShowCmd(int iShowCmd);\n"
        "    [PreserveSig] int GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cchIconPath, out int piIcon);\n"
        "    [PreserveSig] int SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);\n"
        "    [PreserveSig] int SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);\n"
        "    [PreserveSig] int Resolve(IntPtr hwnd, uint fFlags);\n"
        "    [PreserveSig] int SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);\n"
        "  }\n"
        "  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid(\"0000010B-0000-0000-C000-000000000046\")]\n"
        "  public interface IPersistFile {\n"
        "    void GetClassID(out Guid pClassID);\n"
        "    [PreserveSig] int IsDirty();\n"
        "    [PreserveSig] int Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);\n"
        "    [PreserveSig] int Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);\n"
        "    [PreserveSig] int SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);\n"
        "    [PreserveSig] int GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);\n"
        "  }\n"
        "  public static class ShortcutWriter {\n"
        "    public static void Save(string shortcutPath, string targetPath) {\n"
        "      IShellLinkW shellLink = (IShellLinkW)new ShellLink();\n"
        "      int hr = shellLink.SetPath(targetPath);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "      hr = shellLink.SetWorkingDirectory(targetPath);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "      IPersistFile persist = (IPersistFile)shellLink;\n"
        "      hr = persist.Save(shortcutPath, true);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "    }\n"
        "  }\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $typeDefinition\n"
        "$shortcutPath = [System.IO.Path]::GetFullPath($shortcutPath)\n"
        "$targetPath = [System.IO.Path]::GetFullPath($targetPath)\n"
        "[NimdaShortcut.ShortcutWriter]::Save($shortcutPath, $targetPath)\n"
    )
    kwargs: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [
                powershell_exe,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env={
                **os.environ.copy(),
                "NIMDA_SHORTCUT_PATH": str(temporary_path),
                "NIMDA_TARGET_PATH": str(target_path),
            },
            **kwargs,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            if not detail:
                detail = f"PowerShell exited with code {proc.returncode}"
            raise OSError(f"写入 .lnk 失败：{detail}")
        # On Windows os.rename is exclusive: another creator cannot be overwritten.
        os.rename(temporary_path, shortcut_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def windows_shortcut_targets(shortcut_paths: list[Path]) -> dict[str, dict[str, Any]]:
    paths = [p for p in shortcut_paths if p.suffix.lower() == ".lnk"]
    if os.name != "nt" or not paths:
        return {}
    powershell_exe = os.environ.get(
        "SystemRoot",
        r"C:\Windows",
    ) + r"\System32\WindowsPowerShell\v1.0\powershell.exe"
    script = (
        "[Console]::InputEncoding = [System.Text.Encoding]::UTF8\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$ErrorActionPreference = 'Stop'\n"
        "$raw = [Console]::In.ReadToEnd()\n"
        "$paths = $raw | ConvertFrom-Json\n"
        "$typeDefinition = @'\n"
        "using System;\n"
        "using System.Text;\n"
        "using System.Runtime.InteropServices;\n"
        "namespace NimdaShortcutReader {\n"
        "  [ComImport, Guid(\"00021401-0000-0000-C000-000000000046\")]\n"
        "  public class ShellLink {}\n"
        "  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid(\"000214F9-0000-0000-C000-000000000046\")]\n"
        "  public interface IShellLinkW {\n"
        "    [PreserveSig] int GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszFile, int cchMaxPath, IntPtr pfd, uint fFlags);\n"
        "    [PreserveSig] int GetIDList(out IntPtr ppidl);\n"
        "    [PreserveSig] int SetIDList(IntPtr pidl);\n"
        "    [PreserveSig] int GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszName, int cchMaxName);\n"
        "    [PreserveSig] int SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);\n"
        "    [PreserveSig] int GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszDir, int cchMaxPath);\n"
        "    [PreserveSig] int SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);\n"
        "    [PreserveSig] int GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszArgs, int cchMaxPath);\n"
        "    [PreserveSig] int SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);\n"
        "    [PreserveSig] int GetHotkey(out short pwHotkey);\n"
        "    [PreserveSig] int SetHotkey(short wHotkey);\n"
        "    [PreserveSig] int GetShowCmd(out int piShowCmd);\n"
        "    [PreserveSig] int SetShowCmd(int iShowCmd);\n"
        "    [PreserveSig] int GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder pszIconPath, int cchIconPath, out int piIcon);\n"
        "    [PreserveSig] int SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);\n"
        "    [PreserveSig] int SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);\n"
        "    [PreserveSig] int Resolve(IntPtr hwnd, uint fFlags);\n"
        "    [PreserveSig] int SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);\n"
        "  }\n"
        "  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid(\"0000010B-0000-0000-C000-000000000046\")]\n"
        "  public interface IPersistFile {\n"
        "    void GetClassID(out Guid pClassID);\n"
        "    [PreserveSig] int IsDirty();\n"
        "    [PreserveSig] int Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, uint dwMode);\n"
        "    [PreserveSig] int Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, bool fRemember);\n"
        "    [PreserveSig] int SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);\n"
        "    [PreserveSig] int GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);\n"
        "  }\n"
        "  public static class ShortcutReader {\n"
        "    public static string Read(string shortcutPath) {\n"
        "      IShellLinkW shellLink = (IShellLinkW)new ShellLink();\n"
        "      IPersistFile persist = (IPersistFile)shellLink;\n"
        "      int hr = persist.Load(shortcutPath, 0);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "      StringBuilder path = new StringBuilder(32768);\n"
        "      hr = shellLink.GetPath(path, path.Capacity, IntPtr.Zero, 0);\n"
        "      Marshal.ThrowExceptionForHR(hr);\n"
        "      return path.ToString();\n"
        "    }\n"
        "  }\n"
        "}\n"
        "'@\n"
        "Add-Type -TypeDefinition $typeDefinition\n"
        "$rows = foreach ($shortcutPath in $paths) {\n"
        "  try {\n"
        "    $target = [NimdaShortcutReader.ShortcutReader]::Read([string]$shortcutPath)\n"
        "    [pscustomobject]@{ path = [string]$shortcutPath; target = [string]$target; error = '' }\n"
        "  } catch {\n"
        "    [pscustomobject]@{ path = [string]$shortcutPath; target = ''; error = [string]$_.Exception.Message }\n"
        "  }\n"
        "}\n"
        "$json = $rows | ConvertTo-Json -Compress\n"
        "[Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($json))\n"
    )
    kwargs: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(
        [powershell_exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        input=json.dumps([str(p) for p in paths], ensure_ascii=False),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        **kwargs,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return {}
    try:
        decoded_stdout = base64.b64decode(proc.stdout.strip()).decode("utf-8")
        raw_rows = json.loads(decoded_stdout)
    except (ValueError, json.JSONDecodeError):
        return {}
    rows = raw_rows if isinstance(raw_rows, list) else [raw_rows]
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        path_s = str(row.get("path") or "")
        if not path_s:
            continue
        out[str(Path(path_s).expanduser().resolve())] = {
            "target_path": str(row.get("target") or ""),
            "target_resolved": bool(row.get("target")),
            "error": str(row.get("error") or ""),
        }
    return out


def windows_shortcut_target(shortcut_path: Path) -> str:
    if os.name != "nt" or shortcut_path.suffix.lower() != ".lnk":
        return ""
    info = windows_shortcut_targets([shortcut_path]).get(str(shortcut_path.expanduser().resolve())) or {}
    return str(info.get("target_path") or "")
