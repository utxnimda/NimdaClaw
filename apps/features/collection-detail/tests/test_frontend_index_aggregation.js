"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

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
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(callback);
    },
    removeEventListener(type, callback) {
      listeners.set(type, (listeners.get(type) || []).filter((item) => item !== callback));
    },
    body: { classList: { add() {}, remove() {} } },
  };
  const requests = [];
  const statuses = [];
  const confirmations = [];
  const alerts = [];
  const window = {
    document,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
    confirm(message) { confirmations.push(message); return options.confirm !== false; },
    alert(message) { alerts.push(message); },
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
  assert.ok(original.includes(marker), "the hooks must be injected into the real feature closure");
  const source = original.replace(marker, `
    window.indexHooks = {
      attrs: linkOpenAttrs, open: openLinkIndexPath, summary: summaryText, label: statusLabel,
      resourceSummary: resourceScanSummaryText, rebuild: generateLinkIndex,
      state: function () { return linkIndexState; },
      notice: function () { return linkIndexOperationNotice; },
      setState: function (value) {
        featureActive = true; activeSubtab = "index"; linkIndexState = value;
        linkIndexLoading = false; linkIndexOperationNotice = null;
        renderLinkIndexPanel();
      },
    };
` + marker);
  const commonSource = fs.readFileSync(path.resolve(__dirname, "../../../framework/frontend/src/common/runtime.js"), "utf8");
  vm.runInNewContext(commonSource + "\n" + source, {
    window, document, console, URLSearchParams,
    localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  }, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features[0];
  feature.init(context);
  window.indexHooks.setState({ ok: true, plan_summary: { total: 8 }, disk_summary: { shortcut_leaves: 3 } });
  return {
    ...window.indexHooks, requests, statuses, confirmations, alerts,
    html() { return slots.get(INDEX_SLOT).innerHTML; },
    click(action) {
      const target = {
        owner: INDEX_SLOT,
        closest(selector) { return selector === "[data-link-index-action]" ? this : null; },
        getAttribute(name) { return name === "data-link-index-action" ? action : null; },
      };
      for (const callback of listeners.get("click") || []) callback({ target, preventDefault() {} });
    },
    page(value) {
      const target = {
        owner: INDEX_SLOT,
        closest(selector) { return selector === "[data-link-index-unmapped-page]" ? this : null; },
        getAttribute(name) { return name === "data-link-index-unmapped-page" ? String(value) : null; },
      };
      for (const callback of listeners.get("click") || []) callback({ target, preventDefault() {} });
    },
  };
}

test("actual-target mismatch and unresolved observations have explicit display labels", () => {
  const fixture = setup();
  assert.equal(fixture.label("target_mismatch"), "快捷方式指向不一致");
  assert.equal(fixture.label("missing_catalog_binding"), "DB 缺少目录绑定");
  assert.equal(fixture.label("shortcut_target_unknown"), "快捷方式目标未确认");
});

function button(attributes) {
  const values = Object.fromEntries([...attributes.matchAll(/(data-[\w-]+)="([^"]*)"/g)]
    .map((match) => [match[1], match[2]]));
  return { getAttribute(name) { return values[name] ?? null; } };
}

function dbNode(overrides = {}) {
  return {
    source: "index_db", path: "L:/planned.lnk", shortcut_path: "L:/planned.lnk",
    open_path: "L:/planned.lnk", shortcut_exists: false,
    target_path: "S:/Saved/Work", target_exists: true,
    shortcut_target_path: "S:/stale-shortcut-target", shortcut_target_exists: true,
    ...overrides,
  };
}

test("DB-only planned shortcut paths never open as shortcuts; an existing saved target opens directly", async () => {
  const app = setup();
  const attributes = app.attrs(dbNode());
  const target = button(attributes);
  assert.equal(target.getAttribute("data-link-index-open"), "S:/Saved/Work");
  assert.equal(target.getAttribute("data-link-shortcut-path"), "");
  assert.equal(target.getAttribute("data-link-target-path"), "S:/Saved/Work");
  assert.equal(target.getAttribute("data-can-open"), "1");
  assert.doesNotMatch(attributes, / disabled/);
  const run = app.open(target);
  assert.equal(app.requests[0].url, "/api/collection-detail/link-index/open");
  assert.equal(app.requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(app.requests[0].options.body), { path: "S:/Saved/Work" });
  app.requests[0].respond({ ok: true });
  await run;
  assert.equal(app.requests.length, 1);
});

for (const [name, overrides] of [
  ["missing saved target", { target_exists: false }],
  ["missing saved path", { target_path: "", target_exists: true }],
  ["stale matched shortcut explicitly absent on disk", {
    matched_shortcut_path: "L:/stale.lnk", target_exists: false,
  }],
]) {
  test("DB-only " + name + " stays disabled without making an open request", async () => {
    const app = setup();
    const attributes = app.attrs(dbNode(overrides));
    const target = button(attributes);
    assert.equal(target.getAttribute("data-can-open"), "0");
    assert.equal(target.getAttribute("data-link-shortcut-path"), "");
    assert.match(attributes, / disabled$/);
    await app.open(target);
    assert.equal(app.requests.length, 0);
    assert.equal(app.alerts.length, 0);
    assert.equal(app.statuses.at(-1).isError, true);
    assert.match(app.statuses.at(-1).message, /目标目录不存在|没有可打开的路径/);
  });
}

for (const [name, overrides, expectedPath] of [
  ["DB aggregate with an observed matched shortcut", {
    shortcut_exists: true, matched_shortcut_path: "L:/actual.lnk", target_resolved: true,
    shortcut_target_path: "S:/Actual/Work", shortcut_target_exists: true,
  }, "L:/actual.lnk"],
  ["legacy observed matched shortcut without an existence flag", {
    shortcut_exists: undefined, matched_shortcut_path: "L:/observed.lnk",
  }, "L:/observed.lnk"],
  ["physical shortcut before lazy resolution", {
    source: "disk", shortcut_exists: true, shortcut_path: "L:/physical.lnk",
    target_resolved: false, target_exists: false, shortcut_target_exists: false,
    target_path: "", shortcut_target_path: "",
  }, "L:/physical.lnk"],
]) {
  test(name + " opens the real shortcut rather than a DB-planned path", async () => {
    const app = setup();
    const attributes = app.attrs(dbNode(overrides));
    const target = button(attributes);
    assert.equal(target.getAttribute("data-can-open"), "1");
    assert.equal(target.getAttribute("data-link-index-open"), expectedPath);
    assert.equal(target.getAttribute("data-link-shortcut-path"), expectedPath);
    assert.doesNotMatch(attributes, / disabled/);
    const run = app.open(target);
    assert.deepEqual(JSON.parse(app.requests[0].options.body), { path: expectedPath });
    app.requests[0].respond({ ok: true });
    await run;
  });
}

test("a real resolved shortcut with a missing actual target remains disabled even if the DB target exists", async () => {
  const app = setup();
  const attributes = app.attrs(dbNode({
    shortcut_exists: true, matched_shortcut_path: "L:/broken.lnk", target_resolved: true,
    shortcut_target_path: "S:/Missing", shortcut_target_exists: false,
  }));
  const target = button(attributes);
  assert.equal(target.getAttribute("data-link-shortcut-path"), "L:/broken.lnk");
  assert.equal(target.getAttribute("data-link-target-path"), "S:/Missing");
  assert.equal(target.getAttribute("data-target-exists"), "0");
  assert.equal(target.getAttribute("data-can-open"), "0");
  assert.match(attributes, / disabled$/);
  await app.open(target);
  assert.equal(app.requests.length, 0);
});

test("explicitly absent observed shortcuts fall back only to the current saved target", () => {
  const app = setup();
  const target = button(app.attrs(dbNode({ matched_shortcut_path: "L:/stale.lnk" })));
  assert.equal(target.getAttribute("data-link-shortcut-path"), "");
  assert.equal(target.getAttribute("data-link-index-open"), "S:/Saved/Work");
  assert.equal(target.getAttribute("data-can-open"), "1");
});

test("index reload explicitly requests fresh physical observations without generating anything", async () => {
  const app = setup();
  app.click("reload");
  assert.equal(app.requests.length, 1);
  const request = app.requests[0];
  const url = new URL(request.url, "http://frontend.test");
  assert.equal(url.pathname, "/api/collection-detail/link-index");
  assert.equal(url.searchParams.get("lite"), "1");
  assert.equal(url.searchParams.get("refresh_links"), "1");
  assert.equal(request.options.method, "GET");
  assert.equal(request.options.body, undefined);
  request.respond({ ok: true, marker: "fresh-disk", plan_summary: { total: 9 }, disk_summary: { shortcut_leaves: 4 } });
  await flush();
  assert.equal(app.state().marker, "fresh-disk");
  assert.match(app.html(), /DB 压制索引 9 项，实际快捷方式 4 个/);
  assert.match(app.statuses.at(-1).message, /实际索引目录观察已更新/);
});

test("index summary separates DB press records, real shortcuts and unassociated disk entries", () => {
  const app = setup();
  const data = {
    ok: true, plan_summary: { total: 27, empty_target_path: 2, unmapped_on_disk: 3 },
    disk_summary: { shortcut_leaves: 19 },
  };
  assert.equal(app.summary(data), "DB 压制索引 27 项，实际快捷方式 19 个，空路径 2 项，磁盘未关联 3 项");
  app.setState(data);
  assert.doesNotMatch(app.html(), /当前作品 DB \+ 实际快捷方式 \/ 目录聚合/);
  assert.doesNotMatch(app.html(), /索引 DB 仅为可重建缓存/);
  assert.match(app.html(), /DB 压制索引 27 项，实际快捷方式 19 个/);
  assert.match(app.html(), />重建索引缓存<\/button>/);
  assert.match(app.html(), />补建快捷方式<\/button>/);
  assert.doesNotMatch(app.html(), />重新生成索引<\/button>/);
});

test("unmapped shortcut warning lists full paths, actual targets and precise resolution errors", () => {
  const app = setup();
  app.setState({ ok: true, plan_summary: { unmapped_on_disk: 3 }, unmapped_shortcuts: [
    { shortcut_path: "L:/2005/Extra/BDRip.lnk", target_path: "S:/Real/Release", shortcut_exists: true, target_resolved: true, target_exists: true },
    { shortcut_path: "L:/2005/Missing/BDRip.lnk", target_path: "S:/Missing/Release", shortcut_exists: true, target_resolved: true, target_exists: false },
    { shortcut_path: "L:/<unresolved>/DVD.lnk", target_path: "", shortcut_exists: true, target_resolved: false, target_exists: false, target_error: "COM <error> & failure" },
  ] });
  const html = app.html();
  assert.match(html, /<details class="link-index-warnings" open>/);
  assert.match(html, /L:\/2005\/Extra\/BDRip.lnk/);
  assert.match(html, /S:\/Real\/Release/);
  assert.match(html, /S:\/Missing\/Release/);
  assert.match(html, /目标目录存在/);
  assert.match(html, /目标目录不存在/);
  assert.match(html, /目标未解析/);
  assert.match(html, /L:\/&lt;unresolved&gt;\/DVD.lnk/);
  assert.match(html, /COM &lt;error&gt; &amp; failure/);
  assert.equal(app.requests.length, 0);
});

test("unmapped shortcut details paginate all items without rendering an unbounded DOM", () => {
  const app = setup();
  const items = Array.from({ length: 52 }, (_, index) => ({
    shortcut_path: "L:/extra-" + String(index).padStart(3, "0") + ".lnk",
    shortcut_exists: true, target_resolved: false,
  }));
  app.setState({ ok: true, plan_summary: { unmapped_on_disk: 52 }, unmapped_shortcuts: items });
  assert.match(app.html(), /第 1 \/ 3 页，共 52 项；每页 25 项/);
  assert.match(app.html(), /extra-024.lnk/);
  assert.doesNotMatch(app.html(), /extra-025.lnk/);
  app.page(1);
  assert.match(app.html(), /第 2 \/ 3 页/);
  assert.match(app.html(), /extra-025.lnk/);
  assert.match(app.html(), /extra-049.lnk/);
  assert.doesNotMatch(app.html(), /extra-000.lnk|extra-050.lnk/);
  app.page(2);
  assert.match(app.html(), /第 3 \/ 3 页/);
  assert.match(app.html(), /extra-050.lnk/);
  assert.match(app.html(), /extra-051.lnk/);
  app.page(100);
  assert.match(app.html(), /第 3 \/ 3 页/);
  assert.equal(app.requests.length, 0);
});

test("legacy responses explicitly state that unmatched details need a reload", () => {
  const app = setup();
  app.setState({ ok: true, plan_summary: { unmapped_on_disk: 2 } });
  assert.match(app.html(), /当前响应没有具体明细，请点击“重读”获取/);
});

test("open errors are routed through the shared status/details entry without alerts", async () => {
  const app = setup();
  const run = app.open(button(app.attrs(dbNode())));
  app.requests[0].respond({ ok: false, error: "无法访问指定目录" }, 500);
  await run;
  assert.deepEqual(app.statuses.at(-1), { message: "无法访问指定目录", isError: true });
  assert.equal(app.alerts.length, 0);
});

test("rebuilding index cache keeps the existing generation endpoint and empty request contract", async () => {
  const app = setup();
  const run = app.rebuild();
  assert.equal(app.confirmations.length, 1);
  assert.match(app.confirmations[0], /根据当前作品 DB 重建索引缓存/);
  assert.match(app.confirmations[0], /不改写作品 DB 或创建快捷方式/);
  assert.equal(app.requests[0].url, "/api/collection-detail/link-index/generate");
  assert.equal(app.requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(app.requests[0].options.body), {});
  assert.match(app.statuses.at(-1).message, /索引缓存重建中/);
  app.requests[0].respond({ ok: true, index_db: { item_count: 27 }, plan_summary: { total: 27, empty_target_path: 2 } });
  await run;
  assert.match(app.notice().message, /索引缓存已重建：共 27 项，空路径 2 项/);
  assert.match(app.statuses.at(-1).message, /索引缓存已重建/);
  assert.equal(app.requests.length, 1, "rebuilding cache must not invoke shortcut generation");
});

test("canceling cache reconstruction sends no request", async () => {
  const app = setup({ confirm: false });
  await app.rebuild();
  assert.equal(app.confirmations.length, 1);
  assert.equal(app.requests.length, 0);
});

test("resource counts remain physical and ignore DB/index aggregation fields", () => {
  const app = setup();
  const summary = app.resourceSummary({
    ok: true, summary: { existing_root_count: 1, root_count: 2, series_count: 4, dir_count: 17, file_count: 21 },
    plan_summary: { total: 999 }, disk_summary: { shortcut_leaves: 888 },
  });
  assert.match(summary, /资源库 1 \/ 2，一级目录 4，物理目录 17，文件 21/);
  assert.match(summary, /不是 DB 作品数/);
  assert.doesNotMatch(summary, /999|888|实际快捷方式|压制索引/);
});
