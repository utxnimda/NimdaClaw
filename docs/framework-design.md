# nimda Framework Design

`nimda` is organized as one workspace with three parallel concerns:

- `apps/`: code and runnable applications.
- `config/`: editable configuration.
- `data/`: persisted data and runtime output.

Each concern is split again into:

- `framework/`: app shell, shared behavior, global config, and framework runtime data.
- `features/<feature-id>/`: one top-level web tab / functional module.

This keeps the browser UI, backend APIs, config files, and persisted data aligned by feature.

## Directory Contract

```text
nimda/
  apps/
    framework/
      backend/                 Starlette app, CLI, shared layout/config helpers
      frontend/                Vue shell, tab host, theme, shared styles
    features/
      collection-detail/
        backend/               作品数据 API/service code
        frontend/              作品数据 tab registration and UI behavior
        tests/
      collection-info/
        backend/               收集情况 API/service code
        frontend/              收集情况 tab registration and UI behavior
        tests/
      media-directory-organizer/
        backend/               Read-only planning, classifiers and confirmed execution
        frontend/              Directory preview and work landing UI
        tests/
  config/
    framework/
      app.yaml                 Global app config: tab labels, feature order
    features/
      collection-detail/
        config.yaml            作品数据 DB path and enum config
      collection-info/
        config.yaml            收集情况 finish-dir and DB/history paths
      media-directory-organizer/
        config.yaml            Organizer inference and classification settings
  data/
    source/                    Original hand-maintained source material
    framework/
      logs/
      runtime/
    features/
      collection-detail/
        db/
        history/
        index/
      collection-info/
        db/
        history/
```

## Frontend

The frontend entry is `apps/framework/frontend/src/main.js`.

The Vue shell in `apps/framework/frontend/src/App.js` owns:

- top-level layout;
- theme selector;
- tab bar placement;
- shared DOM anchors required by the current feature modules.

Feature tabs are served from:

- `/features/collection-detail/`
- `/features/collection-info/`
- `/features/media-directory-organizer/`

Each feature frontend registers itself through `window.JpTvBrowseFeatureRegistry`. The shell reads registered features, merges labels/order from `/api/config`, and switches tabs by each feature's `tabId` and `viewId`.

The current Vue migration is incremental: Vue owns the app shell, while legacy tab behavior is still mounted from `apps/framework/frontend/src/legacy/shell.js`. Future work can move one feature at a time into Vue components without changing the workspace layout.

## Development Boundaries

The project boundary is data-first:

- Python backend owns data reading, parsing, normalization, validation, persistence, backups, file naming, path safety, and external communication.
- Web/Vue owns data display, user interaction, page composition, tab routing, view-local filters/sort/search, and temporary editing state.
- Web/Vue must not become the canonical data-processing layer.
- Feature data logic must be reusable by other frontends, such as a desktop app, CLI, or another web framework.

In practice:

- Vue receives JSON view models from the backend and renders them.
- Vue submits user intent to the backend as JSON commands or patches.
- Python decides how those commands map to YAML files, history snapshots, enum changes, collection-info records, or external directories.
- Python owns API compatibility, so replacing Vue should not require changing YAML parsing or persistence code.
- Client-side sorting/filtering is allowed for display convenience, but persisted meaning must be computed or validated by Python.
- A new frontend should be able to call the same backend APIs, or a thin adapter around the same Python services, without copying data rules into the frontend.

Examples:

- Adding a row: Vue collects blank/default UI fields; Python chooses the current-year DB file and writes the YAML.
- Renaming an enum: Vue sends `old -> new`; Python updates config and synchronizes existing data.
- Collection years: Vue renders checkboxes; Python scans the finish directory and normalizes saved years.
- Link index generation: Vue edits work-directory and press-subdirectory mappings; Python builds the configured directory tree and writes `.lnk` shortcuts.
- New display mode: Vue can change layout freely; Python payload contracts should remain stable or versioned.

## Backend

The backend app is built by `work_catalog_yaml.jp_tv.browse_app`.

HTTP composition and scheduling are separated from feature behavior:

- `jp_tv.browse_app`: routes, static mounts, local-origin security and lifespan.
- `jp_tv.browse_api`: response builders and existing HTTP error contracts.
- `api_runtime`: request parsing, upload resource cleanup and owned worker queues.

Disk-backed API operations use one worker per app, preserving their ordering
without blocking the ASGI event loop. Read-only provider suggestions have a
separate two-worker queue. `/api/health` bypasses both queues and reports
`ready`, `busy` or `stopping`, with running and queued counts. Database reads
still wait behind active disk work; the queues are not a general background-job
API and do not return durable job IDs.

Cancelling a pending request cancels its queued work. An operation that has
started is allowed to finish. Shutdown rejects new work with HTTP 503, cancels
pending jobs, and waits for started jobs before desktop ownership is released,
including when the ASGI thread has already exited. These queues are in-process;
they neither coordinate multiple application processes nor recover after a
forced process kill or power loss.

The framework backend owns:

- Starlette app creation;
- static mounts;
- API routing;
- workspace layout helpers;
- compatibility wrappers for old import paths.

Feature backend code lives under `apps/features/<feature-id>/backend`:

- `collection_detail`: browse payloads, YAML save, enum edits.
- `collection_info`: collection completion records and finish-dir year scanning.
- `media_directory_organizer`: source inference, release-group classifiers,
  preview/apply, and coordination of database records, media and shortcuts.

Organizer internals have narrower responsibilities:

