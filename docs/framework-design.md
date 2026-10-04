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
      directory-organizer/
        backend/               Per-child plans, strategy classes, safe execution
        frontend/              Editable previews and explicit confirmations
        tests/
  config/
    framework/
      app.yaml                 Global app config: tab labels, feature order
    features/
      collection-detail/
        config.yaml            作品数据 DB path and enum config
      collection-info/
        config.yaml            收集情况 finish-dir and DB/history paths
  data/
    source/                    Original hand-maintained source material
    framework/
      logs/
      runtime/
    features/
      collection-detail/
        db/
        history/
        db/index/              Derived shortcut index, not a work database
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
- the feature-view components and their lifecycle coordination.

Feature tabs are served from:

- `/features/collection-detail/`
- `/features/collection-info/`
- `/features/directory-organizer/`

Each feature frontend registers itself through `window.JpTvBrowseFeatureRegistry`. The shell reads registered features, merges labels/order from `/api/config`, and switches tabs by each feature's `tabId` and `viewId`.

Vue owns the app shell. The collection-page template and its DOM anchors live in
`collection-detail/frontend/view.js`, and table behavior lives in
`collection-detail/frontend/table-controller.js`. Shared `appearance.js` and
`feature-host.js` receive explicit callbacks/context and contain no collection-table state.

Feature lifecycle methods must preserve unsaved drafts when switching tabs or
refreshing enum choices. `collection-info` uses lifecycle/load generations to
ignore stale responses after deactivation, disposal or a newer load. Explicit
reload still replaces a draft when it succeeds; failed reloads retain it.
Saved years missing from the current directory scan remain visible, selectable
and marked as unavailable, rather than being silently removed.

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
- Collection years: Python scans the finish directory for choices and separately
  normalizes saved years. A missing/offline directory never deletes saved years;
  saving records does not need another directory scan. Vue renders both available
  and previously recorded choices.
- Link index generation: Vue edits work-directory and press-subdirectory mappings; Python builds the configured directory tree and writes `.lnk` shortcuts.
- New display mode: Vue can change layout freely; Python payload contracts should remain stable or versioned.

## Backend

The backend app is built by `work_catalog_yaml.jp_tv.browse_app`.

HTTP composition and scheduling are separated from feature behavior:

- `jp_tv.browse_app`: routes, static mounts, local-origin security and lifespan.
- `collection_detail.web` / `collection_info.web` / `directory_organizer.web`: feature-owned HTTP contracts.
- `jp_tv.browse_api`: legacy import bridge only, with no business implementation.
- `api_runtime`: request parsing, upload resource cleanup and owned worker queues.

Disk-backed API operations use one worker per app, preserving their ordering
without blocking the ASGI event loop. A separate two-worker network queue is
reserved for future read-only provider integrations; no current production route
submits to it, and it starts no worker threads without a job. `/api/health`
bypasses both queues and reports
`ready`, `busy` or `stopping`, with running and queued counts. Database reads
still wait behind active disk work; the queues are not a general background-job
API and do not return durable job IDs.

Each queue admits at most 64 unfinished futures (running plus queued) by default.
Saturation returns HTTP 503 with `code: api-queue-full` and `Retry-After: 1`.
Clients do not automatically retry mutations. This bounds admitted unfinished
work, not total process memory or the executor's internal cancelled work items.

Resource-library scanning writes a cache and therefore uses
`POST /api/collection-detail/resource-libraries/scan` with a JSON body (`{}` is
valid). It is subject to the same origin checks as other mutations; GET/HEAD
return 405 and never scan. Read-only resource browsing remains GET-based.

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
- `directory_organizer`: disk-first child plans; pure organizer classes; safe media
  execution composed with shared catalog edits and scoped shortcut generation.
- `catalog_repository`: versioned work/press data independent of page and disk views.
- `catalog_inventory`: DB binding relationships using shared, read-only disk facts.
- `library_status`: read-only page aggregation with separate DB/tree counts.
- `work_catalog_yaml.storage`: generic filesystem and Windows shortcut adapters.


Within `collection_detail`, `resource_tree` holds pure tree summaries and search
projections. `resource_cache` writes each scan into a new generation and publishes
its manifest only after all nodes are ready. Readers and writers share a process
lock; successful publication cleans only the replaced generation. Scan failures
retain the previous snapshot. Root order, exclusions and scan settings are part
of cache identity; old-format or mismatched caches require a rescan, not silent
reuse with different `root:N` mappings. Media files are never deleted by cache
generation cleanup.

Tree summaries and cached payloads share one projection. Lazy nodes keep their
unloaded counts, and `has_children` includes files as well as subdirectories.
Compatible legacy cache nodes are reprojected on read, without a rescan/write.

