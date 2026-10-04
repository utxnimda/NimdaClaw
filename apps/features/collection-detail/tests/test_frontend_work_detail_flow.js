"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { withCommonRuntime } = require("../../../framework/tests/frontend_runtime_fixture");

const SHELL = path.resolve(__dirname, "../frontend/table-controller.js");

function setup() {
  const elements = new Map();
  for (const id of ["chk-sheet-edit", "viewport", "status-line"]) {
    const handlers = new Map();
    elements.set(id, {
      checked: false, textContent: "", className: "", handlers,
      addEventListener(type, handler) { handlers.set(type, handler); },
      contains(target) { return !!target && target.inViewport === true; },
      querySelector() { return null; }, querySelectorAll() { return []; },
    });
  }
  const document = {
    documentElement: { setAttribute() {}, removeAttribute() {} },
    getElementById(id) { return elements.get(id) || null; },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {},
  };
  const dialogs = [];
  const requests = [];
  const window = {
    document, addEventListener() {},
    NimdaWorkDetailDialog: { open(options) { dialogs.push(options); } },
    async testFetch(url, options) {
      requests.push({ url, options });
      throw new Error("Unexpected network request: " + url);
    },
  };
  const original = fs.readFileSync(SHELL, "utf8");
  const marker = '  function mount() {';
  assert.ok(original.includes(marker), "shell startup marker must exist");
  const source = original.slice(0, original.indexOf(marker)) + `
    // Test the actual render fragments and event delegation without starting
    // the application, reading config, or contacting a real database.
    fetchJson = function (url, options) { return window.testFetch(url, options); };
    window.hooks = {
      renderWorkNameField, renderFlatTable, workDetailDraftRecord, openWorkDetails, bindSheetInlineEditOnce,
      setPayload(value) { lastBrowsePayload = value; },
      setEditing(value) { sheetEditMode = value; $chkEdit.checked = value; },
    };
  }
  window.createTableFixture = createTableController;
})();
window.createTableFixture({
  featureHost: { getActiveId() { return "collection-detail"; }, configure() {}, refresh() {} },
  fetchJson(url, options) { return window.testFetch(url, options); },
  setStatus(message, isError) {
    const status = document.getElementById("status-line");
    if (status) { status.textContent = message || ""; status.className = "status" + (isError ? " err" : ""); }
  },
});`;
  vm.runInNewContext(withCommonRuntime(source), {
    window, document, console,
    localStorage: { getItem() { return null; }, setItem() {} },
  }, { filename: SHELL });
  return { ...window.hooks, window, elements, dialogs, requests };
}

function row(overrides = {}) {
  return {
    name: "示例作品", yaml_source_rel: "[JP][TVInfo][2026].yaml", index_in_file: 3,
    domain: "animation", release_type: "tv", country: "japan",
    date: { start: "20260101", end: "20260331" }, path: "示例作品", markers: [],
    collectioned_ordered: [], ...overrides,
  };
}

function payload(rows, sources) {
  return {
    ok: true, profile_groups: [{ rows }],
    sources_loaded: sources || [{ relpath: "[JP][TVInfo][2026].yaml", sha256: "a".repeat(64) }],
  };
}

function plain(value) { return JSON.parse(JSON.stringify(value)); }

function respondWith(app, record, source = {}) {
  app.window.testFetch = async (url, options) => {
    app.requests.push({ url, options });
    return { res: { ok: true, status: 200 }, data: { ok: true, record, source } };
  };
}

