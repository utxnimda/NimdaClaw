"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ROOT = path.resolve(__dirname, "../..");
const tick = () => new Promise((resolve) => setImmediate(resolve));

async function setup(options = {}) {
  class Element {
    constructor(id = "") {
      this.id = id; this.value = ""; this.checked = false; this.disabled = false;
      this.hidden = false; this.textContent = ""; this.innerHTML = ""; this.attrs = {};
      this.children = []; this.listeners = new Map();
      this.classList = { add() {}, remove() {}, toggle() {}, contains() { return false; } };
    }
    addEventListener(type, callback) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(callback); }
    removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
    emit(type, event = {}) { [...(this.listeners.get(type) || [])].forEach((callback) => callback({ target: this, stopPropagation() {}, ...event })); }
    setAttribute(name, value) { this.attrs[name] = value; }
    getAttribute(name) { return this.attrs[name] ?? null; }
    removeAttribute(name) { delete this.attrs[name]; }
    get firstChild() { return this.children[0] || null; }
    appendChild(child) { this.children = this.children.filter((item) => item !== child); this.children.push(child); child.parentElement = child.parentNode = this; }
    removeChild(child) { this.children = this.children.filter((item) => item !== child); child.parentElement = child.parentNode = null; }
    contains(child) { return this.children.includes(child); }
    querySelector() { return null; }
    querySelectorAll() { return []; }
  }
  const elements = new Map();
  let rendered = false;
  const document = new Element();
  document.documentElement = new Element();
  document.body = new Element("body");
  document.getElementById = (id) => {
    if (!rendered && !elements.has(id)) elements.set(id, new Element(id));
    return elements.get(id) || null;
  };
  document.createElement = () => new Element();
  const nav = new Element("nav");
  document.querySelector = (selector) => selector === ".app-tabs" ? nav : null;
  nav.appendChild(document.getElementById("tab-collection-detail"));
  nav.appendChild(document.getElementById("tab-collection-info"));
  const requests = [];
  const notices = [];
  const imports = [];
  const storage = new Map();
  const window = new Element();
  window.document = document;
  window.localStorage = { getItem(key) { return storage.get(key) || null; }, setItem(key, value) { storage.set(key, value); }, removeItem(key) { storage.delete(key); } };
  window.NimdaOperationCenter = {
    attachStatus() {},
    notice(message, isError) { notices.push({ message, isError }); },
    async fetchJson(url) {
      requests.push(url);
      if (url === "/api/config") return { res: { ok: true, status: 200 }, data: {
        ok: true, enum_options: options.enumOptions || {}, enum_labels: options.enumLabels || {}, enum_section_labels: {},
        app: { features: [{ id: "collection-detail", label: "作品数据", order: 10 }, { id: "collection-info", label: "收集情况", order: 20 }, { id: "directory-organizer", label: "目录整理", order: 30 }] },
        paths: { catalog_yaml_relpaths: [] }, default_load: { enabled: false },
      } };
      if (url === "/api/collection-info") return { res: { ok: true, status: 200 }, data: { ok: true, records: [], years: [] } };
      throw new Error("Unexpected request: " + url);
    },
  };
  const context = vm.createContext({
    window, document, localStorage: window.localStorage, console, URLSearchParams,
    setTimeout, clearTimeout, requestAnimationFrame() { return 1; }, cancelAnimationFrame() {},
  });
  window.testImport = async (url) => {
    imports.push(url);
    if (options.unmountDuringImport && url.startsWith("./appearance.js")) window.App.beforeUnmount.call(app);
    if (url.startsWith("./operation-center.js")) return;
    const withoutVersion = url.split("?")[0];
    let actual;
    if (withoutVersion.startsWith("/features/")) {
      const [feature, ...asset] = withoutVersion.slice("/features/".length).split("/");
      actual = path.join(ROOT, "features", feature, "frontend", ...asset);
    } else {
      actual = path.join(ROOT, "framework/frontend/src", withoutVersion);
    }
    const moduleSource = fs.readFileSync(actual, "utf8");
    if (withoutVersion === "/features/collection-detail/view.js") {
      vm.runInContext(moduleSource.replace("export default {", "window.ImportedCollectionDetailView = {"), context, { filename: actual });
      return { default: window.ImportedCollectionDetailView };
    }
    vm.runInContext(moduleSource, context, { filename: actual });
  };
  const filename = path.join(ROOT, "framework/frontend/src/App.js");
  const originalApp = fs.readFileSync(filename, "utf8");
  const viewImport = originalApp.match(/^import CollectionDetailView from "([^"]+)";/m);
  assert.ok(viewImport, "the real App must statically import its feature view");
  const importedView = await window.testImport(viewImport[1]);
  assert.equal(importedView.default, window.ImportedCollectionDetailView);
  const source = originalApp
    .replace(viewImport[0], "const CollectionDetailView = window.ImportedCollectionDetailView;")
    .replace("export default {", "window.App = {")
    .replace(/await import\(([^)]+)\)/g, "await window.testImport($1)");
  vm.runInContext(source, context, { filename });
  const templates = [window.App.template, ...Object.values(window.App.components || {}).map((component) => component.template)];
  for (const template of templates) {
    for (const match of template.matchAll(/\bid="([^"]+)"/g)) {
      if (!elements.has(match[1])) elements.set(match[1], new Element(match[1]));
    }
  }
  rendered = true;
  nav.appendChild(document.getElementById("tab-directory-organizer"));
  const app = { ...window.App.data() };
  await window.App.methods.mountFeatureRuntime.call(app);
  await tick();
  return { app, window, document, elements, imports, requests, notices };
}

