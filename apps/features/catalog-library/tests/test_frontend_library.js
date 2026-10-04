"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const SOURCE = fs.readFileSync(path.join(__dirname, "../frontend/index.js"), "utf8");
const WORK_FORM = fs.readFileSync(path.join(__dirname, "../../collection-detail/frontend/work-record-form.js"), "utf8");
const CSS = fs.readFileSync(path.join(__dirname, "../frontend/style.css"), "utf8");
const plain = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const flush = () => new Promise(resolve => setImmediate(resolve));
const ref = { yaml_source_rel: "2026.yaml", index_in_file: 0, source_sha256: "a".repeat(64), record_sha256: "b".repeat(64) };
const record = { attributes: [{ type: "name", data: "Test" }, { type: "country", data: "japan" }, { type: "date", data: { start: "20260000", end: "" } },
  { type: "collection-type", data: { domain: "animation", release_type: "tv", path: "T:/Test", collectioned: [], continuations: [{ title: "Extras", collectioned: [] }] } }],
  metadata: { summary: "Preserve", aliases: ["Alias"] }, extension: { custom: true } };
const item = { id: "legacy:2026.yaml#0", stable_id: false, ref, name: "Test", domain: "animation", domain_label: "动画", year: "2026", press_count: 0, classifications: [] };
const browse = () => ({ ok: true, items: [item], total: 1, page: 1, page_size: 24, legacy_count: 1, classification_revision: "revision-1",
  facets: { domains: [{ value: "animation", label: "动画", count: 1 }], years: [{ value: "2026", count: 1 }], classifications: [{ id: "series_1", name: "My series", type: "series", count: 0 }] } });
const detail = () => ({ ok: true, item, record: plain(record), ref, resources: [], chapters: [], classifications: [], sources: [], metadata: record.metadata });

function fixture(handler, extra = {}) {
  const registrations = [], calls = [], notices = [], changes = [];
  const window = { NimdaCommon: { ensureFeatureRegistry: () => ({ register: value => registrations.push(value) }) }, ...extra.window };
  const globals = { window, ...extra.globals };
  vm.runInNewContext(WORK_FORM, globals);
  vm.runInNewContext(SOURCE, globals);
  async function request(url, options) {
    const route = url.replace("/api/catalog-library/", ""), body = JSON.parse(options.body); calls.push({ route, body });
    if (handler) { const result = await handler(route, body); if (result !== undefined) return result; }
    if (route === "browse") return browse();
    if (route === "detail") return detail();
    if (route === "edits/preview") return { ok: true, edits: body.edits, issues: [] };
    if (route === "edits/apply") return { ok: true, db_committed: true, records: [{ ref: { ...ref, source_sha256: "c".repeat(64) }, record }] };
    throw Error("Unexpected route " + route);
  }
  const controller = window.NimdaCatalogLibrary.createController({ request, setStatus: (value, error) => notices.push({ value, error }), onChange: (_state, reason) => changes.push(reason) });
  return { window, registrations, calls, notices, changes, controller, request };
}

test("isolated feature registration and controller construction do not initialize data", async () => {
  const f = fixture(); assert.equal(f.registrations[0].id, "catalog-library"); assert.equal(f.registrations[0].order, 5);
  assert.equal(f.calls.length, 0); await f.controller.activate();
  assert.deepEqual(f.calls.map(call => call.route), ["browse"]); assert.equal(f.controller.state.identityMissing, 1);
  assert.equal(f.controller.state.classificationRevision, "revision-1");
});

test("filter requests ignore older success and failure responses", async () => {
  const old = deferred(), newer = deferred();
  const f = fixture((route, body) => route === "browse" && body.query ? body.query === "old" ? old.promise : newer.promise : undefined);
  await f.controller.activate(); const one = f.controller.setFilters({ query: "old" }); const two = f.controller.setFilters({ query: "new" });
  newer.resolve({ ...browse(), items: [{ ...item, name: "Newest" }] }); await two;
  old.reject(Error("Stale failure")); await one;
  assert.equal(f.controller.state.items[0].name, "Newest"); assert.equal(f.controller.state.error, ""); assert.equal(f.controller.state.loading, false);
});

test("leaving a feature invalidates pending browsing and reloads on return", async () => {
  const pending = deferred(); let first = true;
  const f = fixture(route => { if (route === "browse" && first) { first = false; return pending.promise; } });
  const active = f.controller.activate(); f.controller.deactivate(); pending.resolve({ ...browse(), items: [{ ...item, name: "Stale" }] }); await active;
  assert.equal(f.controller.state.items.length, 0); assert.equal(f.controller.state.loading, false);
  await f.controller.activate(); assert.equal(f.controller.state.items[0].name, "Test");
});

test("legacy detail navigation carries full source and record version references", async () => {
  const f = fixture(); await f.controller.activate(); await f.controller.openDetail(item);
  const call = f.calls.find(call => call.route === "detail"); assert.deepEqual(call.body, { id: item.id, ref });
});

test("late detail result never replaces another selected work", async () => {
  const pending = deferred();
  const f = fixture((route, body) => route === "detail" && body.id === "slow" ? pending.promise : undefined);
  await f.controller.activate(); const slow = f.controller.openDetail({ id: "slow" }); await f.controller.openDetail(item);
  pending.resolve({ ...detail(), item: { ...item, name: "Wrong" } }); await slow;
  assert.equal(f.controller.state.detail.item.name, "Test"); assert.equal(f.controller.state.detailLoading, false);
});

test("record save preserves shared full record payload and uses fresh returned ref", async () => {
  const f = fixture(); await f.controller.activate(); await f.controller.openDetail(item); await f.controller.saveRecord(record, ref);
  const writes = f.calls.filter(call => call.route === "edits/apply"); assert.equal(writes.length, 1); assert.deepEqual(writes[0].body.edits[0].record, record);
  const lastDetail = f.calls.filter(call => call.route === "detail").at(-1); assert.equal(lastDetail.body.ref.source_sha256, "c".repeat(64));
});

test("blocking preview does not apply an edit and displays failure", async () => {
  const f = fixture(route => route === "edits/preview" ? { ok: true, issues: [{ blocking: true, message: "conflict" }] } : undefined);
  await f.controller.activate(); await assert.rejects(f.controller.saveRecord(record, ref), /conflict/);
  assert.equal(f.calls.some(call => call.route === "edits/apply"), false); assert.equal(f.notices.at(-1).error, true); assert.equal(f.controller.state.busy, false);
});

