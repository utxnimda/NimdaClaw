"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SHELL = path.resolve(__dirname, "../../../framework/frontend/src/legacy/shell.js");

function setup() {
  const editCheckbox = { checked: false };
  const viewport = { addEventListener() {}, querySelectorAll() { return []; } };
  const document = {
    documentElement: { setAttribute() {}, removeAttribute() {} },
    getElementById(id) {
      return id === "chk-sheet-edit" ? editCheckbox : (id === "viewport" ? viewport : null);
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
  const window = { document, addEventListener() {} };
  const marker = '  $file.addEventListener("change", function () {';
  const original = fs.readFileSync(SHELL, "utf8");
  assert.ok(original.includes(marker), "shell startup marker must exist");
  // Exercise real shell functions, but never start config reads or load actual data.
  const source = original.slice(0, original.indexOf(marker)) + `
    window.hooks = {
      displayCollectionDate, compactCollectionDate, buildRowFilterBlob,
      packSortComparable, renderSheetScalarField, renderFlatTable,
      beginInlineScalarEdit, sheetColumnHardMin, sheetHeaderNeedCap,
      setEditing(value) { sheetEditMode = value; },
    };
  })();`;
  vm.runInNewContext(source, {
    window, document, console,
    localStorage: { getItem() { return null; }, setItem() {} },
  }, { filename: SHELL });
  return { ...window.hooks, editCheckbox };
}

test("collection dates display compact and legacy ISO values identically without timezone conversion", () => {
  const app = setup();
  for (const value of ["20240930", "2024-09-30", " 20240930 ", 20240930]) {
    assert.equal(app.displayCollectionDate(value), "2024-09-30");
  }
  assert.equal(app.displayCollectionDate("20240229"), "2024-02-29");
  assert.equal(app.displayCollectionDate(""), "");
  assert.equal(app.displayCollectionDate(null), "");
});

test("formatting does not discard unknown components or silently repair invalid legacy dates", () => {
  const app = setup();
  for (const [input, expected] of [
    ["XXXXXXXX", "XXXX-XX-XX"], ["20070000", "2007-00-00"],
    ["20070400", "2007-04-00"], ["20074020", "2007-40-20"],
    ["2024????", "2024-??-??"], ["unknown", "unknown"],
  ]) {
    assert.equal(app.displayCollectionDate(input), expected);
    assert.equal(app.compactCollectionDate(expected), input);
  }
});

test("date sorting uses the same compact key for ISO and compact source values", () => {
  const app = setup();
  assert.equal(app.packSortComparable({ row: { date: { start: "2024-09-30" } } }, "date_start"), "20240930");
  assert.equal(app.packSortComparable({ row: { date: { end: "20240930" } } }, "date_end"), "20240930");
  const sorted = ["20241201", "2024-10-01", "20240930"].sort((left, right) =>
    app.packSortComparable({ row: { date: { start: left } } }, "date_start")
      .localeCompare(app.packSortComparable({ row: { date: { start: right } } }, "date_start")));
  assert.deepEqual(sorted, ["20240930", "2024-10-01", "20241201"]);
});

test("date filters match both displayed dates and the stored eight-character form", () => {
  const app = setup();
  const blob = app.buildRowFilterBlob({ row: { date: { start: "20240930", end: "2024-12-01" } } }, []);
  for (const query of ["2024-09", "202409", "09-30"]) assert.ok(blob.date_start.includes(query));
  for (const query of ["2024-12-01", "20241201"]) assert.ok(blob.date_end.includes(query));
});

test("read-only and editable collection table cells consistently show separated dates", () => {
  const app = setup();
  const groups = [{ profile_label: "Animation", rows: [{ index_in_file: 0, name: "Fixture",
    yaml_source_rel: "fixture.yaml", date: { start: "20240930", end: "XXXXXXXX" } }] }];
  const plain = app.renderFlatTable(groups);
  assert.match(plain, /data-col-key="date_start">2024-09-30<\/td>/);
  assert.match(plain, /data-col-key="date_end">XXXX-XX-XX<\/td>/);
  app.editCheckbox.checked = true;
  const editing = app.renderFlatTable(groups);
  assert.match(editing, /data-field="date_start" data-current-value="2024-09-30">2024-09-30<\/span>/);
  assert.match(editing, /data-field="date_end" data-current-value="XXXX-XX-XX">XXXX-XX-XX<\/span>/);
  assert.equal(groups[0].rows[0].date.start, "20240930", "rendering must not mutate the source payload");
});

test("inline date editing keeps a text field for partial dates and explains the visible format", () => {
  const app = setup();
  app.setEditing(true);
  const attrs = { "data-field": "date_start", "data-current-value": "20240900", "data-sheet-iif": "0" };
  const trigger = {
    getAttribute(name) { return attrs[name] || ""; },
    setAttribute(name, value) { attrs[name] = value; },
    querySelector() { return null; }, innerHTML: "",
  };
  app.beginInlineScalarEdit(trigger);
  assert.match(trigger.innerHTML, /type="text"/);
  assert.match(trigger.innerHTML, /value="2024-09-00"/);
  assert.match(trigger.innerHTML, /placeholder="YYYY-MM-DD"/);
});

test("date columns reserve room for ten-character displayed values", () => {
  const app = setup();
  for (const key of ["date_start", "date_end"]) {
    assert.ok(app.sheetColumnHardMin(key) >= 100);
    assert.ok(app.sheetHeaderNeedCap(key) >= app.sheetColumnHardMin(key));
  }
});
