"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { withCommonRuntime } = require("../../../framework/tests/frontend_runtime_fixture");

const FRONTEND = path.resolve(__dirname, "../frontend/index.js");
const INDEX_SLOT = "collection-detail-link-index-slot";
const RESOURCE_SLOT = "collection-detail-resource-slot";
const flush = () => new Promise((resolve) => setImmediate(resolve));

function setup(options = {}) {
  const listeners = new Map();
  const slots = new Map([INDEX_SLOT, RESOURCE_SLOT].map((id) => [id, {
    innerHTML: "", contains(target) { return target.owner === id; }, querySelector() { return null; },
  }]));
  const document = {
    getElementById(id) { return slots.get(id) || null; },
    querySelectorAll() { return []; },
    addEventListener(type, callback) { if (!listeners.has(type)) listeners.set(type, []); listeners.get(type).push(callback); },
    removeEventListener(type, callback) { listeners.set(type, (listeners.get(type) || []).filter((item) => item !== callback)); },
    body: { classList: { add() {}, remove() {} } },
  };
  const requests = [];
  const statuses = [];
  const confirmations = [];
  const window = {
    document,
    confirm(message) { confirmations.push(message); return options.confirm !== false; },
  };
  const context = {
    fetchJson(url, requestOptions) {
      return new Promise((resolve, reject) => requests.push({
        url, options: requestOptions,
        respond(data, status = 200) { resolve({ res: { ok: status >= 200 && status < 300, status }, data }); },
        reject,
      }));
    },
    setStatus(message, isError) { statuses.push({ message, isError }); },
  };
  const original = fs.readFileSync(FRONTEND, "utf8");
  const marker = "  root.register({";
  assert.ok(original.includes(marker));
  const source = original.replace(marker, `
    window.shortcutHooks = {
      generate: generateLinkIndexFiles, resourceSummary: resourceScanSummaryText,
      state: function () { return linkIndexState; },
      notice: function () { return linkIndexOperationNotice; },
      busy: function () { return linkIndexFileGenerationBusy; },
      setState: function (value) {
        featureActive = true; activeSubtab = "index"; linkIndexState = value;
        linkIndexLoading = false; linkIndexOperationNotice = null;
        renderLinkIndexPanel();
      },
    };
` + marker);
  vm.runInNewContext(withCommonRuntime(source), {
    window, document, console, URLSearchParams,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  }, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features[0];
  feature.init(context);
  const initialState = { ok: true, marker: "original", plan_summary: { total: 8 }, items: [] };
  window.shortcutHooks.setState(initialState);
  return {
    ...window.shortcutHooks, feature, context, initialState, requests, statuses, confirmations,
    html() { return slots.get(INDEX_SLOT).innerHTML; },
    deactivate() { feature.deactivate(context); },
    click(action = "generate-files") {
      const target = {
        owner: INDEX_SLOT,
        closest(selector) { return selector === "[data-link-index-action]" ? this : null; },
        getAttribute(name) { return name === "data-link-index-action" ? action : null; },
      };
      for (const callback of listeners.get("click") || []) callback({ target, preventDefault() {} });
    },
  };
}

function preview(overrides = {}) {
  return {
    ok: true, file_generation: {
      incremental: true, plan_id: "reviewed-plan",
      creatable: 2, conflict_count: 0, already_exists_count: 3,
      skipped_unbound_count: 4, skipped_missing_target: 5, skipped_unsafe_count: 6,
      output_roots: ["R:/Links/Animation", "R:/Links/Drama"], ...overrides,
    },
  };
}

function requestBody(request) {
  assert.equal(request.url, "/api/collection-detail/link-index/generate-files");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.headers["Content-Type"], "application/json; charset=utf-8");
  return JSON.parse(request.options.body);
}