test("only one write can execute while a mutation is pending", async () => {
  const pending = deferred();
  const f = fixture(route => route === "edits/preview" ? pending.promise : undefined); await f.controller.activate();
  const saving = f.controller.saveRecord(record, ref); await f.controller.saveRecord(record, ref);
  pending.resolve({ ok: true, edits: [{ ref, record }], issues: [] }); await saving;
  assert.equal(f.calls.filter(call => call.route === "edits/apply").length, 1);
});

test("deactivation between edit preview and apply stops the write", async () => {
  const pending = deferred(); const f = fixture(route => route === "edits/preview" ? pending.promise : undefined); await f.controller.activate();
  const saving = f.controller.saveRecord(record, ref); f.controller.deactivate(); pending.resolve({ edits: [{ ref, record }], issues: [] });
  await assert.rejects(saving, /页面已变化/); assert.equal(f.calls.some(call => call.route === "edits/apply"), false);
});

test("stable IDs require explicit preview then apply using common edits route", async () => {
  const f = fixture(route => route === "identities/preview" ? { ok: true, edits: [{ ref, record: { ...record, id: "work_new" } }], issues: [] } : undefined);
  await f.controller.activate(); await f.controller.previewIdentities(); assert.equal(f.calls.some(call => call.route === "edits/apply"), false);
  await f.controller.applyIdentities(); assert.equal(f.calls.filter(call => call.route === "edits/apply").length, 1); assert.equal(f.controller.state.identityPreview, null);
});

