"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "../frontend/index.js");
const flush = () => new Promise((resolve) => setImmediate(resolve));

function setup() {
  const requests = [];
  const statuses = [];
  const records = [{ domain: "animation", country: "japan", release_type: "tv", completed_years: ["2020"] }];
  const buttons = [{ disabled: false }, { disabled: false }];
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
  };
  vm.runInNewContext(fs.readFileSync(FRONTEND, "utf8"), { window, document, console }, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features[0];
  feature.init(context);
  feature.activate(context);
  return {
    requests, statuses, records, buttons, view,
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
