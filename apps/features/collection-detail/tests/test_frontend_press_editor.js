"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SHELL = path.resolve(__dirname, "../../../framework/frontend/src/legacy/shell.js");

function setup(group = "", mode = "full") {
  const elements = new Map([["status-line", {}]]);
  const document = {
    documentElement: { setAttribute() {}, removeAttribute() {} },
    getElementById(id) { return elements.get(id) || null; },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {},
    body: {
      appendChild(node) { elements.set(node.id, node); node.parentNode = this; },
      removeChild(node) { elements.delete(node.id); node.parentNode = null; },
    },
    createElement() {
      const controls = new Map();
      return {
        innerHTML: "", listeners: {},
        addEventListener(type, callback) { this.listeners[type] = callback; },
        querySelector(selector) {
          if (selector === ".press-editor-panel") return {};
          const id = selector.slice(1);
          if (!controls.has(id)) {
            const select = this.innerHTML.match(new RegExp('<select id="' + id + '"[^>]*>(.*?)</select>'));
            const input = this.innerHTML.match(new RegExp('<input id="' + id + '"[^>]*value="([^"]*)"'));
            const options = select ? [...select[1].matchAll(/<option value="([^"]*)"([^>]*)>/g)] : [];
            const selected = options.find((option) => option[2].includes("selected")) || options[0];
            controls.set(id, { value: selected ? selected[1] : (input ? input[1] : ""), focus() {} });
          }
          return controls.get(id);
        },
      };
    },
  };
  const window = { document, addEventListener() {} };
  const original = fs.readFileSync(SHELL, "utf8");
  const marker = '  $file.addEventListener("change", function () {';
  assert.ok(original.includes(marker));
  const source = original.slice(0, original.indexOf(marker)) + `
    renderPayload = function (payload) { lastBrowsePayload = payload; };
    window.hooks = { enumValueTuplesForEditor, enumSelectForPressEditor, enumEditSelectHtml,
      openPressItemEditor, setBrowseEnumOptions, browseEnumDisplay,
      renderFormatColumnCell, renderPressAggregateColumnCell, sortComparablePressFormatGroups,
      setEditing(value) { sheetEditMode = value; },
      initialize(payload) { lastBrowsePayload = payload; sheetEditMode = true; } };
  })();`;
  vm.runInNewContext(source, { window, document, console,
    localStorage: { getItem() { return null; }, setItem() {} },
  }, { filename: SHELL });
  const row = { index_in_file: 0, yaml_source_rel: "fixture.yaml", path: "Fixture",
    collectioned_ordered: [{ press_format: "BDRip", press_group: group, press_path: "_BDRip", segment: "main" }] };
  window.hooks.initialize({ ok: true, profile_groups: [{ rows: [row] }] });
  window.hooks.setBrowseEnumOptions({ press_format: ["1080p", "BDRip"], press_group: ["JSUM", "VCB"] });
  return { ...window.hooks, row, elements,
    open() {
      assert.equal(window.hooks.openPressItemEditor("fixture.yaml", 0, 0, mode, mode === "split" ? "BDRip" : ""), true);
      return elements.get("press-editor-modal");
    },
  };
}

function trigger(group, enumKey = "press_group") {
  const attrs = { "data-enum-key": enumKey, "data-current-value": group, "data-sheet-iif": "0",
    "data-yaml-rel": "fixture.yaml", "data-source-ord": "0", "data-press-fmt": "BDRip" };
  return { getAttribute(name) { return attrs[name] ?? null; }, classList: { contains() { return true; } } };
}

test("group editor always offers exactly one empty placeholder without changing required format choices", () => {
  const app = setup();
  app.setBrowseEnumOptions({ press_group: ["", "----", "VCB", "VCB"], press_format: ["BDRip"] });
  const groups = app.enumValueTuplesForEditor("press_group", "");
  assert.equal(JSON.stringify(groups), JSON.stringify([{ value: "", label: "—" }, { value: "VCB", label: "VCB" }]));
  assert.doesNotMatch(app.enumSelectForPressEditor("press_format", "", "format", false), /----/);
  assert.match(app.enumSelectForPressEditor("press_format", "", "format", false), /value="BDRip" selected/);
});