- `service`: read-only planning and source-to-target classification.
- `execution`: confirmation, execution-time validation, moves, rollback and
  empty-source cleanup. `service.apply_plan` remains an import-compatible alias.
- `plan_identity`: deterministic plan IDs used by planning and execution.
- `landing_drafts`: registration/repair inputs and source-title suggestions.
- `landing_catalog`: catalog references, press identities and matching candidates.
- `landing`: database/media/shortcut transaction coordination and retry receipts.

`landing_catalog.CatalogReadSession` shares parsed entries and lazily built press
records within one read-only preview/normalization phase. A record's content hash
is computed from the same bytes that produced its entries; each incoming catalog
reference still checks its own work name and expected hash, even on a cache hit.
Sessions never cross a database write or preview-to-apply boundary. Execution
continues to revalidate current state and compare expected pre-write hashes.

Within `collection_detail`, `resource_tree` holds pure tree summaries and search
projections. `resource_cache` writes each scan into a new generation and publishes
its manifest only after all nodes are ready. Readers and writers share a process
lock; successful publication cleans only the replaced generation. Scan failures
retain the previous snapshot. Root order, exclusions and scan settings are part
of cache identity; old-format or mismatched caches require a rescan, not silent
reuse with different `root:N` mappings. Media files are never deleted by cache
generation cleanup.

Shared backend building blocks are kept independent of the web routes:

- `work_catalog_yaml.layout`: workspace/config path resolution and backend discovery.
- `work_catalog_yaml.paths`: validated export paths shared by both materialization commands.
- `work_catalog_yaml.persistence`: staged writes and rollback shared by collection features.
- `work_catalog_yaml.yaml_cache`: bounded content-keyed YAML parse reuse, consumed
  by `yaml_io`. File reads remain fresh and every consumer gets an isolated copy;
  failed parses are not cached. Its byte limit estimates retained objects rather
  than bounding total process memory.

`apps/framework/tests` covers these shared contracts and runs through the same
`scripts/test.ps1` entry as feature tests. Feature modules may depend on these
helpers; generic persistence and path handling must not depend on feature modules.

`work_catalog_yaml.layout.ensure_feature_backend_paths()` currently exposes feature backend packages to the framework app. This is a compatibility bridge; the long-term direction is to make feature backend modules regular package dependencies or load them through an explicit feature registry.

The desktop launcher and packaging spec share `backend_roots()` discovery.
Packaged feature code and static resources are collected from the feature
directories; adding a feature still requires explicit API and UI registration.

PowerShell source entrypoints share `scripts/lib/workspace.ps1` for backend
discovery, explicit source roots, bytecode suppression and scoped environment
setup. It restores environment variables (including absent values) and the
working directory in `finally`; the native process exit status remains intact.
Regression tests compare its discovery order with Python's `backend_roots()`
and cover both Windows PowerShell and PowerShell 7 when available. The shared
benchmark entrypoint uses the same setup, including when launched outside the
repository directory.

## Config

Global app config lives in `config/framework/app.yaml`.

It controls feature labels and order:

```yaml
app:
  features:
    - id: collection-detail
      label: 作品数据
      order: 10
    - id: collection-info
      label: 收集情况
      order: 20
```

Feature config lives in `config/features/<feature-id>/config.yaml`.

Rules:

- Framework config controls the app shell and page tabs.
- Feature config controls only that feature's behavior.
- Feature-specific enum values belong to the feature config unless they are truly global.
- Paths should point into `data/features/<feature-id>/...` by default.
- Relative paths in workspace feature configuration resolve against the shared
  workspace, not the shell's current directory. Absolute paths remain supported.

## Data

Feature data lives in `data/features/<feature-id>/`.

Current feature data:

- `collection-detail/db`: JP TV YAML database files.
- `collection-detail/history`: backups written before YAML saves and enum rename syncs.
- `collection-detail` link-index mappings live inside each work YAML row:
  `attributes/data/path` stores the work parent relative path, and each press item stores
  `press_path` under `attributes/data/collectioned`.
  The shortcut directory tree is a backend-generated view aggregated from all work YAML files.
  Link mapping is edited from the collection-detail list, on each row's press-summary item.
  The link-index tab is a read-only tree/check view over the full configured DB.
- `collection-info/db/collection-info.yaml`: collection completion records.
- `collection-info/history`: backups written before collection-info saves.

New rows in `collection-detail` are saved to the current calendar year's DB file, for example `[JP][TVInfo][2026].yaml`. This uses the current date, not the row content date.

## Adding A Feature

To add a new tab:

1. Create `apps/features/<feature-id>/frontend/index.js`.
2. Register the frontend module with `window.JpTvBrowseFeatureRegistry`.
3. Create `apps/features/<feature-id>/backend/<package_name>/` if the feature needs APIs.
4. Add `config/features/<feature-id>/config.yaml`.
5. Add `data/features/<feature-id>/db` and `data/features/<feature-id>/history` if the feature persists data.
6. Add the feature to `config/framework/app.yaml`.
7. Mount its static frontend and API routes in the framework backend, or move that mounting into a future feature registry.

## Current Design Notes

- `collection-detail` is the 作品数据 feature.
- `collection-info` is the 收集情况 feature.
- The app shell is Vue, but feature internals are still plain JavaScript modules.
- `apps/framework/frontend/src/legacy/shell.js` is transitional and should shrink as features become Vue components.
- Runtime logs, `__pycache__`, and local process helpers are not part of the architecture and should be ignored or moved out of source-controlled project files.
