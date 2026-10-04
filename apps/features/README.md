# Browser Features

Each top-level browser tab is a feature directory.

The application provides three tabs: `collection-detail` for work records, shortcut
indexes and resource-library browsing, and `collection-info` for collection
status, plus `directory-organizer` for disk-first per-child organizing previews
and confirmed execution. The replacement organizer does not restore the retired
`media-directory-organizer` APIs or maintain a separate work database.

Feature modules register themselves through `window.JpTvBrowseFeatureRegistry`:

```js
window.JpTvBrowseFeatureRegistry.register({
  id: "example",
  label: "示例功能",
  tabId: "tab-example",
  viewId: "example-view",
  order: 30,
  init(ctx) {},
  activate(ctx) {},
  deactivate(ctx) {},
  refreshAfterConfig(ctx) {},
});
```

The Vue shell starts from `apps/framework/frontend/src/main.js`. Shared
`appearance.js` manages theme/font preferences and `feature-host.js` manages
registration, tab switching and lifecycle callbacks. Feature directories own
their page-specific state and behavior: the collection table controller lives in
`collection-detail/frontend/table-controller.js`, while its sibling `index.js`
owns shortcut and resource-library views. Shared modules receive explicit
callbacks and do not import collection-table state.

Reusable primitives belong to framework-owned `common`, not a sibling feature:
`apps/framework/frontend/src/common/` and
`apps/framework/backend/work_catalog_yaml/common/`. Features use
`window.NimdaCommon` for registration, escaping, preferences and request helpers;
backend adapters use `common.http.error_response` so handled exceptions retain
their diagnostic stack in the operation log. Domain rules remain in the feature.

Use `ctx.fetchJson` for API operations and `ctx.setStatus` for visible results /
validation errors. Both feed the shared operation-details panel. Persistent
logs live outside feature databases under `data/framework/logs/operations/`;
see `docs/operation-progress.md` for history and raw-log APIs.

Tab labels and order come from the workspace app config first:
`nimda/config/framework/app.yaml` → `app.features`.
The feature module's `label` and `order` are only defaults.