test("inline group editor selects blank values and legacy placeholders, preserving unknown existing groups", () => {
  const app = setup();
  for (const group of ["", "----"]) {
    assert.match(app.enumEditSelectHtml(trigger(group)), /<option value="" selected>—<\/option>/);
  }
  const existing = app.enumEditSelectHtml(trigger("LegacyGroup"));
  assert.match(existing, /<option value="">—<\/option>/);
  assert.match(existing, /<option value="LegacyGroup" selected>LegacyGroup<\/option>/);
  assert.doesNotMatch(app.enumEditSelectHtml(trigger("BDRip", "press_format")), /----/);
});

test("clearing a group remains available even without configured group enum values", () => {
  const app = setup();
  app.setBrowseEnumOptions({});
  assert.match(app.enumEditSelectHtml(trigger("VCB")), /<option value="">—<\/option>/);
  assert.match(app.enumSelectForPressEditor("press_group", "", "group", false), /<option value="" selected>—<\/option>/);
});

for (const mode of ["full", "split", "add"]) {
  test(mode + " editor preserves an unspecified group instead of choosing the first configured group", () => {
    const app = setup("", mode);
    const modal = app.open();
    assert.equal(modal.querySelector("#press-editor-grp").value, "");
    modal.listeners.keydown({ key: "Enter", ctrlKey: true, preventDefault() {} });
    assert.equal(app.row.collectioned_ordered.at(-1).press_group, "");
    assert.equal(app.elements.has("press-editor-modal"), false);
  });
}

test("clearing an existing group changes only that group and keeps the format, paths and segment", () => {
  const app = setup("VCB");
  const modal = app.open();
  assert.equal(modal.querySelector("#press-editor-grp").value, "VCB");
  modal.querySelector("#press-editor-grp").value = "";
  modal.listeners.keydown({ key: "Enter", ctrlKey: true, preventDefault() {} });
  assert.equal(JSON.stringify(app.row.collectioned_ordered), JSON.stringify([
    { press_format: "BDRip", press_group: "", press_path: "_BDRip", segment: "main" },
  ]));
  assert.equal(app.row.path, "Fixture");
});

test("legacy and new empty groups render identically across summary, split columns and tooltips without mutating data", () => {
  const app = setup("----");
  const original = JSON.stringify(app.row);
  const emptyRow = JSON.parse(original);
  emptyRow.collectioned_ordered[0].press_group = "";
  for (const editing of [false, true]) {
    app.setEditing(editing);
    const summary = app.renderPressAggregateColumnCell(app.row, ["BDRip"], {}, 0, "fixture.yaml");
    const split = app.renderFormatColumnCell(app.row, "BDRip", 0, "fixture.yaml", false);
    assert.equal(summary, app.renderPressAggregateColumnCell(emptyRow, ["BDRip"], {}, 0, "fixture.yaml"));
    assert.equal(split, app.renderFormatColumnCell(emptyRow, "BDRip", 0, "fixture.yaml", false));
    assert.match(summary, /BDRip \/ —/);
    assert.match(split, />—<\/span>/);
    assert.doesNotMatch(summary + split, /----/);
  }
  assert.equal(app.sortComparablePressFormatGroups(app.row, "BDRip"), "");
  assert.equal(JSON.stringify(app.row), original, "display must not rewrite legacy catalog values");
});

test("placeholder normalization is limited to empty press groups, preserving real names and other enum values", () => {
  const app = setup();
  for (const value of ["", "----", "  ----  ", null]) assert.equal(app.browseEnumDisplay("press_group", value), "—");
  for (const value of ["VCB", "Group-Name", "Group----Name"]) assert.equal(app.browseEnumDisplay("press_group", value), value);
  assert.equal(app.browseEnumDisplay("press_format", "----"), "----");
  assert.equal(app.browseEnumDisplay("markers", "----"), "----");
});