test("missing shortcuts use one reviewed incremental preview and one explicit incremental apply", async () => {
  const app = setup();
  assert.match(app.html(), />补建快捷方式<\/button>/);
  const run = app.generate();
  assert.deepEqual(requestBody(app.requests[0]), { preview: true, incremental: true });
  assert.equal(app.busy(), true);
  app.requests[0].respond(preview());
  await flush();
  assert.equal(app.confirmations.length, 1);
  assert.match(app.confirmations[0], /待创建 2，已存在 3，未绑定 DB 4/);
  assert.match(app.confirmations[0], /R:\/Links\/Animation\nR:\/Links\/Drama/);
  assert.doesNotMatch(app.confirmations[0], /是否备份|先清空|备份后|backup_existing|confirm_empty/);
  assert.deepEqual(requestBody(app.requests[1]), {
    incremental: true, plan_id: "reviewed-plan", confirm_incremental: true,
  });
  app.requests[1].respond({ ok: true, marker: "applied", file_generation: {
    incremental: true, created: 2, already_exists_count: 3, skipped_unbound_count: 4, skipped_missing_target: 5,
  } });
  await run;
  assert.equal(app.requests.length, 2);
  assert.equal(app.confirmations.length, 1);
  assert.equal(app.busy(), false);
  assert.equal(app.state().marker, "applied");
  assert.match(app.notice().message, /创建 2 个.*未删除或覆盖已有文件/);
  assert.equal(app.notice().is_error, false);
});

for (const [name, info] of [
  ["absent file-generation contract", {}],
  ["old replacement contract", { incremental: false, plan_id: "old", creatable: 2 }],
  ["missing plan id", { incremental: true, creatable: 2 }],
  ["blank plan id", { incremental: true, plan_id: "   ", creatable: 2 }],
  ["non-string plan id", { incremental: true, plan_id: { id: "bad" }, creatable: 2 }],
]) {
  test("a server with " + name + " never reaches confirmation or apply", async () => {
    const app = setup();
    const run = app.generate();
    app.requests[0].respond({ ok: true, file_generation: info });
    await run;
    assert.equal(app.requests.length, 1);
    assert.equal(app.confirmations.length, 0);
    assert.equal(app.state(), app.initialState);
    assert.equal(app.busy(), false);
    assert.match(app.notice().message, /尚未支持安全增量补建/);
    assert.equal(app.notice().is_error, true);
    assert.match(app.statuses.at(-1).message, /尚未支持安全增量补建/);
  });
}

for (const creatable of [0, "0"]) {
  test("zero creatable shortcuts finish without confirmation or apply: " + JSON.stringify(creatable), async () => {
    const app = setup();
    const run = app.generate();
    app.requests[0].respond(preview({ creatable }));
    await run;
    assert.equal(app.requests.length, 1);
    assert.equal(app.confirmations.length, 0);
    assert.equal(app.state(), app.initialState);
    assert.match(app.notice().message, /无需补建.*已存在 3/);
    assert.equal(app.busy(), false);
  });
}

test("conflicting targets block all writes and show escaped conflict details", async () => {
  const app = setup();
  const run = app.generate();
  app.requests[0].respond(preview({
    conflict_count: 1, conflicts: [{ shortcut_path: "R:/Wrong <target>.lnk", error: "existing & different" }],
  }));
  await run;
  assert.equal(app.requests.length, 1);
  assert.equal(app.confirmations.length, 0);
  assert.equal(app.state(), app.initialState);
  assert.equal(app.notice().is_error, true);
  assert.match(app.notice().message, /预检存在冲突/);
  assert.match(app.html(), /Wrong &lt;target&gt;\.lnk/);
  assert.match(app.html(), /existing &amp; different/);
});

test("cancelling the single confirmation retains the displayed index and never applies", async () => {
  const app = setup({ confirm: false });
  const run = app.generate();
  app.requests[0].respond(preview());
  await run;
  assert.equal(app.requests.length, 1);
  assert.equal(app.confirmations.length, 1);
  assert.equal(app.state(), app.initialState);
  assert.equal(app.busy(), false);
  assert.match(app.notice().message, /已取消补建快捷方式/);
});

for (const [phase, data, status] of [
  ["preview", { ok: false, error: "preview refused" }, 409],
  ["preview", null, 200],
  ["apply", { ok: false, error: "plan changed" }, 409],
  ["apply", null, 200],
]) {
  test(phase + " HTTP/data errors retain index state and release the action", async () => {
    const app = setup();
    const run = app.generate();
    if (phase === "apply") {
      app.requests[0].respond(preview());
      await flush();
      app.requests[1].respond(data, status);
    } else app.requests[0].respond(data, status);
    await run;
    assert.equal(app.state(), app.initialState);
    assert.equal(app.notice().is_error, true);
    assert.equal(app.busy(), false);
    assert.equal(app.requests.length, phase === "apply" ? 2 : 1);
    assert.equal(app.statuses.at(-1).isError, true);
  });
}

