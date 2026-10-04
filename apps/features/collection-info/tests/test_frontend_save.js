"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { withCommonRuntime } = require("../../../framework/tests/frontend_runtime_fixture");

const FRONTEND = path.resolve(__dirname, "../frontend/index.js");
const flush = () => new Promise((resolve) => setImmediate(resolve));

function setup(options = {}) {
  const requests = [];
  const statuses = [];
  const records = [{ domain: "animation", country: "japan", release_type: "tv", completed_years: ["2020"] }];
  const buttons = [{ disabled: false }, { disabled: false }];
  const inputs = [{ disabled: false }, { disabled: false }];
  let onClick;
  let html = "";
  let renders = 0;
  const view = {
    get innerHTML() { return html; },
    set innerHTML(value) { html = value; renders += 1; },
    addEventListener(type, callback) { if (type === "click") onClick = callback; },
    removeEventListener() {},
    contains() { return true; },
    querySelectorAll(selector) {
      if (selector === ".collection-record-item") return records.map((record) => ({
        querySelector(fieldSelector) {
          const field = fieldSelector.match(/data-collection-field="([^"]+)"/)[1];
          return { value: record[field] };
        },
        querySelectorAll() {
          return record.completed_years.map((year) => ({ getAttribute() { return year; } }));
        },
      }));
      if (selector.includes('data-collection-action="save"')) return buttons;
      if (selector.includes('.collection-select')) return inputs;
      return [];
    },
  };
  const document = { getElementById() { return view; } };
  const window = { document };
  const context = {
    collectionView: view,
    setStatus(message, error) { statuses.push({ message, error }); },
    browseHttpFailHint(status) { return "HTTP " + status; },
    fetchJson(url, options) {
      return new Promise((resolve, reject) => requests.push({
        url, method: options.method, body: options.body && JSON.parse(options.body),
        respond(data) { resolve({ res: { ok: true, status: 200 }, data }); },
        reject,
      }));
    },
    ...options.context,
  };
  vm.runInNewContext(withCommonRuntime(fs.readFileSync(FRONTEND, "utf8")), { window, document, console }, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features[0];
  feature.init(context);
  feature.activate(context);
  return {
    requests, statuses, records, buttons, inputs, view, feature, context,
    renders: () => renders,
    click(action) {
      const target = { closest() { return this; }, getAttribute() { return action; } };
      onClick({ target });
    },
  };
}

async function loadedApp() {
  const app = setup();
  app.requests[0].respond({ ok: true, records: structuredClone(app.records), years: [{ key: "2020" }] });
  await flush();
  return app;
}

test("collection save preserves edits made during the request and prevents duplicate writes", async () => {
  const app = await loadedApp();
  const rendersBeforeSave = app.renders();
  app.click("save");
  assert.equal(app.buttons.every((button) => button.disabled), true);
  assert.equal(app.requests[1].body.records[0].country, "japan");
  app.records[0].country = "korea";
  app.click("save");
  app.click("reload");
  assert.equal(app.requests.length, 2);
  app.requests[1].respond({ ok: true, records: app.requests[1].body.records });
  await flush();
  assert.equal(app.renders(), rendersBeforeSave, "save completion replaced the edited form");
  assert.equal(app.records[0].country, "korea");
  assert.equal(app.requests.length, 2, "save completion unexpectedly reloaded old records");
  assert.equal(app.buttons.some((button) => button.disabled), false);
  assert.match(app.statuses.at(-1).message, /新编辑尚未保存/);

  app.click("save");
  assert.equal(app.requests[2].body.records[0].country, "korea");
  app.requests[2].respond({ ok: true, records: app.requests[2].body.records });
  await flush();
  assert.match(app.view.innerHTML, /value="korea" selected/);
  assert.equal(app.statuses.at(-1).message, "收集情况已保存。");
});

test("failed collection saves retain current inputs and enable retry", async () => {
  const app = await loadedApp();
  const rendersBeforeSave = app.renders();
  app.click("save");
  app.records[0].completed_years.push("2021");
  app.requests[1].reject(new Error("write failed"));
  await flush();
  assert.equal(app.renders(), rendersBeforeSave);
  assert.equal(app.buttons.some((button) => button.disabled), false);
  assert.match(app.statuses.at(-1).message, /write failed/);
  app.click("save");
  assert.deepEqual(app.requests[2].body.records[0].completed_years, ["2020", "2021"]);
});

test("collection rendering escapes source values without requiring a shell escape helper", async () => {
  const app = setup();
  app.requests[0].respond({ ok: true, path: '<img src=x onerror="bad()">',
    records: app.records, years: [] });
  await flush();
  assert.doesNotMatch(app.view.innerHTML, /<img src=x/);
  assert.match(app.view.innerHTML, /&lt;img src=x onerror=&quot;bad\(\)&quot;&gt;/);
});

test("switching pages and refreshing enum configuration preserve unsaved collection edits", async () => {
  const app = await loadedApp();
  app.records[0].country = "korea";
  app.feature.deactivate();
  app.feature.activate(app.context);
  assert.equal(app.requests.length, 1, "returning to a loaded form must not reload and erase its draft");
  app.feature.refreshAfterConfig(app.context);
  assert.match(app.view.innerHTML, /value="korea" selected/);
  app.click("save");
  assert.equal(app.requests[1].body.records[0].country, "korea");
});

