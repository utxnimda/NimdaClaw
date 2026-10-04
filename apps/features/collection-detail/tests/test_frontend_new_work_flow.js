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
  for (const id of ["chk-sheet-edit", "viewport", "status-line", "btn-sheet-save", "btn-sheet-add-row", "btn-enum-editor", "btn-load-default"]) {
    elements.set(id, {
      checked: false, disabled: false, inert: false, title: "", textContent: "", innerHTML: "",
      addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; },
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
    NimdaNewWorkDialog: { open(options) { dialogs.push(options); } },
    async testFetch(url, options) {
      requests.push({ url, options });
      throw new Error("Unexpected network request: " + url);
    },
  };
  const original = fs.readFileSync(SHELL, "utf8");
  const marker = '  function mount() {';
  assert.ok(original.includes(marker), "shell startup marker must exist");
  const source = original.slice(0, original.indexOf(marker)) + `
    // Rendering is tested elsewhere. Keep this harness focused on the real
    // shell state transitions, and never read config or contact an actual API.
    renderPayload = function (data) { lastBrowsePayload = data; syncSaveToolbar(); };
    fetchJson = function (url, options) { return window.testFetch(url, options); };
    window.hooks = {
      openNewWorkDialog, canAddNewWork, addPayloadRow, gatherBrowseSaveRows,
      sourceHashForRow, removePayloadRow, deletedRowsForSave,
      doSaveBrowseYaml, syncSaveToolbar,
      beginBrowseLoad, finishBrowseLoad, loadDbCatalogPathsFromApi,
      loadAllDbCatalogFromDefault, uploadYamlFiles,
      getPayload() { return lastBrowsePayload; },
      getSaving() { return browseSaving; },
      getLoadedPaths() { return lastDbCatalogLoadedPaths; },
      setPayload(value) { lastBrowsePayload = value; },
      setConfig(value) { lastServerConfig = value; },
      setLoadedPaths(value) { lastDbCatalogLoadedPaths = value; },
      setCatalogPaths(value) { lastCatalogYamlRels = value; },
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

function payload(rows = [], enabled = true) {
  return {
    ok: true, total: rows.length,
    save: { enabled, target_path: "fixture.yaml", target_paths: ["fixture.yaml"] },
    profile_groups: [{ profile_key: "animation-tv", rows }],
  };
}

function patch() {
  return {
    domain: "animation", release_type: "tv", country: "japan", name: "  New work  ",
    path: "New work", date: { start: "2024-09-30", end: "2024-12-30" }, markers: ["Collected"],
    collectioned_ordered: [{ press_format: "BDRip", press_group: "VCB", press_path: "_BDRip(VCB)" }],
  };
}

function response(data, status = 200) {
  return { res: { ok: status >= 200 && status < 300, status }, data };
}

test("opening or cancelling the new-work form never inserts a blank row or writes data", () => {
  const app = setup();
  const initial = payload();
  app.setPayload(initial);
  app.openNewWorkDialog();
  assert.equal(app.dialogs.length, 1);
  assert.equal(app.getPayload(), initial);
  assert.equal(initial.total, 0);
  assert.equal(initial.profile_groups[0].rows.length, 0);
  assert.equal(app.requests.length, 0);
  assert.equal(app.elements.get("chk-sheet-edit").checked, false);
  // Closing the dialog does not invoke onSubmit, so the same state is retained.
  assert.equal(app.getPayload().total, 0);
});

test("confirming the form creates a complete draft, compacts dates and enables editing without writing", () => {
  const app = setup();
  app.setPayload(payload());
  app.openNewWorkDialog();
  const input = patch();
  app.dialogs[0].onSubmit(input);
  const row = app.getPayload().profile_groups[0].rows[0];
  assert.equal(row.name, "New work");
  assert.equal(row._isNew, true);
  assert.equal(row.date.start, "20240930");
  assert.equal(row.date.end, "20241230");
  assert.equal(row.path, "New work");
  assert.equal(row.collectioned_ordered[0].press_path, "_BDRip(VCB)");
  assert.equal(row.yaml_source_rel, app.dialogs[0].targetLabelForDraft(input));
  assert.equal(row.yaml_source_rel, "[JP][TVInfo][2024].yaml");
  assert.equal(app.getPayload().total, 1);
  assert.equal(app.elements.get("chk-sheet-edit").checked, true);
  assert.equal(app.requests.length, 0);
  input.markers.push("Later mutation");
  input.collectioned_ordered[0].press_group = "Other";
  assert.equal(row.markers.length, 1);
  assert.equal(row.collectioned_ordered[0].press_group, "VCB");
});

for (const [date, year] of [["2026-09-21", "2026"], ["2025-01-03", "2025"], ["XXXXXXXX", String(new Date().getFullYear())], ["0000-00-00", String(new Date().getFullYear())]]) {
  test(`Korean new-work draft is routed by country and broadcast year: ${date}`, () => {
    const app = setup();
    app.setConfig({ paths: { filesystem_root: "E:/synthetic-only" } });
    app.openNewWorkDialog();
    const input = { ...patch(), domain: "tv-series", country: "korea", date: { start: date, end: "" } };
    const expected = `[KR][TVInfo][${year}].yaml`;
    assert.equal(app.dialogs[0].targetLabelForDraft(input), expected);
    assert.equal(app.dialogs[0].targetLabelForDraft({ country: "korea", start: date }), expected);
    app.dialogs[0].onSubmit(input);
    const data = app.getPayload();
    assert.equal(data.profile_groups[0].rows[0].yaml_source_rel, expected);
    assert.equal(data.save.target_path, expected);
    assert.equal(app.gatherBrowseSaveRows().new_rows[0].yaml_source_rel, expected);
    assert.equal(app.requests.length, 0);
  });
}

test("a configured empty workspace may open the form without creating a payload until confirmation", () => {
  const app = setup();
  app.setConfig({ paths: { filesystem_root: "E:/synthetic-only" } });
  app.openNewWorkDialog();
  assert.equal(app.dialogs.length, 1);
  assert.equal(app.getPayload(), null);
  app.dialogs[0].onSubmit(patch());
  assert.equal(app.getPayload().total, 1);
  assert.equal(app.getPayload().save.enabled, true);
  assert.equal(app.requests.length, 0);
});

test("a read-only upload cannot be converted into a writable payload by the new-work action", () => {
  const app = setup();
  app.setConfig({ paths: { filesystem_root: "E:/synthetic-only" } });
  const uploaded = payload([], false);
  app.setPayload(uploaded);
  app.openNewWorkDialog();
  assert.equal(app.canAddNewWork(), false);
  assert.equal(app.dialogs.length, 0);
  assert.equal(app.getPayload(), uploaded);
  assert.equal(app.requests.length, 0);
});

test("a form opened for a previous source subset cannot add its draft to a newly loaded subset", () => {
  const app = setup();
  app.setPayload(payload());
  app.openNewWorkDialog();
  const replacement = payload();
  app.setPayload(replacement);
  assert.throws(() => app.dialogs[0].onSubmit(patch()), /重新加载/);
  assert.equal(replacement.total, 0);
  assert.equal(replacement.profile_groups[0].rows.length, 0);
});

test("empty work names fail before creating or mutating a draft payload", () => {
  const app = setup();
  app.setConfig({ paths: { filesystem_root: "E:/synthetic-only" } });
  assert.throws(() => app.addPayloadRow({ name: "  " }), /作品名称/);
  assert.equal(app.getPayload(), null);
});

test("drafts without a materialized table row are still included in the save body", () => {
  const app = setup();
  app.setPayload(payload([{ index_in_file: 7, yaml_source_rel: "fixture.yaml", name: "Existing hidden row" }]));
  app.addPayloadRow(patch());
  const gathered = app.gatherBrowseSaveRows();
  assert.equal(gathered.rows.length, 0, "existing hidden rows retain the established gather behavior");
  assert.equal(gathered.new_rows.length, 1);
  assert.equal(gathered.new_rows[0].name, "New work");
  assert.equal(gathered.new_rows[0].date.start, "20240930");
  assert.equal(gathered.new_rows[0].collectioned_ordered[0].press_group, "VCB");
});

test("existing saved rows use their exact loaded source version while new rows remain unversioned", () => {
  const app = setup();
  const first = { ...patch(), index_in_file: 0, yaml_source_rel: "first/catalog.yaml" };
  const second = { ...patch(), index_in_file: 1, yaml_source_rel: "second\\catalog.yaml" };
  const data = payload([first, second]);
  data.sources_loaded = [
    { relpath: "first/catalog.yaml", sha256: "A".repeat(64) },
    { relpath: "second/catalog.yaml", sha256: "b".repeat(64) },
    { relpath: "[JP][TVInfo][2024].yaml", sha256: "c".repeat(64) },
  ];
  app.setPayload(data);
  app.elements.get("viewport").querySelector = () => ({ querySelector() { return null; }, querySelectorAll() { return []; } });
  app.addPayloadRow(patch());
  const gathered = app.gatherBrowseSaveRows();
  assert.equal(gathered.rows.length, 2);
  assert.equal(gathered.rows[0].source_sha256, "a".repeat(64));
  assert.equal(gathered.rows[1].source_sha256, "b".repeat(64));
  assert.equal(gathered.new_rows.length, 1);
  assert.equal(Object.hasOwn(gathered.new_rows[0], "source_sha256"), false);
  assert.equal(Object.hasOwn(first, "source_sha256"), false, "gathering must not mutate loaded rows");
  assert.equal(app.sourceHashForRow({ yaml_source_rel: "catalog.yaml" }), "", "never fall back to basename");
});

test("deleted existing rows carry the loaded source hash without persisting serialization mutations", () => {
  const app = setup();
  const first = { ...patch(), index_in_file: 0, yaml_source_rel: "nested/catalog.yaml" };
  const data = payload([first]);
  data.sources_loaded = [{ relpath: "nested/catalog.yaml", sha256: "d".repeat(64) }];
  app.setPayload(data);
  assert.equal(app.removePayloadRow("nested\\catalog.yaml", 0), true);
  const deleted = app.deletedRowsForSave();
  assert.equal(deleted.length, 1);
  assert.equal(deleted[0].source_sha256, "d".repeat(64));
  deleted[0].source_sha256 = "mutated caller";
  assert.equal(app.deletedRowsForSave()[0].source_sha256, "d".repeat(64));
  app.addPayloadRow(patch());
  const draft = data.profile_groups.flatMap(group => group.rows).find(row => row._isNew);
  app.removePayloadRow(draft.yaml_source_rel, draft.index_in_file);
  assert.equal(app.deletedRowsForSave().length, 1, "discarded new rows must not become DB deletions");
});

test("legacy source-less fixtures remain compatible for existing updates and deletes", () => {
  const app = setup();
  const first = { ...patch(), index_in_file: 0, yaml_source_rel: "fixture.yaml" };
  app.setPayload(payload([first]));
  app.elements.get("viewport").querySelector = () => ({ querySelector() { return null; }, querySelectorAll() { return []; } });
  assert.equal(Object.hasOwn(app.gatherBrowseSaveRows().rows[0], "source_sha256"), false);
  app.removePayloadRow(first.yaml_source_rel, first.index_in_file);
  assert.equal(Object.hasOwn(app.deletedRowsForSave()[0], "source_sha256"), false);
});

test("a stale catalog save sends original source versions and preserves edits after rejection", async () => {
  const app = setup();
  const first = { ...patch(), name: "Unsaved edit", index_in_file: 0, yaml_source_rel: "fixture.yaml" };
  const second = { ...patch(), name: "Pending deletion", index_in_file: 1, yaml_source_rel: "fixture.yaml" };
  const data = payload([first, second]);
  data.sources_loaded = [{ relpath: "fixture.yaml", sha256: "e".repeat(64) }];
  app.setPayload(data);
  app.setEditing(true);
  app.elements.get("viewport").querySelector = () => ({ querySelector() { return null; }, querySelectorAll() { return []; } });
  app.removePayloadRow(second.yaml_source_rel, second.index_in_file);
  app.window.testFetch = async (url, options) => {
    app.requests.push({ url, options });
    return response({ ok: false, error: "数据库文件已变化，请重新加载或预览" }, 400);
  };
  await app.doSaveBrowseYaml();
  assert.equal(app.requests.length, 1);
  const sent = JSON.parse(app.requests[0].options.body);
  assert.equal(sent.rows[0].source_sha256, "e".repeat(64));
  assert.equal(sent.deleted_rows[0].source_sha256, "e".repeat(64));
  assert.equal(sent.rows[0].name, "Unsaved edit");
  assert.equal(app.getPayload(), data);
  assert.equal(data.save.enabled, true);
  assert.equal(app.deletedRowsForSave().length, 1);
  assert.match(app.elements.get("status-line").textContent, /数据库文件已变化/);
});

test("saving is single-flight, freezes the table and prevents another new-work form until done", async () => {
  const app = setup();
  app.setPayload(payload());
  app.addPayloadRow(patch());
  app.setEditing(true);
  app.setLoadedPaths(["fixture.yaml"]);
  app.setCatalogPaths(["fixture.yaml"]);
  let completeSave;
  app.window.testFetch = function (url, options) {
    app.requests.push({ url, options });
    if (url === "/api/browse/save") return new Promise(resolve => { completeSave = resolve; });
    if (url === "/api/browse/default") return Promise.resolve(response(payload()));
    throw new Error("Unexpected fixture route: " + url);
  };
  const saving = app.doSaveBrowseYaml();
  assert.equal(app.getSaving(), true);
  assert.equal(app.elements.get("viewport").inert, true);
  assert.equal(app.elements.get("btn-sheet-add-row").disabled, true);
  assert.equal(app.canAddNewWork(), false);
  app.openNewWorkDialog();
  assert.equal(app.dialogs.length, 0);
  await app.loadDbCatalogPathsFromApi(["fixture.yaml"]);
  await app.loadAllDbCatalogFromDefault();
  await app.uploadYamlFiles([{ name: "fixture.yaml", size: 1 }]);
  await app.doSaveBrowseYaml();
  assert.equal(app.requests.length, 1);
  assert.equal(JSON.parse(app.requests[0].options.body).new_rows.length, 1);
  completeSave(response({ ok: true, writes: [] }));
  await saving;
  assert.equal(app.getSaving(), false);
  assert.equal(app.elements.get("viewport").inert, false);
  assert.equal(app.requests.filter(req => req.url === "/api/browse/save").length, 1);
  assert.equal(app.getLoadedPaths(), null, "new current-year entries must be included in the refreshed catalog");
});

test("a failed save retains its draft and restores interaction for a deliberate retry", async () => {
  const app = setup();
  app.setPayload(payload());
  app.addPayloadRow(patch());
  app.setEditing(true);
  const initial = app.getPayload();
  app.window.testFetch = async function (url, options) {
    app.requests.push({ url, options });
    return response({ ok: false, error: "Fixture validation failure" }, 400);
  };
  await app.doSaveBrowseYaml();
  assert.equal(app.getPayload(), initial);
  assert.equal(initial.profile_groups[0].rows[0]._isNew, true);
  assert.equal(app.getSaving(), false);
  assert.equal(app.elements.get("viewport").inert, false);
  assert.equal(app.elements.get("btn-sheet-add-row").disabled, false);
  assert.match(app.elements.get("status-line").textContent, /Fixture validation failure/);
});

test("loading a source subset prevents draft confirmation, concurrent loads and saving until it finishes", async () => {
  const app = setup();
  app.setPayload(payload());
  app.openNewWorkDialog();
  app.setEditing(true);
  assert.equal(app.beginBrowseLoad(), true);
  assert.equal(app.canAddNewWork(), false);
  assert.equal(app.elements.get("chk-sheet-edit").disabled, true);
  assert.equal(app.elements.get("btn-sheet-add-row").disabled, true);
  assert.equal(app.beginBrowseLoad(), false);
  assert.throws(() => app.dialogs[0].onSubmit(patch()), /重新加载/);
  await app.doSaveBrowseYaml();
  assert.equal(app.requests.length, 0);
  assert.equal(app.getPayload().total, 0);
  app.finishBrowseLoad();
  assert.equal(app.canAddNewWork(), true);
  assert.equal(app.elements.get("chk-sheet-edit").disabled, false);
  assert.equal(app.elements.get("btn-sheet-add-row").disabled, false);
});

for (const failure of ["http", "network"]) {
  test(`a confirmed save followed by a ${failure} refresh failure cannot append its draft a second time`, async () => {
    const app = setup();
    app.setPayload(payload());
    app.addPayloadRow(patch());
    app.setEditing(true);
    const initial = app.getPayload();
    app.window.testFetch = async function (url, options) {
      app.requests.push({ url, options });
      if (url === "/api/browse/save") return response({ ok: true, writes: [] });
      if (failure === "network") throw new Error("Fixture connection failure");
      return response({ ok: false, error: "Fixture refresh failure" }, 500);
    };
    await app.doSaveBrowseYaml();
    assert.equal(app.getPayload(), initial);
    assert.equal(initial.save.enabled, false);
    assert.equal(initial.profile_groups[0].rows[0]._persistedPendingRefresh, true);
    assert.equal(app.canAddNewWork(), false);
    assert.equal(app.elements.get("btn-sheet-save").disabled, true);
    assert.equal(app.elements.get("viewport").inert, false);
    assert.match(app.elements.get("status-line").textContent, /数据已经保存.*刷新列表失败/);
    await app.doSaveBrowseYaml();
    assert.equal(app.requests.filter(req => req.url === "/api/browse/save").length, 1);
    assert.equal(app.requests.length, 2);
  });
}