test("network errors reached through the real button show a repair failure and preserve index state", async () => {
  const app = setup();
  app.click();
  app.requests[0].reject(new Error("connection lost"));
  await flush();
  assert.equal(app.state(), app.initialState);
  assert.equal(app.busy(), false);
  assert.match(app.notice().message, /快捷方式补建失败：connection lost/);
  assert.equal(app.statuses.at(-1).isError, true);
});

test("rapid repeated clicks admit only one preview and disable conflicting index actions", async () => {
  const app = setup();
  app.click();
  for (const action of ["generate-files", "generate-files", "reload", "generate", "validate"]) app.click(action);
  await app.generate();
  assert.equal(app.requests.length, 1);
  for (const action of ["reload", "validate", "generate", "generate-files"]) {
    assert.match(app.html(), new RegExp('data-link-index-action="' + action + '" disabled'));
  }
  app.requests[0].respond(preview({ creatable: 0 }));
  await flush();
  assert.equal(app.busy(), false);
  assert.doesNotMatch(app.html(), /data-link-index-action="generate-files" disabled/);
});

test("leaving the feature during preview never confirms or applies the stale response", async () => {
  const app = setup();
  const run = app.generate();
  app.deactivate();
  const statusesBeforeResponse = app.statuses.length;
  app.requests[0].respond(preview());
  await run;
  assert.equal(app.requests.length, 1);
  assert.equal(app.confirmations.length, 0);
  assert.equal(app.state(), null);
  assert.equal(app.notice(), null);
  assert.equal(app.busy(), false);
  assert.equal(app.statuses.length, statusesBeforeResponse);
});

test("leaving and returning before preview returns cannot reuse the old confirmation", async () => {
  const app = setup();
  const run = app.generate();
  app.deactivate();
  const currentState = { ok: true, marker: "newly loaded", items: [] };
  app.setState(currentState);
  app.requests[0].respond(preview());
  await run;
  assert.equal(app.confirmations.length, 0);
  assert.equal(app.requests.length, 1);
  assert.equal(app.state(), currentState);
  assert.equal(app.notice(), null);
  assert.equal(app.busy(), false);
});

test("an apply response from a previous lifecycle cannot replace a newly loaded index", async () => {
  const app = setup();
  const run = app.generate();
  app.requests[0].respond(preview());
  await flush();
  assert.equal(app.requests.length, 2);
  app.deactivate();
  const currentState = { ok: true, marker: "newly loaded", items: [] };
  app.setState(currentState);
  app.requests[1].respond({ ok: true, marker: "stale applied response", file_generation: { created: 2 } });
  await run;
  assert.equal(app.state(), currentState);
  assert.equal(app.notice(), null);
  assert.equal(app.busy(), false);
  assert.equal(app.confirmations.length, 1);
});

for (const invalid of [{ creatable: -1 }, { creatable: "invalid" }, { conflict_count: -1 }]) {
  test("invalid preview counts refuse generation: " + JSON.stringify(invalid), async () => {
    const app = setup();
    const run = app.generate();
    app.requests[0].respond(preview(invalid));
    await run;
    assert.equal(app.confirmations.length, 0);
    assert.equal(app.requests.length, 1);
    assert.match(app.notice().message, /预检计数无效/);
  });
}

test("resource statistics label physical top directories and files without claiming DB work counts", () => {
  const app = setup();
  const summary = app.resourceSummary({ ok: true, cached: true, summary: {
    existing_root_count: 2, root_count: 3, series_count: 10, dir_count: 48, file_count: 366,
  } });
  assert.match(summary, /缓存：资源库 2 \/ 3/);
  assert.match(summary, /一级目录 10，物理目录 48，文件 366/);
  assert.match(summary, /不是 DB 作品数/);
  assert.doesNotMatch(summary, /作品 10|作品数 10|压制 48/);
});