test("the real frontend bootstrap composes retained features and the new independent organizer", async () => {
  const fixture = await setup();
  assert.equal(fixture.app.featureError, "");
  assert.ok(fixture.app._nimdaRuntime.table);
  assert.ok(fixture.imports.some((url) => url.includes("table-controller.js?v=144")));
  assert.ok(fixture.imports.some((url) => url.includes("appearance.js?v=138")));
  assert.ok(fixture.imports.some((url) => url.includes("feature-host.js?v=138")));
  const commonIndex = fixture.imports.findIndex((url) => url.includes("common/runtime.js"));
  const featureIndex = fixture.imports.findIndex((url) => url.startsWith("/features/collection-info/index.js"));
  assert.ok(commonIndex >= 0 && commonIndex < featureIndex);
  assert.ok(fixture.imports.some((url) => url.includes("common/operation-result.js")));
  assert.ok(fixture.imports.some((url) => url.startsWith("/features/directory-organizer/index.js")));
  const structureIndex = fixture.imports.findIndex((url) => url.startsWith("/features/directory-organizer/structure-preview.js"));
  const organizerIndex = fixture.imports.findIndex((url) => url.startsWith("/features/directory-organizer/index.js"));
  assert.ok(structureIndex >= 0 && structureIndex < organizerIndex, "load the shared structure model before organizer UI");
  assert.equal(typeof fixture.window.NimdaOrganizerStructure.buildPreview, "function");
  const enumIndex = fixture.imports.findIndex((url) => url.includes("common/enum-fields.js"));
  const newWorkIndex = fixture.imports.findIndex((url) => url.startsWith("/features/collection-detail/new-work-dialog.js"));
  assert.ok(enumIndex >= 0 && enumIndex < newWorkIndex && enumIndex < organizerIndex,
    "both DB forms must load after the shared enum field implementation");
  const browserIndex = fixture.imports.findIndex((url) => url.includes("common/directory-browser.js"));
  const recordFormIndex = fixture.imports.findIndex((url) => url.includes("work-record-form.js"));
  assert.ok(browserIndex >= 0 && browserIndex < organizerIndex && recordFormIndex >= 0 && recordFormIndex < organizerIndex);
  assert.equal(typeof fixture.window.NimdaCommon.DirectoryBrowser.mount, "function");
  assert.equal(typeof fixture.window.NimdaWorkRecordForm.applyPatch, "function");
  assert.ok(fixture.imports.every((url) => !url.includes("legacy/shell")));
  assert.deepEqual(Array.from(fixture.window.JpTvBrowseFeatureRegistry.features, (item) => item.id), ["collection-detail", "collection-info", "directory-organizer"]);
  assert.deepEqual(fixture.requests, ["/api/config"]);
  assert.equal(fixture.app._nimdaRuntime.host.getActiveId(), "collection-detail");
  assert.equal(fixture.elements.get("collection-info-view").hidden, true);
  assert.equal(fixture.elements.get("directory-organizer-view").hidden, true);
  fixture.window.App.beforeUnmount.call(fixture.app);
  assert.equal(fixture.app._nimdaRuntime, null);
  assert.equal(fixture.elements.get("btn-reload-cfg").listeners.get("click").size, 0);
});