test("classification membership previews scoped ref and saves through common writer", async () => {
  const f = fixture(route => route === "classification-edits/preview" ? { ok: true, edits: [{ ref, record }], issues: [] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.assignClassification("series_1", "assign");
  assert.deepEqual(f.calls.find(call => call.route === "classification-edits/preview").body, { refs: [ref], value_id: "series_1", action: "assign" });
  assert.equal(f.calls.filter(call => call.route === "edits/apply").length, 1);
});

test("classification creation and rename carry dictionary revision", async () => {
  const f = fixture(route => route === "classifications" ? { ok: true } : undefined); await f.controller.activate();
  await f.controller.saveClassification("New series"); await f.controller.saveClassification("Renamed", "series_1");
  assert.deepEqual(f.calls.filter(call => call.route === "classifications").map(call => call.body), [
    { revision: "revision-1", type: "series", name: "New series" }, { revision: "revision-1", type: "series", name: "Renamed", id: "series_1" }]);
  await assert.rejects(f.controller.saveClassification("  "), /请输入系列名称/); assert.equal(f.notices.at(-1).error, true);
});

test("provider confirmation keeps snapshot token and exact preview ref; no fields preselected", async () => {
  const imported = { ...ref, record_sha256: "e".repeat(64) };
  const f = fixture((route, body) => {
    if (route === "provider/preview") return { ok: true, ref: imported, snapshot_id: "snapshot", preview_token: "verified", diff: [
      { field: "metadata.summary", selectable: true, changed: true, before: "old", after: "new" }, { field: "path", selectable: false, changed: true }] };
    if (route === "provider/apply") return { ok: true, records: [{ ref, record }] };
  });
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.previewProvider("123");
  assert.deepEqual(plain(f.controller.state.provider.selected), []);
  f.controller.selectProviderField("path", true); f.controller.selectProviderField("arbitrary", true); f.controller.selectProviderField("metadata.summary", true);
  assert.deepEqual(plain(f.controller.state.provider.selected), ["metadata.summary"]);
  await f.controller.applyProvider(); assert.deepEqual(f.calls.find(call => call.route === "provider/apply").body,
    { ref: imported, snapshot_id: "snapshot", preview_token: "verified", selected_fields: ["metadata.summary"] });
});

test("provider supports explicit source-only confirmation without replacing fields", async () => {
  const f = fixture(route => route === "provider/preview" ? { ref, snapshot_id: "snapshot", preview_token: "verified", diff: [] } : route === "provider/apply" ? { records: [{ ref, record }] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.previewProvider(123); await f.controller.applyProvider();
  assert.deepEqual(f.calls.find(call => call.route === "provider/apply").body.selected_fields, []);
});

test("closed detail ignores late provider results", async () => {
  const pending = deferred(); const f = fixture(route => route === "provider/search" ? pending.promise : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); const searching = f.controller.searchProvider("Test"); f.controller.closeDetail();
  pending.resolve({ results: [{ id: 123, name: "Late" }] }); await searching;
  assert.equal(f.controller.state.provider.results.length, 0); assert.equal(f.controller.state.provider.loading, false);
});

test("episode range is optional and bounded preview sends finite numeric limits only", async () => {
  const f = fixture(route => route === "provider/preview" ? { ref, snapshot_id: "snapshot", preview_token: "token", diff: [] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.previewProvider(123);
  assert.equal(Object.hasOwn(f.calls.find(call => call.route === "provider/preview").body, "episode_range"), false);
  f.controller.setEpisodeRange("0", "12.5"); await f.controller.previewProvider(123);
  assert.deepEqual(f.calls.filter(call => call.route === "provider/preview").at(-1).body.episode_range, { start: 0, end: 12.5 });
  assert.deepEqual(plain(f.controller.state.provider.selected), []);
});

test("incomplete, reversed and nonfinite episode ranges display validation without fetching", async () => {
  const f = fixture(); await f.controller.activate(); await f.controller.openDetail(item);
  for (const [start, end] of [["1", ""], ["", "12"], ["-1", "12"], ["13", "12"], ["0", "Infinity"], ["NaN", "12"], ["1", "1e309"], ["0x10", "20"]]) {
    f.controller.setEpisodeRange(start, end); await f.controller.previewProvider(123);
    assert.ok(f.controller.state.provider.error, `Expected range validation for ${start},${end}`); assert.equal(f.controller.state.provider.loading, false);
  }
  assert.equal(f.calls.some(call => call.route === "provider/preview"), false);
});

test("range changes invalidate displayed token and selected fields before apply", async () => {
  const f = fixture(route => route === "provider/preview" ? { ref, snapshot_id: "snapshot", preview_token: "token", diff: [{ field: "metadata.episodes", changed: true, selectable: true }] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.previewProvider(123);
  f.controller.selectProviderField("metadata.episodes", true); f.controller.setEpisodeRange("1", "12");
  assert.equal(f.controller.state.provider.preview, null); assert.deepEqual(plain(f.controller.state.provider.selected), []);
  await assert.rejects(f.controller.applyProvider(), /预览差异/); assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
});

test("range changes discard in-flight preview results and never restore stale token", async () => {
  const pending = deferred(); const f = fixture(route => route === "provider/preview" ? pending.promise : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); f.controller.setEpisodeRange("1", "12");
  const previewing = f.controller.previewProvider(123); f.controller.setEpisodeRange("13", "24");
  pending.resolve({ ref, snapshot_id: "old", preview_token: "old-token", diff: [], episode_range: { start: 1, end: 12 } }); await previewing;
  assert.equal(f.controller.state.provider.preview, null); assert.equal(f.controller.state.provider.rangeStart, "13"); assert.equal(f.controller.state.provider.loading, false);
});

test("resource action reuses existing endpoint and is single-flight without preflight scans", async () => {
  const pending = deferred(); const f = fixture(route => route === "/api/collection-detail/press/open" ? pending.promise : undefined);
  await f.controller.activate(); await f.controller.openDetail(item);
  const opening = f.controller.openResource("T:/Test", "BDRip"); await f.controller.openResource("T:/Test", "BDRip");
  assert.deepEqual(f.calls.filter(call => call.route === "/api/collection-detail/press/open"), [{ route: "/api/collection-detail/press/open", body: { path: "T:/Test", press_path: "BDRip" } }]);
  pending.resolve({ ok: false, error: "目录不可用" }); await opening;
  assert.equal(f.notices.at(-1).value, "目录不可用"); assert.equal(f.notices.at(-1).error, true); assert.equal(f.controller.state.resourceOpening, false);
  assert.equal(f.calls.some(call => /scan|inventory/.test(call.route)), false);
});

test("cover gate rejects remote, data, encoded, traversal and scheme-relative URLs", () => {
  const f = fixture(), cover = f.window.NimdaCatalogLibrary.localCover;
  for (const value of ["https://example.com/a.jpg", "//example.com/a.jpg", "data:image/svg+xml,x", "javascript:alert(1)", "/api/catalog-library/assets/../x", "/api/catalog-library/assets/%2e%2e/x", "/api/catalog-library/assets//x"]) assert.equal(cover(value), "");
  for (const extension of ["jpg", "png", "gif", "webp"]) { const url = "/api/catalog-library/assets/" + "a".repeat(64) + "." + extension; assert.equal(cover(url), url); }
  for (const value of ["/api/catalog-library/assets/abc123.jpg", "/api/catalog-library/assets/" + "a".repeat(64) + ".svg", "/api/catalog-library/assets/" + "a".repeat(64) + ".jpg?remote=x"]) assert.equal(cover(value), "");
});

test("page grids and detail panels use one main scroll, with narrow and theme styles", () => {
  assert.match(CSS, /max-height:\s*none;\s*overflow:\s*visible/);
  assert.doesNotMatch(CSS, /overflow(?:-y)?:\s*(?:auto|scroll)/);
  assert.match(CSS, /max-width: 480px/); assert.match(CSS, /var\(--text/); assert.match(CSS, /:focus-visible/);
  assert.doesNotMatch(SOURCE, /innerHTML\s*=/);
});

function domFixture(handler) {
  let document;
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.className = ""; this.value = ""; this.textContent = ""; this.hidden = false; this.disabled = false; }
    appendChild(node) { this.children.push(node); node.parentNode = this; return node; }
    removeChild(node) { this.children = this.children.filter(child => child !== node); node.parentNode = null; }
    remove() { if (this.parentNode) this.parentNode.removeChild(this); }
    get firstChild() { return this.children[0]; }
    get isConnected() { return this === document.body || !!(this.parentNode && this.parentNode.isConnected); }
    get classList() { return { add: value => { if (!this.className.split(" ").includes(value)) this.className += " " + value; } }; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    focus() { document.activeElement = this; }
    emit(name, extra = {}) { const event = { target: this, preventDefault() {}, ...extra }; (this.listeners[name] || []).forEach(callback => callback(event)); }
  }
  const flatten = node => [node, ...node.children.flatMap(flatten)];
  document = { createElement: tag => new Element(tag), listeners: {}, addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); },
    removeEventListener(name, callback) { this.listeners[name] = (this.listeners[name] || []).filter(value => value !== callback); },
    getElementById(id) { return flatten(this.body).find(node => node.id === id) || null; },
    querySelector(selector) { if (selector === "dialog[open]") return null; return flatten(this.body).find(node => node.className.split(" ").includes(selector.slice(1))) || null; } };
  document.body = new Element("body"); const shell = document.body.appendChild(new Element("div")); shell.className = "app-shell";
  shell.appendChild(new Element("nav")).className = "app-tabs";
  let editor;
  const f = fixture((route, body) => handler && handler(route, body) || (route === "browse" ? { ...browse(), items: [{ ...item, name: "<img src=x onerror=alert(1)>" }] } : undefined),
    { window: { NimdaNewWorkDialog: { open(options) { editor = options; } } }, globals: { document } });
  const feature = f.registrations[0], context = { fetchJson: f.request, config: {}, setStatus() {} };
  const nodes = () => flatten(document.body);
  const findButton = label => nodes().find(node => node.tagName === "button" && node.textContent === label);
  return { ...f, feature, context, document, nodes, findButton, editor: () => editor };
}

test("DOM init is passive, display text is safe, keyboard detail back restores card focus", async () => {
  const f = domFixture(); f.feature.init(f.context); assert.equal(f.calls.length, 0); f.feature.activate(f.context); await flush();
  const unsafe = f.nodes().find(node => node.textContent === "<img src=x onerror=alert(1)>"); assert.equal(unsafe.tagName, "h3");
  assert.equal(f.nodes().some(node => node.tagName === "img"), false);
  const card = f.nodes().find(node => node.className.includes("library-card-link")); card.focus(); card.emit("click"); await flush();
  assert.equal(f.document.activeElement.tagName, "h1");
  const tab = f.document.getElementById("library-detail-tab-overview"); tab.emit("keydown", { key: "ArrowRight" });
  assert.equal(f.document.activeElement.id, "library-detail-tab-resources");
  f.document.listeners.keydown[0]({ key: "Escape", preventDefault() {} });
  assert.ok(f.document.activeElement.className.includes("library-card-link"));
  f.feature.dispose(); assert.equal(f.document.listeners.keydown.length, 0);
});

test("shared work form edits preserve local metadata and continuation structure", async () => {
  const f = domFixture(); f.feature.init(f.context); f.feature.activate(f.context); await flush();
  f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  f.findButton("编辑作品").emit("click"); await flush(); const editor = f.editor(); assert.ok(editor); assert.equal(editor.initialData.date.start, "20260000");
  await editor.onSubmit({ name: "Changed" });
  const saved = f.calls.find(call => call.route === "edits/apply").body.edits[0].record;
  assert.deepEqual(saved.extension, record.extension); assert.deepEqual(saved.metadata, record.metadata);
  assert.deepEqual(saved.attributes.find(attr => attr.type === "collection-type").data.continuations, record.attributes[3].data.continuations);
});

test("shared editor rejects submit after leaving the feature", async () => {
  const f = domFixture(); f.feature.init(f.context); f.feature.activate(f.context); await flush();
  f.findButton("＋ 新增作品").emit("click"); await flush(); f.feature.deactivate();
  await assert.rejects(f.editor().onSubmit({ name: "Stale" }), /页面已变化/); assert.equal(f.calls.some(call => call.route === "edits/apply"), false);
});

test("source UI searches once and folds long diffs without preselecting provider fields", async () => {
  const chapters = Array.from({ length: 500 }, (_, index) => ({ title: "Episode " + index }));
  const f = domFixture(route => route === "provider/search" ? { results: [{ id: 123, name: "<script>source</script>" }] } : route === "provider/preview" ? {
    ref, snapshot_id: "snapshot", preview_token: "token", diff: [{ field: "metadata.episodes", label: "章节", before: [], after: chapters, selectable: true, changed: true }] } : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush();
  f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  f.findButton("来源").emit("click"); await flush();
  const input = f.document.getElementById("library-provider-query"); input.value = "Test"; input.focus();
  f.nodes().find(node => node.className === "library-provider-search").emit("submit"); await flush();
  assert.equal(f.calls.filter(call => call.route === "provider/search").length, 1); assert.equal(f.document.activeElement.id, "library-provider-query");
  f.findButton("预览差异").emit("click"); await flush();
  assert.equal(f.nodes().find(node => node.type === "checkbox").checked, false);
  const expanded = f.nodes().find(node => node.tagName === "details" && node.children[0].textContent.includes("500 条记录")); assert.ok(expanded); assert.equal(expanded.open, undefined);
  assert.ok(f.findButton("仅确认来源关联")); assert.equal(f.nodes().some(node => node.tagName === "script"), false);
  assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
});

test("source range controls show selected scope/counts and retire preview on edit", async () => {
  const f = domFixture((route, body) => route === "provider/search" ? { results: [{ id: 123, name: "Source" }] } : route === "provider/preview" ? {
    ref, snapshot_id: "snapshot", preview_token: "token", episode_range: body.episode_range || null, episode_counts: { selected: body.episode_range ? 12 : 26, total: 26 }, diff: [] } : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush();
  f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush(); f.findButton("来源").emit("click"); await flush();
  const search = f.document.getElementById("library-provider-query"); search.value = "Source";
  f.nodes().find(node => node.className === "library-provider-search").emit("submit"); await flush();
  f.findButton("预览差异").emit("click"); await flush(); assert.match(f.nodes().find(node => node.className === "library-preview-scope").textContent, /完整条目.*26 \/ 26/);
  let start = f.document.getElementById("library-episode-start"); start.value = "1"; start.focus(); start.emit("input");
  assert.equal(f.document.activeElement.id, "library-episode-start"); assert.equal(f.findButton("仅确认来源关联"), undefined);
  f.findButton("预览差异").emit("click"); await flush(); assert.ok(f.nodes().find(node => node.className === "library-error" && node.textContent.includes("同时填写")));
  const end = f.document.getElementById("library-episode-end"); end.value = "12"; end.emit("input");
  f.findButton("预览差异").emit("click"); await flush(); assert.match(f.nodes().find(node => node.className === "library-preview-scope").textContent, /正片第 1–12 集.*12 \/ 26/);
  assert.deepEqual(f.calls.filter(call => call.route === "provider/preview").at(-1).body.episode_range, { start: 1, end: 12 });
});

test("technical identity is folded and only fully bound resources offer open directory", async () => {
  const f = domFixture(route => route === "detail" ? { ...detail(), resources: [{ press_format: "BDRip", press_path: "BDRip" }, { press_format: "WEB", press_path: "" }] } : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush(); f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  const identity = f.nodes().find(node => node.className === "library-data-identity"); assert.ok(identity); assert.equal(identity.tagName, "details"); assert.equal(identity.open, undefined);
  assert.equal(identity.children[0].textContent, "数据标识"); f.findButton("资源").emit("click"); await flush();
  assert.equal(f.nodes().filter(node => node.tagName === "button" && node.textContent === "打开目录").length, 1);
  assert.equal(f.calls.some(call => call.route === "/api/collection-detail/press/open"), false);
});

test("cover-only preview is explicit, single-flight and applies the signed snapshot only after selection", async () => {
  const pending = deferred(), f = fixture(route => route === "provider/cover-preview" ? pending.promise : route === "provider/apply" ? { records: [{ ref, record }] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); f.controller.setEpisodeRange("1", "12");
  const previewing = f.controller.previewCover("123"); await f.controller.previewCover("123");
  assert.deepEqual(f.calls.filter(call => call.route === "provider/cover-preview"), [{ route: "provider/cover-preview", body: { ref, subject_id: "123" } }]);
  pending.resolve({ ref, snapshot_id: "cover-snapshot", preview_token: "cover-token", cover_only: true, diff: [{ field: "metadata.cover", changed: true, selectable: true }] }); await previewing;
  assert.equal(f.controller.state.detailTab, "sources"); assert.deepEqual(plain(f.controller.state.provider.selected), []);
  await assert.rejects(f.controller.applyProvider(), /勾选封面/); assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
  f.controller.selectProviderField("metadata.cover", true); await f.controller.applyProvider();
  assert.deepEqual(f.calls.find(call => call.route === "provider/apply").body, { ref, snapshot_id: "cover-snapshot", preview_token: "cover-token", selected_fields: ["metadata.cover"] });
});

test("closing a work discards late cover preview and failure never replaces current cover", async () => {
  const pending = deferred(), coverUrl = "/api/catalog-library/assets/" + "a".repeat(64) + ".jpg";
  const f = fixture(route => route === "provider/cover-preview" ? pending.promise : route === "detail" ? { ...detail(), item: { ...item, cover_url: coverUrl } } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); const previewing = f.controller.previewCover();
  assert.equal(f.controller.state.detail.item.cover_url, coverUrl); f.controller.closeDetail();
  pending.resolve({ ref, snapshot_id: "late", preview_token: "late", diff: [] }); await previewing;
  assert.equal(f.controller.state.provider.preview, null); assert.equal(f.controller.state.detail, null);
  const failed = fixture(route => route === "provider/cover-preview" ? { ok: false, error: "封面下载失败：TLS 连接被中断" } : route === "detail" ? { ...detail(), item: { ...item, cover_url: coverUrl } } : undefined);
  await failed.controller.activate(); await failed.controller.openDetail(item); await failed.controller.previewCover();
  assert.match(failed.controller.state.provider.error, /TLS/); assert.equal(failed.controller.state.detail.item.cover_url, coverUrl);
});

test("bound work has explicit cover update; preview renders local images with fallback and no automatic apply", async () => {
  const before = "/api/catalog-library/assets/" + "a".repeat(64) + ".jpg", after = "/api/catalog-library/assets/" + "b".repeat(64) + ".png";
  const f = domFixture(route => route === "detail" ? { ...detail(), item: { ...item, cover_url: before }, sources: [{ provider: "bangumi", external_id: "123" }] } : route === "provider/cover-preview" ? {
    ref, snapshot_id: "cover", preview_token: "token", cover_only: true, diff: [{ field: "metadata.cover", label: "封面", before_url: before, after_url: after, selectable: true, changed: true }] } : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush(); f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  f.findButton("更新封面").emit("click"); await flush();
  assert.equal(f.findButton("确认更新封面").disabled, true); assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
  const images = f.nodes().filter(node => node.tagName === "img"); assert.ok(images.some(image => image.src === after));
  assert.ok(images.every(image => image.loading === "lazy" && image.width > 0 && image.height > 0 && image.alt));
  const candidate = images.find(image => image.src === after), container = candidate.parentNode; candidate.emit("error");
  assert.equal(container.children.some(node => node.tagName === "img"), false); assert.ok(container.children.some(node => node.className === "library-cover-placeholder"));
  const checkbox = f.nodes().find(node => node.type === "checkbox"); checkbox.checked = true; checkbox.emit("change"); assert.equal(f.findButton("确认更新封面").disabled, false);
});

test("malicious remote preview image URLs remain text-only placeholders", async () => {
  const f = domFixture(route => route === "detail" ? { ...detail(), sources: [{ provider: "bangumi", external_id: "123" }] } : route === "provider/cover-preview" ? {
    ref, snapshot_id: "cover", preview_token: "token", diff: [{ field: "metadata.cover", before_url: "https://example.com/old.jpg", after_url: "//example.com/new.jpg", selectable: false, changed: false }] } : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush(); f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  f.findButton("更新封面").emit("click"); await flush(); assert.equal(f.nodes().some(node => node.tagName === "img"), false); assert.equal(f.findButton("确认更新封面").disabled, true);
});

test("browse keeps cards during pending refresh and renders bounded pagination in a sidebar layout", async () => {
  const pending = deferred(); let requests = 0;
  const f = domFixture(route => route === "browse" ? (++requests === 1 ? { ...browse(), total: 1520 } : pending.promise) : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush();
  const card = f.nodes().find(node => node.className.includes("library-card-link"));
  const sidebar = f.nodes().find(node => node.className === "library-browse-sidebar"), layout = f.nodes().find(node => node.className === "library-browse-layout");
  assert.equal(sidebar.parentNode, layout); assert.ok(sidebar.children.some(node => node.className === "library-sidebar-section"));
  const pagination = f.nodes().find(node => node.className === "library-pagination"); assert.ok(pagination.children.length <= 10); assert.ok(pagination.children.some(node => node.attributes["aria-current"] === "page"));
  f.findButton("刷新本地列表").emit("click"); await flush(); assert.ok(f.nodes().includes(card)); assert.equal(f.findButton("刷新本地列表").disabled, true);
  pending.resolve({ ...browse(), total: 1520 }); await flush(); assert.equal(f.findButton("刷新本地列表").disabled, false);
});

test("appearance can recolor the library without theme-specific geometry or remote assets", () => {
  assert.match(CSS, /max-width:\s*1100px/); assert.match(CSS, /grid-template-columns:\s*minmax\(0, 1fr\) 208px/);
  assert.doesNotMatch(CSS, /data-theme|data-appearance|theme-|--selected-bg|--panel-bg|https?:|url\(/);
  for (const variable of ["--text", "--muted", "--accent", "--panel", "--border", "--field-input-bg", "--code-bg"]) assert.ok(CSS.includes("var(" + variable));
  assert.doesNotMatch(SOURCE, /fetch\([^)]*(?:bangumi|lain\.bgm)/);
});

test("initial browse completion preserves an unsubmitted search draft and submits that exact text", async () => {
  const initial = deferred(); let requests = 0;
  const f = domFixture(route => route === "browse" && ++requests === 1 ? initial.promise : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush();
  const search = f.nodes().find(node => node.tagName === "input" && node.type === "search"), form = f.nodes().find(node => node.className === "library-filters");
  search.value = "Little Busters"; search.emit("input"); search.focus();
  initial.resolve({ ...browse(), total: 1520 }); await flush();
  assert.equal(search.value, "Little Busters"); assert.equal(f.document.activeElement, search);
  form.emit("submit"); await flush();
  assert.equal(f.calls.filter(call => call.route === "browse").at(-1).body.query, "Little Busters"); assert.equal(search.value, "Little Busters");
});

test("search submitted during initial loading wins over the late unfiltered page", async () => {
  const initial = deferred(), searched = deferred();
  const f = domFixture((route, body) => route === "browse" ? body.query ? searched.promise : initial.promise : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush();
  const search = f.nodes().find(node => node.tagName === "input" && node.type === "search"); search.value = "Little Busters"; search.emit("input");
  f.nodes().find(node => node.className === "library-filters").emit("submit"); await flush();
  searched.resolve({ ...browse(), items: [{ ...item, name: "Little Busters" }], total: 3 }); await flush();
  initial.resolve({ ...browse(), total: 1520 }); await flush();
  assert.equal(search.value, "Little Busters"); assert.equal(f.nodes().find(node => node.className === "library-total").textContent, "3 部作品");
});

test("refresh, detail navigation and reactivation preserve a newer unsubmitted search draft", async () => {
  const refreshing = deferred(), detailPending = deferred(); let requests = 0;
  const f = domFixture(route => route === "browse" && ++requests === 2 ? refreshing.promise : route === "detail" ? detailPending.promise : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush();
  const search = f.nodes().find(node => node.tagName === "input" && node.type === "search");
  f.findButton("刷新本地列表").emit("click"); await flush(); search.value = "Draft while refreshing"; search.emit("input");
  refreshing.resolve(browse()); await flush(); assert.equal(search.value, "Draft while refreshing");
  f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  detailPending.resolve(detail()); await flush(); f.findButton("← 返回作品库").emit("click"); await flush();
  assert.equal(search.value, "Draft while refreshing");
  f.feature.deactivate(); f.feature.activate(f.context); await flush(); assert.equal(search.value, "Draft while refreshing");
  assert.ok(f.calls.filter(call => call.route === "browse").every(call => call.body.query === ""));
  f.nodes().find(node => node.className === "library-filters").emit("submit"); await flush();
  assert.equal(f.calls.filter(call => call.route === "browse").at(-1).body.query, "Draft while refreshing");
  f.findButton("清除筛选").emit("click"); await flush(); assert.equal(search.value, "");
});

test("refresh without a Bangumi binding prepares named search without network or a stale preview", async () => {
  const f = fixture(route => route === "provider/preview" ? { ref, snapshot_id: "old", preview_token: "old", diff: [{ field: "name", selectable: true, changed: true }] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.previewProvider(123); f.controller.selectProviderField("name", true);
  const before = f.calls.length; await f.controller.refreshProvider();
  assert.equal(f.controller.state.detailTab, "sources"); assert.equal(f.controller.state.provider.query, "Test");
  assert.match(f.controller.state.provider.guidance, /尚未关联/); assert.equal(f.calls.length, before);
  assert.equal(f.controller.state.provider.preview, null); assert.deepEqual(plain(f.controller.state.provider.selected), []);
  await assert.rejects(f.controller.applyProvider(), /预览差异/);
});

test("single binding refresh uses shared preview and its exact range, without any selected fields or writes", async () => {
  const binding = { provider: "bangumi", external_id: "123", scope: "episodes:13-24", episode_range: { start: 13, end: 24 } };
  const f = fixture((route, body) => route === "detail" ? { ...detail(), sources: [binding] } : route === "provider/preview" ? {
    ref, snapshot_id: "new", preview_token: "new", episode_range: body.episode_range, diff: [{ field: "name", changed: true, selectable: true }] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); f.controller.setEpisodeRange("1", "12"); await f.controller.refreshProvider();
  assert.deepEqual(f.calls.find(call => call.route === "provider/preview").body, { ref, subject_id: "123", episode_range: { start: 13, end: 24 } });
  assert.equal(f.controller.state.provider.rangeStart, "13"); assert.equal(f.controller.state.provider.rangeEnd, "24");
  assert.deepEqual(plain(f.controller.state.provider.selected), []); assert.equal(f.calls.some(call => /apply$/.test(call.route)), false);
});

test("multiple bindings including the same subject with different scopes always require explicit selection", async () => {
  for (const secondId of ["123", "456"]) {
    const sources = [{ provider: "bangumi", external_id: "123", scope: "episodes:1-12", episode_range: { start: 1, end: 12 } },
      { provider: "bangumi", external_id: secondId, scope: "episodes:13-24", episode_range: { start: 13, end: 24 } }];
    const f = fixture(route => route === "detail" ? { ...detail(), sources } : route === "provider/preview" ? { ref, snapshot_id: "new", preview_token: "new", diff: [] } : undefined);
    await f.controller.activate(); await f.controller.openDetail(item); await f.controller.refreshProvider();
    assert.match(f.controller.state.provider.guidance, /多个来源或章节范围/); assert.equal(f.calls.some(call => call.route.startsWith("provider/")), false);
    await f.controller.refreshProvider(f.controller.state.detail.sources[1]);
    assert.deepEqual(f.calls.find(call => call.route === "provider/preview").body, { ref, subject_id: secondId, episode_range: { start: 13, end: 24 } });
  }
});

test("unscoped refresh clears old range and invalid scoped associations never broaden to all chapters", async () => {
  const unscoped = { provider: "bangumi", external_id: "123", scope: "", episode_range: null };
  const f = fixture(route => route === "detail" ? { ...detail(), sources: [unscoped] } : route === "provider/preview" ? { ref, snapshot_id: "new", preview_token: "new", diff: [] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); f.controller.setEpisodeRange("13", "24"); await f.controller.refreshProvider();
  assert.equal(f.controller.state.provider.rangeStart, ""); assert.equal(f.controller.state.provider.rangeEnd, "");
  assert.deepEqual(f.calls.find(call => call.route === "provider/preview").body, { ref, subject_id: "123" });
  for (const association of [
    { scope: "episodes:1-12" }, { scope: "episodes:1-12", episode_range: {} },
    { scope: "episodes:1-12", episode_range: { start: 12, end: 1 } },
    { scope: "episodes:1-12", episode_range: { start: 13, end: 24 } },
    { scope: "custom", episode_range: { start: 1, end: 12 } }
  ]) {
    const invalid = fixture(route => route === "detail" ? { ...detail(), sources: [{ provider: "bangumi", external_id: "123", ...association }] } : undefined);
    await invalid.controller.activate(); await invalid.controller.openDetail(item); await invalid.controller.refreshProvider();
    assert.ok(invalid.controller.state.provider.error); assert.equal(invalid.calls.some(call => call.route.startsWith("provider/")), false);
  }
});

test("refresh is single-flight and a failed refresh cannot apply the older signed preview", async () => {
  const pending = deferred(); let previewCount = 0;
  const f = fixture(route => route === "detail" ? { ...detail(), sources: [{ provider: "bangumi", external_id: "123" }] } : route === "provider/preview" ? ++previewCount === 1 ? {
    ref, snapshot_id: "old", preview_token: "old", diff: [{ field: "name", changed: true, selectable: true }] } : pending.promise : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.refreshProvider();
  const old = f.controller.state.provider.preview; f.controller.selectProviderField("name", true);
  const refreshing = f.controller.refreshProvider(); await f.controller.refreshProvider(); await f.controller.previewProvider(456);
  assert.equal(previewCount, 2); assert.equal(f.controller.state.provider.preview, null);
  await assert.rejects(f.controller.applyProvider(old), /预览已变化/);
  pending.reject(Error("Bangumi 请求失败：连接超时")); await refreshing;
  assert.match(f.controller.state.provider.error, /连接超时/); await assert.rejects(f.controller.applyProvider(old), /预览差异/);
  assert.equal(f.calls.some(call => /apply$/.test(call.route)), false);
});

test("closed, inactive and disposed detail ignore late refresh and cannot apply or launch more provider work", async () => {
  for (const action of ["closeDetail", "deactivate", "dispose"]) {
    const pending = deferred();
    const f = fixture(route => route === "detail" ? { ...detail(), sources: [{ provider: "bangumi", external_id: "123" }] } : route === "provider/preview" ? pending.promise : undefined);
    await f.controller.activate(); await f.controller.openDetail(item); const refreshing = f.controller.refreshProvider(); f.controller[action]();
    pending.resolve({ ref, snapshot_id: "late", preview_token: "late", diff: [] }); await refreshing;
    assert.equal(f.controller.state.provider.preview, null); await assert.rejects(f.controller.applyProvider(), /页面或预览已变化/);
    const calls = f.calls.length; await f.controller.refreshProvider(); await f.controller.previewProvider(123); await f.controller.previewCover(123); await f.controller.searchProvider("Test"); assert.equal(f.calls.length, calls);
  }
});

test("busy saves suppress refresh and stale source selections or old preview controls cannot apply", async () => {
  const pending = deferred(); let version = 0;
  const f = fixture(route => route === "detail" ? { ...detail(), sources: [{ provider: "bangumi", external_id: "123" }] } : route === "edits/preview" ? pending.promise : route === "provider/preview" ? {
    ref, snapshot_id: "snapshot" + ++version, preview_token: "token" + version, diff: [] } : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.refreshProvider(); const old = f.controller.state.provider.preview;
  await f.controller.refreshProvider(); await assert.rejects(f.controller.applyProvider(old), /旧的预览/);
  await f.controller.refreshProvider({ provider: "bangumi", external_id: "999" }); assert.match(f.controller.state.provider.error, /来源关联已变化/);
  const saving = f.controller.saveRecord(record, ref), calls = f.calls.length; await f.controller.refreshProvider(); assert.equal(f.calls.length, calls);
  pending.resolve({ issues: [{ message: "blocked", blocking: true }] }); await assert.rejects(saving, /blocked/);
  assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
});

test("detail refresh button opens and focuses a prefilled search for an unbound work", async () => {
  const f = domFixture(); f.feature.init(f.context); f.feature.activate(f.context); await flush();
  assert.ok(f.findButton("刷新本地列表")); f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  f.findButton("刷新 Bangumi 数据").emit("click"); await flush();
  const search = f.document.getElementById("library-provider-query"); assert.equal(search.value, "Test"); assert.equal(f.document.activeElement, search);
  assert.equal(f.calls.some(call => call.route.startsWith("provider/")), false);
});

test("detail refresh shows each same-subject binding and refreshes only the chosen episode scope", async () => {
  const sources = [{ provider: "bangumi", external_id: "123", scope: "episodes:1-12", episode_range: { start: 1, end: 12 } },
    { provider: "bangumi", external_id: "123", scope: "episodes:13-24", episode_range: { start: 13, end: 24 } }];
  const pending = deferred(), f = domFixture(route => route === "detail" ? { ...detail(), sources } : route === "provider/preview" ? pending.promise : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush(); f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  f.findButton("刷新 Bangumi 数据").emit("click"); await flush();
  assert.equal(f.document.activeElement.id, "library-source-choices"); assert.equal(f.calls.some(call => call.route.startsWith("provider/")), false);
  const buttons = f.nodes().filter(node => node.tagName === "button" && node.textContent === "刷新此来源数据"); assert.equal(buttons.length, 2);
  assert.equal(f.nodes().filter(node => node.tagName === "button" && node.textContent === "更新此来源封面").length, 2);
  buttons[1].emit("click"); await flush(); assert.equal(f.findButton("刷新 Bangumi 数据").disabled, true);
  assert.deepEqual(f.calls.find(call => call.route === "provider/preview").body.episode_range, { start: 13, end: 24 });
  pending.resolve({ ref, snapshot_id: "chosen", preview_token: "chosen", diff: [{ field: "name", changed: true, selectable: true }], episode_range: { start: 13, end: 24 } }); await flush();
  assert.equal(f.nodes().find(node => node.type === "checkbox").checked, false); assert.equal(f.findButton("刷新 Bangumi 数据").disabled, false);
  assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
});

const aliasPreview = (options = {}) => ({ ref, snapshot_id: "alias-snapshot", preview_token: "alias-token", automatic_aliases: ["Original title"], diff: [
  { field: "name", label: "作品名", before: "Test", after: "Original title", changed: true, selectable: true },
  { field: "metadata.aliases", label: "别名", before: ["Existing"], after: ["Existing", "Original title", "中文名"], after_when_name_selected: ["Existing", "中文名"], changed: true, selectable: true }
], ...options });

test("source-only confirmation reports automatic aliases actually added by the server", async () => {
  for (const added of [["Original title"], []]) {
    const f = fixture(route => route === "provider/preview" ? aliasPreview() : route === "provider/apply" ? { records: [{ ref, record }], automatic_aliases_added: added } : undefined);
    await f.controller.activate(); await f.controller.openDetail(item); await f.controller.previewProvider(123);
    assert.deepEqual(plain(f.controller.state.provider.selected), []); assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
    await f.controller.applyProvider();
    assert.deepEqual(f.calls.find(call => call.route === "provider/apply").body.selected_fields, []);
    if (added.length) { assert.match(f.notices.at(-1).value, /自动追加别名：Original title/); assert.doesNotMatch(f.notices.at(-1).value, /未修改|未覆盖/); }
    else { assert.match(f.notices.at(-1).value, /未修改作品字段/); assert.doesNotMatch(f.notices.at(-1).value, /追加别名/); }
  }
});

test("name selection prunes an alias selection whose merged variant is empty or unchanged", async () => {
  for (const variant of [[], ["Existing"]]) {
    const preview = aliasPreview(); preview.diff[1].after_when_name_selected = variant;
    const f = fixture(route => route === "provider/preview" ? preview : route === "provider/apply" ? { records: [{ ref, record }], automatic_aliases_added: [] } : undefined);
    await f.controller.activate(); await f.controller.openDetail(item); await f.controller.previewProvider(123);
    f.controller.selectProviderField("metadata.aliases", true); assert.deepEqual(plain(f.controller.state.provider.selected), ["metadata.aliases"]);
    f.controller.selectProviderField("name", true); assert.deepEqual(plain(f.controller.state.provider.selected), ["name"]);
    f.controller.selectProviderField("metadata.aliases", true); assert.deepEqual(plain(f.controller.state.provider.selected), ["name"]);
    await f.controller.applyProvider(); assert.deepEqual(f.calls.find(call => call.route === "provider/apply").body.selected_fields, ["name"]);
  }
});

test("name variant updates alias eligibility and deselecting name enables the merged full list again", async () => {
  const preview = aliasPreview(); preview.diff[1].after_when_name_selected = ["Existing"];
  const f = fixture(route => route === "provider/preview" ? preview : undefined);
  await f.controller.activate(); await f.controller.openDetail(item); await f.controller.previewProvider(123);
  f.controller.selectProviderField("name", true); f.controller.selectProviderField("metadata.aliases", true); assert.deepEqual(plain(f.controller.state.provider.selected), ["name"]);
  f.controller.selectProviderField("name", false); f.controller.selectProviderField("metadata.aliases", true); assert.deepEqual(plain(f.controller.state.provider.selected), ["metadata.aliases"]);
});

async function openAliasPreview(preview, applyResult) {
  const f = domFixture(route => route === "detail" ? { ...detail(), sources: [{ provider: "bangumi", external_id: "123" }] } :
    route === "provider/preview" ? preview : route === "provider/apply" ? applyResult || { records: [{ ref, record }], automatic_aliases_added: [] } : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush(); f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  f.findButton("刷新 Bangumi 数据").emit("click"); await flush(); return f;
}

test("automatic alias preview is explicit without selecting fields and uses the correct confirmation label", async () => {
  const f = await openAliasPreview(aliasPreview(), { records: [{ ref, record }], automatic_aliases_added: ["Original title"] });
  const notice = f.nodes().find(node => node.className.includes("library-automatic-aliases"));
  assert.equal(notice.hidden, false); assert.match(notice.textContent, /Original title/); assert.match(notice.textContent, /无需勾选.*已有别名会保留并去重/);
  assert.ok(f.nodes().filter(node => node.type === "checkbox").every(node => node.checked === false));
  assert.ok(f.findButton("确认来源并追加别名")); assert.equal(f.findButton("仅确认来源关联"), undefined);
  assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
  assert.ok(f.nodes().some(node => node.textContent.includes("别名会与已有内容合并去重")));
  f.findButton("确认来源并追加别名").emit("click"); await flush();
  assert.deepEqual(f.calls.find(call => call.route === "provider/apply").body.selected_fields, []);
  assert.match(f.nodes().find(node => node.className === "library-notice").textContent, /自动追加别名：Original title/);
});

test("name checkbox switches alias merged preview and hides automatic addition then restores both", async () => {
  const f = await openAliasPreview(aliasPreview()), notice = f.nodes().find(node => node.className.includes("library-automatic-aliases"));
  const row = f.nodes().find(node => node.className === "library-diff" && node.children.some(child => child.textContent === "作品名"));
  const name = row.children.find(node => node.type === "checkbox"), aliasAfter = f.nodes().find(node => node.className.includes("library-aliases-after"));
  assert.deepEqual(JSON.parse(aliasAfter.children[0].textContent), ["Existing", "Original title", "中文名"]);
  name.checked = true; name.emit("change"); assert.equal(notice.hidden, true);
  assert.deepEqual(JSON.parse(aliasAfter.children[0].textContent), ["Existing", "中文名"]); assert.ok(f.findButton("确认写入所选字段"));
  name.checked = false; name.emit("change"); assert.equal(notice.hidden, false);
  assert.deepEqual(JSON.parse(aliasAfter.children[0].textContent), ["Existing", "Original title", "中文名"]); assert.ok(f.findButton("确认来源并追加别名"));
  assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
});

test("selecting aliases keeps automatic addition disclosed while changing the confirmation to selected fields", async () => {
  const f = await openAliasPreview(aliasPreview()), notice = f.nodes().find(node => node.className.includes("library-automatic-aliases"));
  const row = f.nodes().find(node => node.className === "library-diff" && node.children.some(child => child.textContent === "别名"));
  const aliases = row.children.find(node => node.type === "checkbox"); aliases.checked = true; aliases.emit("change");
  assert.equal(notice.hidden, false); assert.match(notice.textContent, /保留并去重/); assert.ok(f.findButton("确认写入所选字段"));
  assert.equal(f.calls.some(call => call.route === "provider/apply"), false);
});

test("alias checkbox is unchecked and disabled when the selected-name variant has no merge changes", async () => {
  const preview = aliasPreview(); preview.diff[1].after_when_name_selected = ["Existing"];
  const f = await openAliasPreview(preview), rows = f.nodes().filter(node => node.className === "library-diff");
  const name = rows[0].children.find(node => node.type === "checkbox"), aliases = rows[1].children.find(node => node.type === "checkbox");
  aliases.checked = true; aliases.emit("change"); assert.equal(aliases.checked, true);
  name.checked = true; name.emit("change"); assert.equal(aliases.checked, false); assert.equal(aliases.disabled, true);
  name.checked = false; name.emit("change"); assert.equal(aliases.checked, false); assert.equal(aliases.disabled, false);
});

test("no automatic alias retains source-only label and cover-only never offers automatic aliases", async () => {
  const noAlias = await openAliasPreview(aliasPreview({ automatic_aliases: [] }));
  assert.ok(noAlias.findButton("仅确认来源关联")); assert.equal(noAlias.nodes().find(node => node.className.includes("library-automatic-aliases")).hidden, true);
  const f = domFixture(route => route === "detail" ? { ...detail(), sources: [{ provider: "bangumi", external_id: "123" }] } : route === "provider/cover-preview" ? {
    ref, snapshot_id: "cover", preview_token: "cover", cover_only: true, automatic_aliases: ["Should not appear"], diff: [{ field: "metadata.cover", changed: true, selectable: true }] } : undefined);
  f.feature.init(f.context); f.feature.activate(f.context); await flush(); f.nodes().find(node => node.className.includes("library-card-link")).emit("click"); await flush();
  f.findButton("更新封面").emit("click"); await flush();
  assert.equal(f.nodes().find(node => node.className.includes("library-automatic-aliases")).hidden, true);
  assert.equal(f.findButton("确认来源并追加别名"), undefined); assert.ok(f.findButton("确认更新封面"));
});
