# nimda

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