test("retained collection status opens through the shared host and disposal removes shared listeners", async () => {
  const fixture = await setup();
  assert.equal(fixture.app.featureError, "");
  fixture.elements.get("tab-collection-info").emit("click");
  await tick();
  assert.equal(fixture.app._nimdaRuntime.host.getActiveId(), "collection-info");
  assert.equal(fixture.elements.get("collection-detail-view").hidden, true);
  assert.equal(fixture.elements.get("collection-info-view").hidden, false);
  assert.ok(fixture.requests.includes("/api/collection-info"));
  fixture.elements.get("tab-collection-detail").emit("click");
  assert.equal(fixture.app._nimdaRuntime.host.getActiveId(), "collection-detail");
  fixture.window.App.beforeUnmount.call(fixture.app);
  assert.equal(fixture.elements.get("tab-collection-info").listeners.get("click").size, 0);
  assert.equal(fixture.elements.get("theme-select").listeners.get("change").size, 0);
  assert.equal(fixture.elements.get("font-select").listeners.get("change").size, 0);
});

test("feature context exposes the same canonical enum config used by work-data forms", async () => {
  const enumOptions = { country: [{ value: "japan" }, "korea"], press_format: ["BDRip"] };
  const enumLabels = { country: { japan: "日本", korea: "韩国" } };
  const fixture = await setup({ enumOptions, enumLabels });
  assert.equal(fixture.app.featureError, "");
  const context = fixture.app._nimdaRuntime.table.getFeatureContext();
  assert.equal(context.config.enum_options, context.browseEnumOptions());
  assert.deepEqual(context.config.enum_options, enumOptions);
  assert.deepEqual(context.config.enum_labels, enumLabels);
  assert.equal(context.browseEnumDisplay("country", "korea"), context.config.enum_labels.country.korea);
  fixture.window.App.beforeUnmount.call(fixture.app);
});

test("unmounting during module imports never starts the table or API work afterwards", async () => {
  const fixture = await setup({ unmountDuringImport: true });
  assert.equal(fixture.app.featureError, "");
  assert.equal(fixture.app._nimdaRuntime, undefined);
  assert.deepEqual(fixture.requests, []);
  assert.equal(fixture.elements.get("tab-collection-info").listeners.size, 0);
  assert.equal(fixture.document.listeners.size, 0);
});

test("App composes the feature view without owning its collection configuration template", async () => {
  const fixture = await setup();
  const component = fixture.window.App.components.CollectionDetailView;
  assert.equal(component, fixture.window.ImportedCollectionDetailView);
  assert.equal(component.name, "CollectionDetailView");
  assert.ok(fixture.imports.includes("/features/collection-detail/view.js?v=137"));
  assert.match(fixture.window.App.template, /<CollectionDetailView\s*\/>/);
  assert.doesNotMatch(fixture.window.App.template, /id="(?:config-panel|cfg-fs-root|viewport|collection-detail-view)"|collection-detail-subtabs/);
  assert.deepEqual(Array.from(fixture.window.App.template.matchAll(/\bdata-tab="([^"]+)"/g), (match) => match[1]),
    ["collection-detail", "collection-info", "directory-organizer"]);
  fixture.window.App.beforeUnmount.call(fixture.app);
});