for (const editing of [false, true]) {
  test(`the complete escaped work name opens details in ${editing ? "edit" : "read-only"} mode`, () => {
    const app = setup();
    app.setEditing(editing);
    const html = app.renderWorkNameField(row({ name: 'Name <script> & "Long name"' }), "fixture.yaml");
    assert.match(html, /<button type="button" class="sheet-work-name-link"[^>]*data-action="work-details"/);
    assert.doesNotMatch(html, /<button[^>]*\bdisabled(?:\s|=|>)/, "work names remain clickable without link decoration");
    assert.match(html, /data-sheet-iif="3" data-yaml-rel="fixture.yaml"/);
    assert.match(html, />Name &lt;script&gt; &amp; &quot;Long name&quot;<\/button>/);
    assert.doesNotMatch(html, /<script>/);
    if (editing) {
      assert.match(html, /^<span class="sheet-work-name-content">/);
      assert.match(html, /data-action="inline-edit-scalar"/);
      assert.match(html, /data-field="name" data-current-value="Name &lt;script&gt; &amp; &quot;Long name&quot;">编辑<\/span>/);
    } else {
      assert.doesNotMatch(html, /inline-edit-scalar|sheet-work-name-edit|sheet-work-name-content/);
    }
  });

  test(`delegated work-name clicks work in ${editing ? "edit" : "read-only"} mode`, () => {
    const app = setup();
    const target = row();
    app.setPayload(payload([target]));
    app.setEditing(editing);
    app.bindSheetInlineEditOnce();
    app.bindSheetInlineEditOnce();
    const trigger = {
      inViewport: true,
      getAttribute(key) { return { "data-yaml-rel": target.yaml_source_rel, "data-sheet-iif": "3" }[key] || null; },
    };
    const click = app.elements.get("viewport").handlers.get("click");
    click({ target: { closest(selector) { return selector === '[data-action="work-details"]' ? trigger : null; } } });
    assert.equal(app.dialogs.length, 1);
    assert.equal(app.dialogs[0].title, target.name);
    assert.equal(app.requests.length, 0, "opening is lazy; the dialog loads the selected record");
  });
}

test("database detail is lazy and requests exactly the selected file, record index and source revision", async () => {
  const app = setup();
  const target = row({ yaml_source_rel: "nested\\chosen.yaml", index_in_file: 9, name: "Unsaved list title" });
  app.setPayload(payload([
    row({ yaml_source_rel: "other.yaml", index_in_file: 9 }), target,
  ], [
    { relpath: "other.yaml", sha256: "b".repeat(64) },
    { relpath: "nested/chosen.yaml", sha256: "c".repeat(64) },
  ]));
  const record = { attributes: [{ type: "name", data: "Persisted title" }], custom: { value: 42 } };
  const source = { relpath: target.yaml_source_rel, index_in_file: 9 };
  respondWith(app, record, source);
  assert.equal(app.openWorkDetails("nested/chosen.yaml", 9), true);
  assert.equal(app.requests.length, 0);
  assert.match(app.dialogs[0].notice, /已保存.*尚未保存/);
  const details = await app.dialogs[0].loadRecord();
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].url, "/api/collection-detail/work/detail");
  assert.equal(app.requests[0].options.method, "POST");
  assert.deepEqual(JSON.parse(app.requests[0].options.body), {
    yaml_source_rel: "nested\\chosen.yaml", index_in_file: 9, source_sha256: "c".repeat(64),
  });
  assert.equal(details.record, record, "the authoritative record is not merged with modified list fields");
  assert.equal(details.source, source);
  assert.equal(details.record.attributes[0].data, "Persisted title");
});

test("uploaded complete source records are shown without a database request or source mutation", async () => {
  const app = setup();
  const sourceRecord = { attributes: [{ type: "name", data: "Original uploaded title" }], nested: { arbitrary: [1, 2] } };
  const target = row({ name: "List representation", source_record: sourceRecord });
  app.setPayload(payload([target], []));
  app.openWorkDetails(target.yaml_source_rel, target.index_in_file);
  assert.match(app.dialogs[0].notice, /上传文件.*不会写入/);
  const detail = await app.dialogs[0].loadRecord();
  assert.deepEqual(plain(detail.record), sourceRecord);
  detail.record.nested.arbitrary.push(3);
  assert.deepEqual(sourceRecord.nested.arbitrary, [1, 2], "the dialog receives an isolated snapshot");
  assert.equal(app.requests.length, 0);
});

