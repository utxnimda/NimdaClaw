# NimdaClaw

`nimda` is the workspace root. The app is split by framework and feature tabs
across code, config, and data.

```text
nimda/
  apps/
    framework/
      backend/                Starlette app, CLI, shared parsing/yaml helpers
      frontend/               Vue app shell, tabs, theme, shared styles
    features/
      collection-detail/
        backend/              "作品数据" API, payload, save, enum edits
        frontend/             "作品数据" tab module
        tests/
      collection-info/
        backend/              "收集情况" API/service
        frontend/             "收集情况" tab module
        tests/
      media-directory-organizer/
        backend/              Preview, classification, media/DB/shortcut coordination
        frontend/             "目录整理" tab module
        tests/
  config/
    framework/
      app.yaml                Global app shell config: feature labels/order
    features/
      collection-detail/
        config.yaml           DB path and enum config
      collection-info/
        config.yaml           Finish-dir scan and collection-info DB config
  data/
    source/                   Original hand-maintained source files
    framework/
      logs/
      runtime/
    features/
      collection-detail/
        db/
        history/
      collection-info/
        db/
        history/
```

Rules:

- Add a new tab as the same feature id under `apps/features/`,
  `config/features/`, and `data/features/`.
- Keep framework shell behavior under `apps/framework`.
- Keep page-specific backend and frontend behavior under `apps/features/<feature-id>`.
- Keep global tab labels/order in `config/framework/app.yaml`.
- `apps/framework/frontend/src/main.js` is the Vue entry. Existing feature modules are
  mounted by the Vue shell and can be migrated into Vue components one feature at a time.
- Keep data rules in Python services. Vue/Web is only the presentation and page-composition
  layer, so another frontend can reuse the same data-processing code later.
- `collection-detail` stores link-index mappings in the work YAML rows themselves
  (`attributes/data/path` and each press item's `press_path`); Python aggregates those rows
  into the configured shortcut directory tree. Editing happens in the collection list's
  press-summary items; the index tab is display/check only.

See `docs/framework-design.md` for the framework/feature architecture contract.

## Local Development

Install the backend and Web dependencies from the workspace root:

```powershell
python -m pip install -e ".\apps\framework\backend[web]"
```

`apps/framework/backend/pyproject.toml` is the backend dependency source of
truth; there is no separate backend `requirements.txt` to keep in sync.

Start the local application:

```powershell
.\scripts\run.cmd
```

Build the portable Windows desktop application:

```powershell
.\scripts\build-desktop.cmd -Python "D:\SoftIDE\Python\python.exe"
```

The results are `dist/Nimda/Nimda.exe` and `dist/Nimda-Windows-x64.zip`. The
package contains no database or normal feature-config copy;
`dist/Nimda/nimda-desktop.yaml` points it at the original shared workspace's
`config` and `data` directories. The desktop window owns the embedded Uvicorn
server lifecycle, so closing the application also closes its listener.
See `docs/desktop-app.md` for packaging and source-mode launch details.

Every desktop build now verifies its Python modules, frontend resources, default
configuration, shared-workspace bootstrap and ZIP contents against the checkout.
To check an existing package without launching it:

```powershell
.\build\desktop-venv\Scripts\python.exe .\scripts\verify-desktop.py --workspace "E:\Project\nimda"
```

See `docs/review-2026-09-20.md` for the latest review, fixes and verification limits.

Run all framework/feature Python tests, JavaScript behavior tests and syntax checks:

```powershell
.\scripts\test.cmd
```

Run the read-only API and synthetic resource/matching/frontend benchmarks:

```powershell
.\scripts\benchmark.cmd -Python "D:\SoftIDE\Python\python.exe" -Rounds 3
```

The API benchmark reads the configured database. Resource scans use temporary
fixtures; no real media scan, database write or shortcut creation is performed.
`-SkipJavaScript` is available for test/benchmark runs without Node.js.

If Python is not on `PATH`, pass its executable explicitly:

```powershell
.\scripts\test.cmd -Python "D:\SoftIDE\Python\python.exe"
.\scripts\run.cmd -Python "D:\SoftIDE\Python\python.exe"
```

The Web application listens on `127.0.0.1` by default. Binding a non-loopback
address is rejected unless `-AllowRemote` (CLI: `--allow-remote`) is supplied
explicitly; remote use should also have network-level access controls.

Feature history snapshots and `data/features/**/db/index/` are generated local
artifacts. They are ignored by Git; canonical collection mappings remain in the
yearly collection-detail YAML files.

Workspace-configured relative paths are resolved against the shared workspace,
independently of the current terminal directory. Renaming the checkout does not
change its layout. Desktop runtime and packaging use the same backend discovery
in `work_catalog_yaml.layout`; feature frontend resources are discovered by the
packaging script under `apps/features/*/frontend`.

Source launch, organizer, test and benchmark entrypoints use
`scripts/lib/workspace.ps1` to discover all feature backends and pin the source
workspace. The helper restores the caller's environment and working directory
on success or failure, so a packaged app's environment cannot redirect a source
run to another configuration/data directory.
