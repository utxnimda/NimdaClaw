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
  for (const id of ["chk-sheet-edit", "viewport", "status-line", "btn-sheet-save", "btn-sheet-add-row", "btn-enum-editor", "btn-load-default", "sheet-filter-hint", "enum-editor-panel"]) {
    elements.set(id, {
      checked: false, disabled: false, inert: false, hidden: false, title: "", textContent: "", innerHTML: "",
      addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    });
  }
  const document = {
    documentElement: { setAttribute() {}, removeAttribute() {} },
    getElementById(id) { return elements.get(id) || null; },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {},
  };
  const window = {
    document, addEventListener() {}, filterRows: [], filters: {}, requests: [], renderCalls: [], renderReal: false,
    async testFetch(url) { throw new Error("Unexpected network request: " + url); },
  };
  elements.get("viewport").querySelectorAll = selector => selector === "tbody tr.sheet-row" ? window.filterRows : [];
  const original = fs.readFileSync(SHELL, "utf8");
  const marker = '  function mount() {';
  assert.ok(original.includes(marker), "shell startup must remain excluded from the fixture");
  const source = original.slice(0, original.indexOf(marker)) + `
    // Load real shell behavior without application startup or actual API access.
    var actualRenderPayload = renderPayload;
    renderPayload = function (data, preserve) {
      window.renderCalls.push({ data: data, preserve: preserve });
      if (window.renderReal) return actualRenderPayload(data, preserve);
      lastBrowsePayload = data;
      syncSaveToolbar();
    };
    fetchJson = function (url, options) {
      window.requests.push({ url: url, options: options });
      return window.testFetch(url, options);
    };
    syncSheetFiltersFromInputs = function () { return window.filters; };
    scheduleSheetColumnFit = function () {};
    window.hooks = {
      isUnsavedNewRow, addPayloadRow, compareFlatPack, buildBrowseFlatPacks,
      renderFlatTable, applySheetFilters, gatherBrowseSaveRows,
      doSaveBrowseYaml, renderSavedBrowsePayload, reloadBrowseAfterSave,
      getPayload() { return lastBrowsePayload; },
      setPayload(value) { lastBrowsePayload = value; },
      setEditing(value) { sheetEditMode = value; $chkEdit.checked = value; },
      setSort(key, dir) { sheetSortKey = key; sheetSortDir = dir; },
      setLoadedPaths(value) { lastDbCatalogLoadedPaths = value; },
      setTransientState(filters, deleted, enums) {
        persistedSheetFilters = filters; deletedSheetRows = deleted; enumEditorDraft = enums;
      },
      getState() { return { sortKey: sheetSortKey, sortDir: sheetSortDir,
        filters: persistedSheetFilters, deleted: deletedSheetRows, enums: enumEditorDraft }; },
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
  return { ...window.hooks, window, elements };
}

function row(name, overrides = {}) {
  return {
    name, index_in_file: 1, yaml_source_rel: "[JP][TVInfo][2001].yaml",
    domain: "animation", release_type: "tv", country: "japan",
    path: name, date: { start: "20010101", end: "20010331" }, markers: [],
    collectioned_ordered: [], ...overrides,
  };
}

function payload(rows = []) {
  return {
    ok: true, total: rows.length, save: { enabled: true, target_path: "fixture.yaml", target_paths: ["fixture.yaml"] },
    profile_groups: [{ profile_key: "animation-tv", rows }],
  };
}

function addDraft(app, name, overrides = {}) {
  app.addPayloadRow(row(name, overrides));
  return app.getPayload().profile_groups.flatMap(group => group.rows).find(item => item.name === name);
}

function sortedRows(app) {
  return Array.from(app.buildBrowseFlatPacks(app.getPayload().profile_groups)).sort(app.compareFlatPack).map(pack => pack.row);
}

function fixture() {
  const app = setup();
  const existing = [row("Zulu existing"), row("Alpha existing", { index_in_file: 2 })];
  app.setPayload(payload(existing.slice()));
  const first = addDraft(app, "First draft", { date: { start: "20300101", end: "20300331" } });
  const second = addDraft(app, "Second draft", {
    domain: "tv-series", country: "korea", date: { start: "19810101", end: "19810331" },
  });
  const third = addDraft(app, "Third draft", { date: { start: "20220101", end: "20220331" } });
  return { app, existing, drafts: [first, second, third] };
}

function tableRows(html) {
  return Array.from(html.matchAll(/<tr class="sheet-row\b[^>]*>[\s\S]*?<\/tr>/g), match => {
    const attrs = Object.fromEntries(Array.from(match[0].matchAll(/\b(data-sheet-pinned-new|data-sheet-iif|data-yaml-rel|data-sheet-fblob)="([^"]*)"/g), item => [item[1], item[2]]));
    return { html: match[0], attrs, style: { display: "none" }, getAttribute(name) { return this.attrs[name] ?? null; } };
  });
}

test("new rows receive one increasing insertion sequence across profiles and catalog years", () => {
  const { app, drafts } = fixture();
  assert.ok(app.getPayload().profile_groups.length > 1);
  assert.equal(new Set(drafts.map(item => item.yaml_source_rel)).size, 3);
  assert.ok(drafts.every(item => Number.isSafeInteger(item._newRowOrder)));
  assert.ok(drafts[0]._newRowOrder < drafts[1]._newRowOrder);
  assert.ok(drafts[1]._newRowOrder < drafts[2]._newRowOrder);
  assert.deepEqual(sortedRows(app).slice(0, 3), drafts.slice().reverse());
  assert.equal(app.window.requests.length, 0);
});

for (const key of [null, "name", "date_start", "date_end", "domain", "country", "release_type", "markers", "press_fmt_agg", "fmt:BDRip"]) {
  for (const dir of [1, -1]) {
    test(`draft insertion order outranks ${key || "default"} sort in direction ${dir}`, () => {
      const { app, drafts, existing } = fixture();
      app.setSort(key, dir);
      const sorted = sortedRows(app);
      assert.deepEqual(sorted.slice(0, 3), drafts.slice().reverse());
      assert.equal(sorted.length, 5);
      const expectedExisting = existing.map(item => ({ row: item })).sort(app.compareFlatPack).map(pack => pack.row);
      assert.deepEqual(sorted.slice(3), expectedExisting);
      if (key === "name") assert.deepEqual(sorted.slice(3).map(item => item.name), dir === 1 ? ["Alpha existing", "Zulu existing"] : ["Zulu existing", "Alpha existing"]);
    });
  }
}

test("editing a draft's name, dates and profile does not change its insertion order", () => {
  const { app, drafts } = fixture();
  const orders = drafts.map(item => item._newRowOrder);
  Object.assign(drafts[0], { name: "AAAA edited", country: "korea", date: { start: "20991231", end: "21000101" } });
  Object.assign(drafts[2], { name: "ZZZZ edited", date: { start: "19000101", end: "19010101" } });
  for (const key of ["name", "date_start", "date_end", "country"]) {
    for (const dir of [1, -1]) {
      app.setSort(key, dir);
      assert.deepEqual(sortedRows(app).slice(0, 3), drafts.slice().reverse());
    }
  }
  assert.deepEqual(drafts.map(item => item._newRowOrder), orders);
});

test("confirmed saves awaiting refresh rejoin ordinary sorting instead of staying pinned", () => {
  const { app, drafts } = fixture();
  drafts[2]._persistedPendingRefresh = true;
  drafts[2].name = "ZZZZ saved";
  app.setSort("name", 1);
  assert.equal(app.isUnsavedNewRow(null), false);
  assert.equal(app.isUnsavedNewRow(drafts[2]), false);
  assert.deepEqual(sortedRows(app).slice(0, 2), [drafts[1], drafts[0]]);
  assert.equal(sortedRows(app).at(-1), drafts[2]);
});

for (const editing of [false, true]) {
  test(`rendered rows keep only unsaved drafts pinned in ${editing ? "edit" : "read-only"} mode`, () => {
    const { app, drafts } = fixture();
    drafts[0]._persistedPendingRefresh = true;
    app.setEditing(editing);
    app.setSort("name", -1);
    const expected = sortedRows(app);
    const rendered = tableRows(app.renderFlatTable(app.getPayload().profile_groups));
    assert.equal(rendered.length, expected.length);
    assert.deepEqual(rendered.map(item => item.attrs["data-sheet-pinned-new"]), ["1", "1", "0", "0", "0"]);
    for (let index = 0; index < expected.length; index++) {
      assert.equal(rendered[index].attrs["data-sheet-iif"], String(expected[index].index_in_file));
      assert.equal(rendered[index].attrs["data-yaml-rel"], expected[index].yaml_source_rel);
    }
  });
}

for (const [label, filters] of [
  ["text", { name: { filterKind: "substr", needle: "alpha existing" } }],
  ["enum", { country: { filterKind: "enumAny", vals: ["not-a-country"] } }],
]) {
  test(`drafts remain visible under ${label} filters while saved rows follow the filter`, () => {
    const { app, drafts } = fixture();
    drafts[0]._persistedPendingRefresh = true;
    const rendered = tableRows(app.renderFlatTable(app.getPayload().profile_groups));
    app.window.filterRows = rendered;
    app.window.filters = filters;
    app.applySheetFilters();
    const visible = rendered.filter(item => item.style.display !== "none");
    assert.equal(visible.length, label === "text" ? 3 : 2);
    assert.equal(visible.filter(item => item.attrs["data-sheet-pinned-new"] === "1").length, 2);
    assert.equal(rendered.find(item => item.html.includes(">First draft</button>")).style.display, "none");
    assert.match(app.elements.get("sheet-filter-hint").textContent, /待保存新增 2 行已置顶（不受筛选影响）/);
    assert.match(app.elements.get("sheet-filter-hint").textContent, new RegExp(`筛选后可见 ${visible.length} / 5`));
    app.window.filters = {};
    app.applySheetFilters();
    assert.ok(rendered.every(item => item.style.display === ""));
    assert.match(app.elements.get("sheet-filter-hint").textContent, /待保存新增 2/);
    rendered.forEach(item => { item.attrs["data-sheet-pinned-new"] = "0"; });
    app.applySheetFilters();
    assert.equal(app.elements.get("sheet-filter-hint").textContent, "");
  });
}

test("save payload excludes insertion metadata without mutating the in-memory drafts", () => {
  const { app, drafts } = fixture();
  const gathered = app.gatherBrowseSaveRows();
  assert.equal(gathered.new_rows.length, 3);
  assert.doesNotMatch(JSON.stringify(gathered), /_newRowOrder|_isNew|_persistedPendingRefresh/);
  assert.ok(drafts.every(item => item._isNew && Number.isSafeInteger(item._newRowOrder)));
  assert.equal(app.window.requests.length, 0);
});

function response(data, status = 200) {
  return { res: { ok: status >= 200 && status < 300, status }, data };
}

for (const saved of [false, true]) {
  test(`${saved ? "successful save with failed refresh unpins" : "failed save retains"} the real draft`, async () => {
    const app = setup();
    app.setPayload(payload());
    const draft = addDraft(app, "Draft");
    app.setEditing(true);
    app.window.testFetch = async url => {
      if (url === "/api/browse/save") return saved ? response({ ok: true, writes: [] }) : response({ ok: false, error: "Fixture save failure" }, 400);
      if (url === "/api/browse/default") return response({ ok: false, error: "Fixture reload failure" }, 500);
      throw new Error("Unexpected fixture route: " + url);
    };
    await app.doSaveBrowseYaml();
    assert.equal(app.isUnsavedNewRow(draft), !saved);
    assert.equal(app.getPayload().save.enabled, !saved);
    assert.equal(app.window.requests.length, saved ? 2 : 1);
    const submitted = JSON.parse(app.window.requests[0].options.body);
    assert.equal(submitted.new_rows.length, 1);
    assert.doesNotMatch(JSON.stringify(submitted), /_newRowOrder|_isNew/);
    const rendered = tableRows(app.renderFlatTable(app.getPayload().profile_groups));
    assert.equal(rendered[0].attrs["data-sheet-pinned-new"], saved ? "0" : "1");
  });
}

for (const subset of [false, true]) {
  test(`successful ${subset ? "subset" : "full catalog"} reload preserves sort/filter but clears stale editing state`, async () => {
    const app = setup();
    app.setPayload(payload());
    app.setSort("name", -1);
    const filters = { name: { filterKind: "substr", needle: "saved" } };
    app.setTransientState(filters, { oldRecord: { index_in_file: 9, yaml_source_rel: "old.yaml" } }, { obsolete: true });
    app.setLoadedPaths(subset ? ["fixture.yaml"] : null);
    app.window.renderReal = true;
    const refreshed = payload([row("Saved Z"), row("Saved A", { index_in_file: 2 })]);
    app.window.testFetch = async url => {
      assert.equal(url, subset ? "/api/browse/catalog" : "/api/browse/default");
      return response(refreshed);
    };
    assert.equal(await app.reloadBrowseAfterSave(), true);
    const state = app.getState();
    assert.equal(state.sortKey, "name");
    assert.equal(state.sortDir, -1);
    assert.equal(state.filters, filters);
    assert.equal(Object.keys(state.deleted).length, 0);
    assert.equal(state.enums, null);
    assert.equal(app.elements.get("enum-editor-panel").hidden, true);
    assert.equal(app.getPayload(), refreshed);
    assert.equal(app.window.renderCalls.length, 1);
    assert.equal(app.window.renderCalls[0].preserve, true);
    const rendered = tableRows(app.elements.get("viewport").innerHTML);
    assert.equal(rendered.length, 2);
    assert.match(rendered[0].html, />Saved Z<\/button>/);
    assert.ok(rendered.every(item => item.attrs["data-sheet-pinned-new"] === "0"));
  });
}