test("organizer tab mounts without disk IO and keeps its draft across host tab changes", async () => {
  const fixture = await setup();
  assert.equal(fixture.app.featureError, "");
  const tab = fixture.elements.get("tab-directory-organizer"), view = fixture.elements.get("directory-organizer-view");
  const descendants = node => [node, ...node.children.flatMap(descendants)];
  const root = descendants(view).find(node => node.attrs["aria-label"] === "待整理目录路径");
  assert.ok(root);
  tab.emit("click"); assert.equal(fixture.app._nimdaRuntime.host.getActiveId(), "directory-organizer");
  assert.equal(view.hidden, false); assert.equal(fixture.elements.get("collection-detail-view").hidden, true);
  root.value = "U:/Manual draft"; root.emit("input");
  fixture.elements.get("tab-collection-detail").emit("click"); tab.emit("click");
  assert.equal(root.value, "U:/Manual draft"); assert.deepEqual(fixture.requests, ["/api/config"]);
  fixture.window.App.beforeUnmount.call(fixture.app);
  assert.equal(tab.listeners.get("click").size, 0); assert.equal(view.children.length, 0);
});

test("feature status messages remain visible and are forwarded into shared operation details", async () => {
  const fixture = await setup();
  const context = fixture.app._nimdaRuntime.table.getFeatureContext();
  context.setStatus("请选择需要处理的文件", true);
  assert.deepEqual(fixture.notices.at(-1), { message: "请选择需要处理的文件", isError: true });
  assert.equal(fixture.elements.get("status-line").textContent, "请选择需要处理的文件");
  assert.match(fixture.elements.get("status-line").className, /err.*operation-status-link/);
  context.setStatus("已完成", false);
  assert.deepEqual(fixture.notices.at(-1), { message: "已完成", isError: false });
  assert.equal(fixture.elements.get("status-line").textContent, "已完成");
  fixture.window.App.beforeUnmount.call(fixture.app);
});

test("the collection view preserves every toolbar, popup and subpanel node required by the real controller", async () => {
  const fixture = await setup();
  const template = fixture.window.App.components.CollectionDetailView.template;
  const expected = `
    collection-detail-view collection-detail-config-slot config-panel
    cfg-used-path cfg-fs-root cfg-history-root cfg-link-shortcut-root cfg-link-layout cfg-link-name
    btn-reload-cfg yaml-file file-meta btn-load-default db-catalog-popover db-catalog-total
    btn-db-catalog-all btn-db-catalog-none db-catalog-load-mode btn-db-catalog-run db-catalog-list
    btn-sheet-save btn-sheet-add-row btn-enum-editor yaml-pick-panel yaml-pick-count
    btn-yaml-pick-all btn-yaml-pick-none btn-yaml-pick-load yaml-pick-list
    enum-editor-panel enum-editor-body btn-enum-save btn-enum-close chk-sheet-edit
    collection-detail-list-panel viewport collection-detail-index-panel collection-detail-link-index-slot
    collection-detail-resource-panel collection-detail-resource-slot
  `.trim().split(/\s+/).sort();
  const actual = Array.from(template.matchAll(/\bid="([^"]+)"/g), (match) => match[1]).sort();
  assert.deepEqual(actual, expected);
  assert.equal(new Set(actual).size, actual.length);
  assert.deepEqual(Array.from(template.matchAll(/\bdata-collection-detail-subtab="([^"]+)"/g), (match) => match[1]),
    ["list", "index", "resource"]);
  assert.match(template, /<main id="collection-detail-view" class="feature-view">/);
  assert.equal(fixture.app.featureError, "");
  assert.equal(fixture.elements.get("btn-reload-cfg").listeners.get("click").size, 1);
  assert.equal(fixture.elements.get("yaml-file").listeners.get("change").size, 1);
  fixture.window.App.beforeUnmount.call(fixture.app);
});