The two directory views have different source contracts:

- The **index directory** joins current work/press DB records with actual shortcut
  directory observations and target availability. Derived index/scan caches are
  replaceable accelerators, never a second authority for work identity. DB-only
  expected links and disk-only unassociated links remain distinguishable.
- The **resource-library directory** is a physical-media scan tree only. It does
  not insert DB-only works or turn directory names into work records. Its saved
  snapshot remains usable while offline, with the scan time shown; an explicit
  rescan updates physical observations.

Refreshing an index view never registers works or saves inferred bindings.
Shortcut creation is a separate saved-DB-only command, not an application of the
display tree's unassociated observations or suggestions.

Shared backend building blocks are kept independent of the web routes:

The `common` boundary lives **inside `framework`**, not beside it: framework is
the reusable foundation and features depend on it, while common must never
import a feature. It does not become a third application or packaging root.

- `apps/framework/backend/work_catalog_yaml/common/`: HTTP error responses,
  operation-log storage, common `[date/time][level]` formatting, daily application logging and safe result projections. Web adapters share caught
  exception reporting without importing sibling features.
- `apps/framework/frontend/src/common/`: feature registration primitives, HTML
  escaping, local preferences, request/error handling and operation-result
  projections. The shell loads these before feature scripts.
- Framework coordinators (`operation_progress`, `api_runtime`, `feature-host`
  and `operation-center`) compose common primitives; domain catalog matching,
  shortcut rules and collection state remain feature-owned.

- `work_catalog_yaml.layout`: workspace/config path resolution and backend discovery.
- `work_catalog_yaml.paths`: validated export paths shared by both materialization commands.
- `work_catalog_yaml.persistence`: staged writes and rollback shared by collection features.
- `work_catalog_yaml.input_validation`: strict non-negative record indices shared
  by collection edits/deletes and DB references; booleans and floats are
  rejected instead of silently selecting a different row through integer coercion.
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
    - id: directory-organizer
      label: 目录整理
      order: 30
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

- `collection-detail/db`: JP/KR work collection YAML database files.
- `collection-detail/history`: backups written before YAML saves and enum rename syncs.
- `collection-detail` link-index mappings live inside each work YAML row:
  `attributes/data/path` stores the work directory (absolute or configured-root-relative), and each press item stores
  `press_path` under `attributes/data/collectioned` or each continuation's `collectioned`.
  The shortcut directory tree is a backend-generated view aggregated from all work YAML files.
  Link mapping is edited from the collection-detail list, on each row's press-summary item.
  The link-index tab is a read-only tree/check view over the full configured DB.
- `collection-info/db/collection-info.yaml`: collection completion records.
- `collection-info/history`: backups written before collection-info saves.
- `directory-organizer/history`: append-only JSONL media execution receipts;
  not a work database. The organizer shares collection-detail configuration.

New rows in `collection-detail` are saved to the current calendar year's DB file, for example `[JP][TVInfo][2026].yaml`. This uses the current date, not the row content date.

Broadcast dates use compact `YYYYMMDD` strings in YAML and read API payloads.
The collection table displays and edits them as `YYYY-MM-DD`; filters accept
both forms and sorting compares compact keys. New writes normalize date values
through `jp_tv.dates.normalize_air_date`. Unknown `00`/`X` components and blank
values are retained, not guessed. Changed/new dates are calendar-validated;
unchanged historical errors do not block unrelated full-table edits.

`scripts/normalize-catalog-dates.py` previews format-only repairs by default;
`--apply` backs up changed files and uses the shared catalog lock/atomic writes.
It preserves unrelated YAML bytes and reports invalid dates/reversed ranges.
Files with YAML aliases are rejected to avoid modifying shared non-date data.

## Three-layer data chain

`页面展示聚合 → DB 数据 → 硬盘数据` is a dependency direction, not an
automatic import or synchronization pipeline.

1. Presentation/application: `collection_detail.web`, `payload`,
   `work_detail`, `library_status` and feature frontend modules compose
   view models and user commands. `collection_info.web` owns completion APIs.
2. Database: `CatalogRepository` owns versioned YAML reads and work/press DTOs;
   `CatalogInventoryRepository` joins saved bindings with explicit disk facts.
   Save/repair services own validated, backed-up writes. DB-only records remain valid.
3. Disk: `work_catalog_yaml.storage` owns ordinary-directory checks and Windows
   shortcut I/O. This shared layer never imports feature or presentation modules.

These data sources are deliberately different:

| Source | Meaning | Authority |
| --- | --- | --- |
| `collection-detail/db/[JP\|KR][TVInfo][year].yaml` | Work records, dates, collection/press records, work `path` and `press_path` | Canonical |
| `collection-info/db/collection-info.yaml` | Manually recorded annual completion state | Canonical, not a work list |
| Resource scan manifest/node cache | Observed physical directories/files at scan time | Derived; may be stale/offline |
| `collection-detail/db/index/link-index.yaml` | Shortcut/index projection of work records | Derived, not another catalog |
| Media directories and `.lnk` files | Actual physical resources and generated references | Disk facts, not implicit DB records |

A work record count, press record count, directory count and file count are
separate metrics. Multiple works/broadcast seasons may share one directory;
one work may have multiple press directories. Never infer one count from another.

`GET /api/collection-detail/library-status` reads these relationships without
scanning media, rewriting DBs, refreshing caches or creating shortcuts:

- `both`: a saved binding and ordinary directory exist.
- `db-only`: a saved binding is absent on disk while its resource root is online.
- `unbound`: the DB has no directory binding; disk existence is unknown.
- `offline`: root/target cannot be inspected; this is not proof of absence.
- `unsafe/invalid`: unsafe or out-of-scope bindings; never traverse them.
- `observed-unbound`: a cached, currently present directory has no saved path
  binding. It may still have an unbound same-name DB record; no automatic matching
  or insertion is performed.

Shortcut creation uses only saved DB `path/press_path`, metadata and configured
naming templates. Disk checks determine availability, not work identity. A missing
binding is repaired/saved explicitly before generation; generation itself never
adds work records or backfills inferred bindings. The UI incrementally creates
missing shortcuts, retains existing files, and reports conflicts/skips. Legacy
full-replacement requests must pass stricter preflight and confirmations.

The old directory-organizer feature, its routes/config/CLI/classifiers and
one-time Korean shortcut reorganization script were removed on 2026-10-03.
The replacement `directory-organizer` is a separate feature. Historical review
documents do not imply the retired APIs still exist.

## Replacement directory organizer

The selected root's actual direct children define the scope. Each child has an
independent draft, disk fingerprint, complete DB record edits and expiring plan.
DB-only works never become organizing tasks. A changed draft needs a new preview
and confirmation; a batch reuses the single-child executor sequentially.

`BaseOrganizer` supplies overridable directory filters and pure layout helpers;
generic, VCB, Jsum and manual strategies never move media or save DB records.
All automatic fields can be edited. Multiple DB works can share one physical
release directory; one work can keep several independent press directories.

`collection_detail.catalog_edit_service` exposes full versioned records and
delegates every save to the original collection-list saver. Full records retain
extension fields. `collection_detail.shortcut_service` delegates scoped commands
to the same link-index planner/writer used by the index page. Scoped generation
does not replace the full index cache or touch unrelated shortcuts.

The execution coordinator checks disk/DB versions and cross-plan conflicts before
moving files. Overlapping edits to one work are three-way merged; conflicting
changes stop the batch. Only same-volume, ordinary, non-linked files are allowed,
and existing files are never overwritten. Changes to unselected press bindings
are rejected rather than silently retargeting other releases.

Media moves have append-only intent/result receipts. Before DB commit, failures
attempt to roll back this child's moves; after a confirmed or uncertain DB commit,
media is not blindly rolled back. This is not a distributed transaction or an
automatic crash-recovery mechanism. See the feature README for operational limits.

After media and DB synchronization, scoped shortcuts are previewed against the
saved authoritative data and require separate confirmation. The framework's
queue, operation-details/log viewer and native folder-picker bridge are reused.

## Adding A Feature

To add a new tab:

1. Create `apps/features/<feature-id>/frontend/index.js`.
2. Register the frontend module with `window.JpTvBrowseFeatureRegistry`.
3. Create `apps/features/<feature-id>/backend/<package_name>/` if the feature needs APIs.
4. Add `config/features/<feature-id>/config.yaml` only when independent feature settings are needed.
5. Add `data/features/<feature-id>/db` and `data/features/<feature-id>/history` if the feature persists data.
6. Add the feature to `config/framework/app.yaml`.
7. Mount its static frontend and API routes in the framework backend, or move that mounting into a future feature registry.

## Current Design Notes

- `collection-detail` is the 作品数据 feature.
- `collection-info` is the 收集情况 feature.
- `directory-organizer` is the 目录整理 feature, sharing canonical catalog/shortcut services.
- The app shell is Vue, but feature internals are still plain JavaScript modules.
- Framework frontend code owns shell/appearance/feature hosting; collection-table business behavior is owned by the collection-detail feature.
- Runtime logs, `__pycache__`, and local process helpers are not part of the architecture and should be ignored or moved out of source-controlled project files.
