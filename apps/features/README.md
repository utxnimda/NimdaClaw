# Browser Features

Each top-level browser tab is a feature directory.

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

The Vue shell starts from `apps/framework/frontend/src/main.js`. For now it mounts
the legacy shell from `apps/framework/frontend/src/legacy/shell.js`, which owns shared
state and tab switching while feature directories own their page-specific UI and behavior.

Tab labels and order come from the workspace app config first:
`nimda/config/framework/app.yaml` → `app.features`.
The feature module's `label` and `order` are only defaults.
