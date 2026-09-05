# Nimda Windows desktop application

The desktop build uses the installed Microsoft Edge WebView2 Runtime for the
window and runs Uvicorn inside the same application process. It does not launch
`scripts/run.cmd`, PowerShell, or a second persistent server process.

## Build

From the workspace root:

```powershell
.\scripts\build-desktop.cmd -Python "D:\SoftIDE\Python\python.exe"
```

By default the generated desktop configuration points back to the workspace
being built. A different shared workspace can be selected at build time:

```powershell
.\scripts\build-desktop.cmd `
  -Python "D:\SoftIDE\Python\python.exe" `
  -SharedWorkspace "E:\Project\nimda"
```

The first build creates `build/desktop-venv` and installs the pinned desktop
build dependencies. The portable application is generated at:

```text
dist/Nimda/Nimda.exe
dist/Nimda-Windows-x64.zip
```

Keep the complete `dist/Nimda` directory together. The package deliberately
contains neither the normal `config` tree nor any application database. Instead,
`nimda-desktop.yaml` beside `Nimda.exe` selects the shared workspace:

```yaml
version: 1
paths:
  workspace_root: 'E:/Project/nimda'
```

The desktop app then uses `<workspace_root>/config` and
`<workspace_root>/data`, exactly like a source-mode launch. Edit this path if the
original workspace moves or the package is copied to another computer. Relative
paths are resolved from the directory containing `nimda-desktop.yaml`.

The application UI is still loaded from the packaged `apps` resources. Normal
feature configuration, collection-detail YAML files, collection-info records,
indexes, caches, history, logs, and runtime state all remain in the selected
shared workspace. Rebuilding the desktop package therefore cannot overwrite the
database. External media and shortcut paths remain the absolute drive paths
configured in the shared YAML files.

Because source mode and desktop mode now edit the same files, do not save from
both applications at the same time.

## Lifecycle

- The desktop app binds only to `127.0.0.1`.
- It prefers port 8765 and automatically uses a free system port if occupied.
- Only one desktop instance can run at a time.
- Closing the final window rejects new API work, cancels queued operations, and
  waits for already-started disk/provider operations before releasing the
  listener and desktop instance lock. Waiting also applies if the Web server
  thread has already exited unexpectedly. A long disk operation can therefore
  delay final process exit; it is not forcibly interrupted partway through a save
  or move. Forced process termination and power loss are outside this guarantee.
- Disk APIs run serially in an owned worker; provider suggestions have a separate
  queue. `/api/health` remains responsive during work and reports running/queued
  counts. The desktop server and workers remain within the same process.
- Runtime state is written to
  `<workspace_root>/data/framework/runtime/desktop.json` while the app is open
  and removed during normal shutdown.

For source-mode desktop development, install the `desktop` extra and run:

```powershell
.\scripts\run-desktop.cmd -Python "D:\SoftIDE\Python\python.exe"
```