test("new drafts use standard attributes and reconstruct continuations without leaking list metadata", async () => {
  const app = setup();
  const target = row({
    _isNew: true, yaml_source_rel: "draft.yaml", index_in_file: 100,
    date: { start: "2026-01-01", end: "2026-03-31" }, markers: ["Collected"],
    collectioned_ordered: [
      { press_format: "BDRip", press_group: "VCB", press_path: "_BDRip(VCB)", segment: "main", continuation_index: null },
      { press_format: "1080p", press_group: "Jsum", segment: "continuation", continuation_index: 2, continuation_title: "Second season", custom: { preserved: true } },
      { press_format: "BDRip", press_group: "VCB", segment: "continuation", continuation_index: 2, continuation_title: "Second season" },
      { press_format: "DVD", segment: "continuation", continuation_index: 4, continuation_title: "Special" },
    ],
  });
  const before = plain(target);
  app.setPayload(payload([target], []));
  app.openWorkDetails(target.yaml_source_rel, 100);
  assert.match(app.dialogs[0].notice, /新增草稿.*不代表已保存/);
  const details = await app.dialogs[0].loadRecord();
  const record = plain(details.record);
  assert.deepEqual(record.attributes.map(item => item.type), ["date", "collection-type", "country", "name"]);
  assert.deepEqual(record.attributes[0].data, { start: "20260101", end: "20260331" });
  const collection = record.attributes[1].data;
  assert.deepEqual(collection.markers, ["Collected"]);
  assert.deepEqual(collection.collectioned, [{ press_format: "BDRip", press_group: "VCB", press_path: "_BDRip(VCB)" }]);
  assert.equal(collection.continuations.length, 2);
  assert.equal(collection.continuations[0].title, "Second season");
  assert.equal(collection.continuations[0].collectioned.length, 2);
  assert.deepEqual(collection.continuations[0].collectioned[0].custom, { preserved: true });
  assert.equal(collection.continuations[1].title, "Special");
  assert.doesNotMatch(JSON.stringify(record), /_isNew|yaml_source_rel|index_in_file|continuation_index|continuation_title|"segment"/);
  details.record.attributes[1].data.markers.push("Later mutation");
  details.record.attributes[1].data.continuations[0].collectioned[0].custom.preserved = false;
  assert.deepEqual(target, before);
  assert.equal(app.requests.length, 0);
});

test("a committed draft awaiting refresh cannot be mistaken for an unsaved or uploaded record", async () => {
  const app = setup();
  const target = row({ _isNew: true, _persistedPendingRefresh: true, source_record: { stale: true } });
  app.setPayload(payload([target]));
  app.openWorkDetails(target.yaml_source_rel, target.index_in_file);
  await assert.rejects(app.dialogs[0].loadRecord(), /已经保存.*刷新列表/);
  assert.equal(app.requests.length, 0);
});

test("missing matching source revision fails closed rather than fetching a possibly different record", async () => {
  const app = setup();
  const target = row();
  app.setPayload(payload([target], [{ relpath: "not-selected.yaml", sha256: "d".repeat(64) }]));
  app.openWorkDetails(target.yaml_source_rel, target.index_in_file);
  await assert.rejects(app.dialogs[0].loadRecord(), /缺少来源版本信息.*重新加载/);
  assert.equal(app.requests.length, 0);
});

test("a dialog keeps its selected row and hash after list reload or in-place mutation", async () => {
  const app = setup();
  const target = row();
  const original = payload([target]);
  app.setPayload(original);
  respondWith(app, { attributes: [{ type: "name", data: "Saved original" }] });
  app.openWorkDetails(target.yaml_source_rel, target.index_in_file);
  target.yaml_source_rel = "mutated.yaml";
  target.index_in_file = 55;
  target._isNew = true;
  original.sources_loaded[0].sha256 = "e".repeat(64);
  app.setPayload(payload([row({ yaml_source_rel: "replacement.yaml", index_in_file: 99 })]));
  await app.dialogs[0].loadRecord();
  assert.deepEqual(JSON.parse(app.requests[0].options.body), {
    yaml_source_rel: "[JP][TVInfo][2026].yaml", index_in_file: 3, source_sha256: "a".repeat(64),
  });
});

test("an uploaded dialog keeps its complete record snapshot after source mutation or replacement", async () => {
  const app = setup();
  const target = row({ source_record: { attributes: [{ type: "name", data: "Original upload" }] } });
  app.setPayload(payload([target], []));
  app.openWorkDetails(target.yaml_source_rel, target.index_in_file);
  target.source_record.attributes[0].data = "Changed later";
  app.setPayload(payload([row()], []));
  assert.equal((await app.dialogs[0].loadRecord()).record.attributes[0].data, "Original upload");
  assert.equal(app.requests.length, 0);
});

for (const result of [
  { res: { ok: false, status: 409 }, data: { ok: false, error: "Source changed; reload required" } },
  { res: { ok: true, status: 200 }, data: { ok: true } },
]) {
  test(`database detail rejects ${result.res.ok ? "an incomplete success" : "a stale source"} without falling back to list data`, async () => {
    const app = setup();
    const target = row();
    app.setPayload(payload([target]));
    app.window.testFetch = async (url, options) => { app.requests.push({ url, options }); return result; };
    app.openWorkDetails(target.yaml_source_rel, target.index_in_file);
    await assert.rejects(app.dialogs[0].loadRecord(), result.res.ok ? /读取完整作品记录失败/ : /Source changed/);
    assert.equal(app.requests.length, 1);
  });
}