test("a previous page's read cannot replace current data or clear its loading state", async () => {
  const app = setup();
  app.feature.activate(app.context);
  assert.equal(app.requests.length, 1, "concurrent activation should share the current read");
  app.feature.deactivate();
  app.feature.activate(app.context);
  assert.equal(app.requests.length, 2);
  app.requests[0].respond({ ok: true, path: "stale.yaml", records: app.records });
  await flush();
  assert.doesNotMatch(app.view.innerHTML, /stale\.yaml/);
  assert.equal(app.buttons.every((button) => button.disabled), true);
  app.requests[1].respond({ ok: true, path: "current.yaml", records: app.records });
  await flush();
  assert.match(app.view.innerHTML, /current\.yaml/);
  assert.equal(app.buttons.some((button) => button.disabled), false);
});

test("late reads do not post errors after leaving the collection page", async () => {
  const app = setup();
  app.feature.deactivate();
  const statusesBefore = app.statuses.length;
  app.requests[0].reject(new Error("stale-read-failure"));
  await flush();
  assert.equal(app.statuses.length, statusesBefore);
});

test("reload failures preserve the previous form and safely enable retry", async () => {
  const app = await loadedApp();
  const before = app.view.innerHTML;
  app.click("reload");
  assert.equal(app.inputs.every((input) => input.disabled), true);
  app.click("save");
  app.click("reload");
  assert.equal(app.requests.length, 2);
  app.requests[1].reject(new Error("offline"));
  await flush();
  assert.equal(app.view.innerHTML, before);
  assert.equal(app.buttons.some((button) => button.disabled), false);
  assert.equal(app.inputs.some((input) => input.disabled), false);
  app.click("reload");
  assert.equal(app.requests.length, 3);
});

test("a disposed save cannot render into a replacement feature or unlock its active save", async () => {
  const app = await loadedApp();
  app.click("save");
  app.feature.dispose();
  app.feature.init(app.context);
  app.feature.activate(app.context);
  app.requests[2].respond({ ok: true, path: "replacement.yaml", records: app.records });
  await flush();
  app.click("save");
  app.requests[1].respond({ ok: true, path: "disposed.yaml", records: app.records });
  await flush();
  assert.doesNotMatch(app.view.innerHTML, /disposed\.yaml/);
  assert.equal(app.buttons.every((button) => button.disabled), true);
  app.requests[3].respond({ ok: true, path: "replacement.yaml", records: app.records });
  await flush();
  assert.equal(app.buttons.some((button) => button.disabled), false);
});

test("leaving during configuration loading prevents a stale follow-up collection request", async () => {
  let completeConfig;
  const app = setup({ context: { loadServerConfig() {
    return new Promise((resolve) => { completeConfig = resolve; });
  } } });
  assert.equal(app.requests.length, 0);
  app.feature.deactivate();
  completeConfig();
  await flush();
  assert.equal(app.requests.length, 0);
});

test("saved completion years remain checked even when their directories were not scanned", async () => {
  const app = setup();
  app.requests[0].respond({ ok: true, records: app.records, years: [], warning: "drive offline" });
  await flush();
  assert.match(app.view.innerHTML, /data-year-key="2020"[^>]+checked/);
  assert.match(app.view.innerHTML, /2020（已记录，目录未发现）/);
  assert.match(app.view.innerHTML, /collection-year-chip-unavailable/);
  app.feature.refreshAfterConfig(app.context);
  assert.match(app.view.innerHTML, /data-year-key="2020"[^>]+checked/);
  app.click("save");
  assert.deepEqual(app.requests[1].body.records[0].completed_years, ["2020"]);
});

test("scanned and saved year choices are normalized without duplicate checkboxes", async () => {
  const app = setup();
  app.records[0].completed_years = ["2020", "199X"];
  app.requests[0].respond({ ok: true, records: app.records,
    years: [{ key: " 2020 " }, { key: "2020" }, { key: "199x", label: "九十年代" }] });
  await flush();
  assert.equal((app.view.innerHTML.match(/data-year-key="2020"/g) || []).length, 1);
  assert.match(app.view.innerHTML, /data-year-key="199X"[^>]+checked/);
  assert.doesNotMatch(app.view.innerHTML, /目录未发现/);
});

test("unchecked offline year options remain available across draft rerenders until an explicit reload", async () => {
  const app = setup();
  app.requests[0].respond({ ok: true, records: structuredClone(app.records), years: [] });
  await flush();
  app.records[0].completed_years = [];
  app.feature.refreshAfterConfig(app.context);
  assert.match(app.view.innerHTML, /data-year-key="2020"/);
  assert.doesNotMatch(app.view.innerHTML, /data-year-key="2020"[^>]+checked/);
  app.click("add-record");
  assert.match(app.view.innerHTML, /data-year-key="2020"/);
  app.records[0].completed_years = ["2020"];
  app.feature.refreshAfterConfig(app.context);
  assert.match(app.view.innerHTML, /data-year-key="2020"[^>]+checked/);
  app.click("reload");
  app.requests[1].respond({ ok: true, records: [{ ...app.records[0], completed_years: [] }], years: [] });
  await flush();
  assert.doesNotMatch(app.view.innerHTML, /data-year-key="2020"/);
});
