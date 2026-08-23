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

Run all Python tests and JavaScript syntax checks:

```powershell
.\scripts\test.cmd
```

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