test("unmatched rows or a missing dialog component do not request records", () => {
  const app = setup();
  const target = row();
  app.setPayload(payload([target]));
  assert.equal(app.openWorkDetails("other.yaml", 3), false);
  assert.equal(app.openWorkDetails(target.yaml_source_rel, 3.5), false);
  assert.equal(app.dialogs.length, 0);
  delete app.window.NimdaWorkDetailDialog;
  assert.equal(app.openWorkDetails(target.yaml_source_rel, target.index_in_file), false);
  assert.match(app.elements.get("status-line").textContent, /尚未加载/);
  assert.equal(app.requests.length, 0);
});

test("rendered name cells keep detail, edit and delete as separate non-nested actions", () => {
  const app = setup();
  app.setEditing(true);
  const html = app.renderFlatTable([{ profile_key: "animation-tv", rows: [row({ name: "A long title with distinct edit and delete actions" })] }]);
  const cell = /<td class="col-name"[^>]*>([\s\S]*?)<\/td>/.exec(html);
  assert.ok(cell, "the real table must contain a name cell");
  const stack = [];
  const actions = [];
  for (const token of cell[1].matchAll(/<(\/?)([a-z][a-z0-9]*)\b([^>]*)>/gi)) {
    const [, closing, tag, attributes] = token;
    if (closing) {
      const last = stack.pop();
      assert.equal(last && last.tag, tag, "name-cell markup must remain properly nested");
      continue;
    }
    const match = /\bdata-action="([^"]+)"/.exec(attributes);
    const action = match && match[1];
    if (action) {
      assert.equal(stack.some(parent => parent.action), false, "interactive actions must not contain other actions");
      actions.push({ action, tag });
    }
    if (!["input", "br", "img", "hr"].includes(tag)) stack.push({ tag, action });
  }
  assert.deepEqual(actions, [
    { action: "work-details", tag: "button" },
    { action: "inline-edit-scalar", tag: "span" },
    { action: "delete-row", tag: "button" },
  ]);
  assert.equal(stack.length, 0);
});

test("an absolutely positioned row-delete action retains reserved name-cell space", () => {
  const stylesheet = fs.readFileSync(path.resolve(__dirname, "../../../framework/frontend/styles/app.css"), "utf8");
  const deleteRule = /table\.sheet\.sheet-editing \.row-delete-inline-btn\s*\{([^}]+)\}/.exec(stylesheet);
  assert.ok(deleteRule, "the row-delete layout rule must be present");
  if (!/position:\s*absolute\s*;/.test(deleteRule[1])) return;
  const nameRule = /table\.sheet\.sheet-editing \.col-name\s*\{([^}]+)\}/.exec(stylesheet);
  assert.ok(nameRule, "an overlay delete action requires its own name-cell layout rule");
  const padding = /padding-right:\s*([\d.]+)rem\s*;/.exec(nameRule[1]);
  assert.ok(padding && Number(padding[1]) >= 3, "reserve at least 3rem so hovering does not place delete over the edit action");
});

test("work names remain clickable without underlines and editable layouts fit narrow columns", () => {
  const stylesheet = fs.readFileSync(path.resolve(__dirname, "../../../framework/frontend/styles/app.css"), "utf8");
  const name = /\.sheet-work-name-link\s*\{([^}]+)\}/.exec(stylesheet);
  assert.ok(name);
  assert.match(name[1], /text-decoration:\s*none\s*;/);
  assert.match(name[1], /cursor:\s*pointer\s*;/);
  for (const hover of stylesheet.matchAll(/[^{}]*\.sheet-work-name-link:hover[^{}]*\{([^}]+)\}/g)) {
    assert.doesNotMatch(hover[1], /text-decoration(?:-line)?:[^;]*underline/, "hover must not restore the removed underline");
  }
  const content = /\.sheet-work-name-content\s*\{([^}]+)\}/.exec(stylesheet);
  assert.ok(content);
  assert.match(content[1], /grid-template-columns:\s*minmax\(0,\s*1fr\)\s+auto\s*;/);
  const input = /\.sheet-work-name-edit \.sheet-inline-input\s*\{([^}]+)\}/.exec(stylesheet);
  assert.ok(input);
  assert.match(input[1], /min-width:\s*0\s*;/);
  assert.match(input[1], /width:\s*100%\s*;/);
  assert.match(stylesheet, /\.sheet-work-name-edit:has\(\.sheet-inline-input\)\s*\{[^}]*grid-column:\s*1\s*\/\s*-1\s*;/);
});
