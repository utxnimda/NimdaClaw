"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const code = fs.readFileSync(path.join(__dirname, "../frontend/index.js"), "utf8");
const enumCode = fs.readFileSync(path.join(__dirname, "../../../framework/frontend/src/common/enum-fields.js"), "utf8");
const workFormCode = fs.readFileSync(path.join(__dirname, "../../collection-detail/frontend/work-record-form.js"), "utf8");
const directoryBrowserCode = fs.readFileSync(path.join(__dirname, "../../../framework/frontend/src/common/directory-browser.js"), "utf8");
const sectionTabsCode = fs.readFileSync(path.join(__dirname, "../../../framework/frontend/src/common/section-tabs.js"), "utf8");
const plain = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

test("child folder lists use page scrolling without desktop or narrow-screen height caps", () => {
  const css = fs.readFileSync(path.join(__dirname, "../frontend/style.css"), "utf8");
  const rules = [...css.matchAll(/\.organizer-children\s*\{([^}]+)\}/g)].map(match => match[1]);
  assert.ok(rules.length > 0);
  assert.match(rules.join(";"), /max-height\s*:\s*none\s*;/);
  assert.match(rules.join(";"), /overflow\s*:\s*visible\s*;/);
  for (const rule of rules) {
    assert.doesNotMatch(rule, /(?:^|;)\s*(?:max-)?height\s*:\s*(?:[\d.]|calc\(|min\(|max\(|clamp\()/);
    assert.doesNotMatch(rule, /overflow(?:-[xy])?\s*:\s*(?:auto|scroll|hidden)\b/);
  }
});

function fixture(handler, settings = {}) {
  const registrations = [], calls = [], notices = [], window = { NimdaCommon: { ensureFeatureRegistry: () => ({ register: value => registrations.push(value) }) } };
  vm.runInNewContext(workFormCode, { window });
  vm.runInNewContext(code, { window });
  let plan = 0;
  const controller = window.NimdaDirectoryOrganizer.createController({
    autoPreviewLimit: settings.autoPreviewLimit || 0,
    request: async (url, options) => {
      const body = JSON.parse(options.body), route = url.split("/").pop(); calls.push({ route, body });
      if (handler) { const result = await handler(route, body); if (result !== undefined) return result; }
      if (route === "scan") return { ok: true, root: body.root, children: ["A", "B"].map(name => ({ id: name, name, path: body.root + "/" + name })), strategies: [{ id: "generic", label: "通用" }] };
      if (route === "preview") return { ok: true, plan: { id: "plan" + ++plan, root: body.root, child: body.child, can_execute: true, status: "needs_work", draft: body.draft || { work_root: body.root, release_name: body.child, records: [], file_targets: {} }, files: [], catalog_candidates: [] } };
      if (route === "execute") return { ok: true, results: body.plan_ids.map(plan_id => ({ plan_id, status: "succeeded" })) };
      throw Error("Unexpected route: " + route);
    }, setStatus: (value, error) => notices.push({ value, error })
  });
  return { controller, calls, notices, registrations, library: window.NimdaDirectoryOrganizer };
}
async function scanned(f) { f.controller.setRoot("U:/Test"); await f.controller.scan(); return f.controller; }

test("registers an isolated organizer feature and scan is read only", async () => {
  const f = fixture(), c = await scanned(f);
  assert.equal(f.registrations[0].id, "directory-organizer"); assert.equal(f.registrations[0].order, 30);
  assert.deepEqual(f.calls.map(row => row.route), ["scan"]);
  assert.equal(c.child("A").selected, false); assert.equal(c.state.generateShortcuts, false);
});
test("each child previews and locks independently; any edit invalidates only that plan", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A"); await c.preview("B");
  assert.equal(c.lock("A"), true); assert.equal(c.lock("B"), true);
  assert.equal(c.edit("A", d => { d.release_name = "bad"; }), false);
  c.unlock("A"); c.setTarget("A", "release_name", "New");
  assert.equal(c.child("A").plan, null); assert.equal(c.child("B").locked, true);
  await c.preview("A"); assert.equal(c.child("A").draft.release_name, "New");
});
test("batch execution requires every selected plan locked and preserves requested order", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A"); await c.preview("B"); c.lock("A");
  await c.execute(["B", "A"]); assert.equal(f.calls.filter(row => row.route === "execute").length, 0);
  c.lock("B"); const expected = [c.child("B").plan.id, c.child("A").plan.id]; await c.execute(["B", "A"]);
  assert.deepEqual(f.calls.find(row => row.route === "execute").body, { plan_ids: expected, confirm: true, generate_shortcuts: false });
  assert.equal(c.child("A").executed, true); assert.equal(c.child("A").plan, null);
  await c.execute(["A"]); assert.equal(f.calls.filter(row => row.route === "execute").length, 1);
});
test("switching roots discards a stale scan response", async () => {
  const waiting = deferred(), f = fixture(route => route === "scan" ? waiting.promise : undefined), c = f.controller;
  c.setRoot("U:/Old"); const scan = c.scan(); c.setRoot("U:/New");
  waiting.resolve({ ok: true, root: "U:/Old", children: [{ name: "Old" }] }); await scan;
  assert.equal(c.state.root, "U:/New"); assert.equal(c.state.children.length, 0);
});
test("edits during preview prevent stale automatic values replacing manual draft", async () => {
  const waiting = deferred(), f = fixture(route => route === "preview" ? waiting.promise : undefined), c = await scanned(f);
  const preview = c.preview("A"); c.setTarget("A", "release_name", "Manual");
  waiting.resolve({ ok: true, plan: { id: "stale", draft: { release_name: "Old" }, can_execute: true } }); await preview;
  assert.equal(c.child("A").draft.release_name, "Manual"); assert.equal(c.child("A").plan, null);
});
test("cancelled native directory chooser preserves entered root", async () => {
  const f = fixture(route => route === "choose-directory" ? { ok: true, cancelled: true } : undefined), c = f.controller;
  c.setRoot("U:/Keep"); await c.choose(); assert.equal(c.state.root, "U:/Keep"); assert.equal(c.state.choosing, false);
  assert.deepEqual(f.calls[0].body, { root: "U:/Keep" });
});
test("new records require explicit confirmation and preserve unknown fields", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A"); c.addRecord("A");
  assert.equal(c.child("A").draft.records[0].confirmed, false);
  const record = plain(c.child("A").draft.records[0].record); record.vendor = { key: "preserve" }; record.attributes.push({ type: "custom", data: { items: [1, 2] } });
  c.updateRecordJson("A", 0, JSON.stringify(record)); await c.preview("A");
  assert.deepEqual(plain(c.child("A").draft.records[0].record), record);
});
test("invalid JSON prevents preview and confirmation while preserving invalid input", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A"); c.addRecord("A");
  c.updateRecordJson("A", 0, "{broken"); const previous = f.calls.length; await c.preview("A");
  assert.equal(f.calls.length, previous); assert.equal(c.lock("A"), false); assert.equal(c.child("A").jsonTexts[0], "{broken");
});
test("multiple DB records can bind one target without overwriting unrelated presses", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A");
  for (let index = 0; index < 2; index++) {
    const record = f.library.template("Season " + index), coll = f.library.attribute(record, "collection-type", {}).data;
    coll.collectioned.push({ press_format: "DVDRip", press_group: "", press_path: "Unrelated" });
    c.addCandidate("A", { ref: { yaml_source_rel: "2005.yaml", index_in_file: index, record_sha256: "keep" }, record });
  }
  c.edit("A", d => d.records.forEach(row => { row.press_keys = ["0:existing"]; }));
  c.setTarget("A", "work_root", "U:/Shared"); c.setTarget("A", "release_name", "Shared_BDRip");
  c.child("A").draft.records.forEach(row => {
    const coll = f.library.attribute(row.record, "collection-type", {}).data;
    assert.equal(coll.path, "U:/Shared"); assert.equal(coll.collectioned[0].press_path, "Shared_BDRip"); assert.equal(coll.collectioned[1].press_path, "Unrelated"); assert.equal(row.ref.record_sha256, "keep");
  });
  c.syncFromRecord("A", 1, "press_path", "Updated", 0); assert.equal(c.child("A").draft.release_name, "Updated");
  assert.equal(f.library.attribute(c.child("A").draft.records[0].record, "collection-type", {}).data.collectioned[0].press_path, "Updated");
});
test("marking complete preserves source location and can be undone manually", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A"); c.addRecord("A"); c.setTarget("A", "release_name", "Renamed");
  c.setCompleted("A", true); assert.equal(c.child("A").draft.release_name, "A"); assert.equal(c.child("A").draft.completed, true);
  c.setCompleted("A", false); assert.equal(c.child("A").draft.completed, false);
});
test("network failures after execution invalidate all plans rather than blindly retry", async () => {
  const f = fixture(route => { if (route === "execute") throw Error("lost connection"); }), c = await scanned(f);
  await c.preview("A"); c.lock("A"); await c.execute(["A"]);
  assert.equal(c.child("A").locked, false); assert.equal(c.child("A").plan, null); assert.match(f.notices.at(-1).value, /不能重复使用旧计划/);
});
test("shortcut generation requires its own preview ID and explicit confirmation", async () => {
  const f = fixture((route, body) => {
    if (route === "execute") return { ok: true, results: [{ plan_id: body.plan_ids[0], status: "succeeded", shortcut_refs: [{ work_key: "work", press_key: "0:press" }], shortcut_preview: { plan_id: "links", creatable: 1, conflict_count: 0, items: [{ target_path: "U:/A", shortcut_path: "E:/Links/A.lnk", status: "planned" }] } }] };
    if (route === "shortcuts") return { ok: true, created: 1 };
  }), c = await scanned(f); await c.preview("A"); c.lock("A"); await c.execute(["A"]);
  assert.equal(f.calls.filter(row => row.route === "shortcuts").length, 0); await c.shortcuts("A", true);
  assert.deepEqual(f.calls.at(-1).body, { refs: [{ work_key: "work", press_key: "0:press" }], preview: false, confirm: true, plan_id: "links" });
  assert.equal(c.child("A").result.shortcut_preview, null);
});
test("catalog searches ignore late results and preserve associated draft", async () => {
  const first = deferred(), second = deferred(); let count = 0;
  const f = fixture(route => route === "catalog" ? (++count === 1 ? first.promise : second.promise) : undefined), c = await scanned(f);
  await c.preview("A"); c.addRecord("A"); const p1 = c.search("A", "old"), p2 = c.search("A", "new");
  second.resolve({ ok: true, records: [{ name: "new" }], total: 1 }); await p2;
  first.resolve({ ok: true, records: [{ name: "old" }], total: 1 }); await p1;
  assert.equal(c.child("A").candidates[0].name, "new"); assert.equal(c.child("A").draft.records.length, 1);
});
test("rule selection before first preview does not suppress automatic DB matching", async () => {
  const f = fixture(), c = await scanned(f); c.edit("A", (_draft, item) => { item.strategy = "generic"; }); await c.preview("A");
  assert.equal(Object.hasOwn(f.calls.at(-1).body.draft, "records"), false);
});

test("editing JSON press ordering clears bindings so positions cannot silently point to another version", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A"); c.addRecord("A");
  c.edit("A", draft => { const entry = draft.records[0]; entry.press_keys = ["0:selected"]; const coll = f.library.attribute(entry.record, "collection-type", {}).data; coll.collectioned.push({ press_format: "Other", press_path: "Other" }); });
  const record = plain(c.child("A").draft.records[0].record); record.attributes.find(row => row.type === "collection-type").data.collectioned.reverse();
  c.updateRecordJson("A", 0, JSON.stringify(record)); assert.deepEqual(plain(c.child("A").draft.records[0].press_keys), []); assert.equal(c.child("A").draft.records[0].manual_press_binding, true);
});

test("empty existing records can add visual press drafts and continuations while preserving unrelated data", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A");
  const record = f.library.template("Known"), collection = f.library.attribute(record, "collection-type", {}).data; collection.collectioned = []; collection.extra = "keep";
  c.addCandidate("A", { ref: { yaml_source_rel: "2005.yaml", index_in_file: 1 }, record });
  c.addPress("A", 0); let entry = c.child("A").draft.records[0]; let coll = f.library.attribute(entry.record, "collection-type", {}).data;
  assert.equal(coll.collectioned.length, 1); assert.equal(coll.collectioned[0].press_path, "A"); assert.equal(coll.path, "U:/Test"); assert.equal(coll.extra, "keep");
  assert.deepEqual(plain(entry.press_keys), ["0:manual"]); c.addContinuation("A", 0); c.addPress("A", 0, 0);
  assert.equal(coll.continuations[0].collectioned.length, 1); assert.deepEqual(plain(entry.press_keys), ["1:manual"]);
  c.removePress("A", 0, 0); assert.equal(coll.collectioned.length, 0); assert.equal(coll.continuations[0].collectioned.length, 1); assert.deepEqual(plain(entry.press_keys), []);
});

test("shortcut conflicts or empty plans cannot be confirmed even through the controller", async () => {
  const f = fixture((route, body) => route === "execute" ? { ok: true, results: [{ plan_id: body.plan_ids[0], status: "succeeded", shortcut_refs: [{ work_key: "a", press_key: "0:b" }], shortcut_preview: { plan_id: "conflict", creatable: 1, conflict_count: 1, conflicts: [{ message: "actual target differs" }] } }] } : undefined);
  const c = await scanned(f); await c.preview("A"); c.lock("A"); await c.execute(["A"]); await c.shortcuts("A", true);
  assert.equal(f.calls.filter(row => row.route === "shortcuts").length, 0); assert.match(f.notices.at(-1).value, /存在冲突/);
  c.child("A").result.shortcut_preview = { plan_id: "empty", creatable: 0 }; await c.shortcuts("A", true);
  assert.equal(f.calls.filter(row => row.route === "shortcuts").length, 0); assert.match(f.notices.at(-1).value, /没有需要新增/);
});

test("scan automatically previews at most 64 children sequentially without locking or writing", async () => {
  const f = fixture((route, body) => route === "scan" ? { ok: true, root: body.root, children: Array.from({ length: 70 }, (_, index) => ({ id: "C" + index, name: "C" + index })) } : undefined, { autoPreviewLimit: 64 });
  const c = await scanned(f), previews = f.calls.filter(row => row.route === "preview");
  assert.equal(previews.length, 64); assert.deepEqual(previews.map(row => row.body.child), Array.from({ length: 64 }, (_, index) => "C" + index));
  assert.equal(c.state.autoPreview.completed, 64); assert.equal(c.state.autoPreview.running, false);
  assert.ok(c.state.children.slice(0, 64).every(row => row.plan && !row.locked)); assert.equal(c.child("C64").plan, null);
  assert.ok(f.calls.every(row => ["scan", "preview"].includes(row.route)));
  c.lock("C0"); await c.preview("C64"); assert.equal(c.child("C0").locked, false); assert.equal(c.child("C0").plan, null); assert.ok(c.child("C64").plan);
});

test("cancelling automatic preview stops later children while the current read can finish", async () => {
  const waiting = deferred(), f = fixture(route => route === "preview" ? waiting.promise : undefined, { autoPreviewLimit: 64 }), c = f.controller;
  c.setRoot("U:/Test"); const scanning = c.scan(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(c.state.autoPreview.running, true); c.stopAutoPreview();
  waiting.resolve({ ok: true, plan: { id: "read-only", draft: { records: [] }, can_execute: true } }); await scanning;
  assert.equal(f.calls.filter(row => row.route === "preview").length, 1); assert.equal(c.child("A").locked, false); assert.ok(c.child("A").plan);
  assert.equal(c.state.autoPreview.running, false); assert.equal(c.state.autoPreview.cancelled, true);
});

test("a failed automatic preview does not prevent checking the next independent child", async () => {
  const f = fixture((route, body) => { if (route === "preview" && body.child === "A") throw Error("unreadable A"); }, { autoPreviewLimit: 64 }), c = await scanned(f);
  assert.match(c.child("A").error, /unreadable/); assert.ok(c.child("B").plan); assert.equal(c.state.autoPreview.completed, 2);
});

test("root change or disposal prevents remaining automatic preview requests", async () => {
  for (const action of ["root", "dispose"]) {
    const waiting = deferred(), f = fixture(route => route === "preview" ? waiting.promise : undefined, { autoPreviewLimit: 64 }), c = f.controller;
    c.setRoot("U:/Test"); const scanning = c.scan(); await new Promise(resolve => setImmediate(resolve));
    if (action === "root") c.setRoot("U:/Other"); else c.dispose();
    waiting.resolve({ ok: true, plan: { id: "stale", draft: {}, can_execute: true } }); await scanning;
    assert.equal(f.calls.filter(row => row.route === "preview").length, 1);
    if (action === "root") assert.equal(c.state.children.length, 0);
  }
});

test("leaving during the initial scan prevents automatic preview from starting later", async () => {
  const waiting = deferred(), f = fixture(route => route === "scan" ? waiting.promise : undefined, { autoPreviewLimit: 64 }), c = f.controller;
  c.setRoot("U:/Test"); const scanning = c.scan(); c.stopAutoPreview();
  waiting.resolve({ ok: true, root: "U:/Test", children: [{ id: "A", name: "A" }] }); await scanning;
  assert.equal(f.calls.filter(row => row.route === "preview").length, 0);
});

test("an unmatched existing single press is not silently rebound when the top target changes", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A");
  const record = f.library.template("Existing"), coll = f.library.attribute(record, "collection-type", {}).data;
  coll.path = "U:/Original"; coll.collectioned[0].press_path = "Original_DVDRip";
  c.addCandidate("A", { ref: { yaml_source_rel: "2005.yaml", index_in_file: 2 }, record });
  c.setTarget("A", "work_root", "U:/New"); c.setTarget("A", "release_name", "New_BDRip");
  const entry = c.child("A").draft.records[0], actual = f.library.attribute(entry.record, "collection-type", {}).data;
  assert.equal(actual.path, "U:/Original"); assert.equal(actual.collectioned[0].press_path, "Original_DVDRip");
  c.edit("A", draft => { draft.records[0].press_keys = ["0:manual"]; }); c.setTarget("A", "release_name", "Confirmed_BDRip");
  assert.equal(actual.path, "U:/New"); assert.equal(actual.collectioned[0].press_path, "Confirmed_BDRip");
});

test("starting execution stops the remaining automatic previews rather than counting skipped reads as done", async () => {
  const waiting = deferred(), f = fixture((route, body) => {
    if (route === "scan") return { ok: true, root: body.root, children: ["A", "B", "C"].map(name => ({ id: name, name })) };
    if (route === "preview" && body.child === "B") return waiting.promise;
  }, { autoPreviewLimit: 64 }), c = f.controller;
  c.setRoot("U:/Test"); const scanning = c.scan(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(c.state.autoPreview.completed, 1); assert.equal(c.lock("A"), true); await c.execute(["A"]);
  assert.equal(c.state.autoPreview.running, false); assert.equal(c.state.autoPreview.cancelled, true);
  waiting.resolve({ ok: true, plan: { id: "readonly-B", draft: { records: [] }, can_execute: true } }); await scanning;
  assert.deepEqual(f.calls.filter(row => row.route === "preview").map(row => row.body.child), ["A", "B"]);
  assert.equal(c.state.autoPreview.completed, 1); assert.equal(c.child("C").plan, null);
});

test("valid JSON with unsafe record structure preserves the last valid draft and reports field errors", async () => {
  const f = fixture(), c = await scanned(f); await c.preview("A"); c.addRecord("A");
  const valid = plain(c.child("A").draft.records[0].record);
  const badAttributes = [
    [null], [{ type: "date", data: null }], [{ type: "collection-type", data: null }],
    [{ type: "collection-type", data: { collectioned: [null] } }],
    [{ type: "collection-type", data: { collectioned: {} } }],
    [{ type: "collection-type", data: { continuations: [null] } }],
    [{ type: "collection-type", data: { continuations: [{ collectioned: ["bad"] }] } }]
  ];
  for (const attributes of badAttributes) {
    const raw = JSON.stringify({ attributes }); c.updateRecordJson("A", 0, raw);
    assert.ok(c.child("A").jsonErrors[0]); assert.equal(c.child("A").jsonTexts[0], raw);
    assert.deepEqual(plain(c.child("A").draft.records[0].record), valid);
  }
  const extended = plain(valid); extended.attributes.push({ type: "external-extension", data: null }); extended.extra = [null, { arbitrary: true }];
  c.updateRecordJson("A", 0, JSON.stringify(extended)); assert.equal(c.child("A").jsonErrors[0], undefined);
  assert.deepEqual(plain(c.child("A").draft.records[0].record), extended);
});

test("media and DB summaries are separate and prefer authoritative backend status", () => {
  const f = fixture(), record = f.library.template("Missing DB");
  const item = { path: "U:/Work", draft: { records: [{ ref: null, record }] }, plan: {
    media_status: "complete", database_status: "unresolved", counts: { files: 20, moves: 0, unchanged: 20, db_records: 1, db_changes: 0, blocking_issues: 1 },
    files: [], issues: [{ code: "new-record-confirmation", blocking: true }]
  } };
  const summary = f.library.planOverview(item); assert.equal(summary.media, "目录已整理"); assert.equal(summary.database, "DB 待补齐"); assert.equal(summary.moves, 0); assert.equal(summary.unchanged, 20);
  item.plan.media_status = "unresolved"; item.plan.database_status = "matched";
  assert.equal(f.library.planOverview(item).media, "目录待核对"); assert.equal(f.library.planOverview(item).database, "DB 已关联");
  item.plan = { files: [{ changed: false }], db_changes: [], issues: [] };
  assert.equal(f.library.planOverview(item).media, "目录已整理"); assert.equal(f.library.planOverview(item).files, 1);
});

test("dirty drafts never inherit matched DB status or counts from their historical preview", () => {
  const f = fixture(), lastPlan = { database_status: "matched", media_status: "complete",
    counts: { files: 99, moves: 0, db_records: 1, db_changes: 7, blocking_issues: 3 },
    files: [{ source_rel: "episode.mkv", target_rel: "episode.mkv", changed: false }], source_path: "U:/Test/A", issues: [] };
  const item = { plan: null, lastPlan, draft: { completed: true, work_root: "U:/Test", release_name: "A", records: [] }, path: "U:/Test/A" };
  let summary = f.library.planOverview(item);
  assert.equal(summary.database, "DB 待重新预览"); assert.equal(summary.media, "目录待重新预览"); assert.equal(summary.records, 0);
  assert.equal(summary.changes, null); assert.equal(summary.blocking, null); assert.equal(summary.files, 1); assert.equal(summary.outdated, true);
  item.draft.records = [{ ref: null, record: {} }, { ref: null, record: {} }]; summary = f.library.planOverview(item);
  assert.equal(summary.database, "DB 待重新预览"); assert.equal(summary.records, 2);
  lastPlan.database_status = "needs_sync"; assert.equal(f.library.planOverview(item).database, "DB 待重新预览");
});

test("feature DOM switches bounded directory views and opens paginated file details only on demand", async () => {
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.style = {}; this.textContent = ""; this.value = ""; }
    get firstChild() { return this.children[0]; }
    get isConnected() { return this === body || !!(this.parentNode && this.parentNode.isConnected); }
    appendChild(child) { this.children.push(child); child.parentNode = child.parentElement = this; return child; }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; }
    setAttribute(key, value) { this.attributes[key] = value; }
    getAttribute(key) { return this.attributes[key] ?? null; }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    emit(type, extra = {}) { (this.listeners[type] || []).forEach(callback => callback({ target: this, preventDefault() {}, ...extra })); }
    focus() { doc.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; this.emit("close"); }
  }
  const body = new Element("body"), nav = new Element("nav"), shell = new Element("div"); body.appendChild(nav); body.appendChild(shell);
  const descendants = node => [node, ...node.children.flatMap(descendants)];
  const doc = { body, createElement: tag => new Element(tag), getElementById: id => descendants(body).find(node => node.id === id), querySelector: selector => selector === ".app-tabs" ? nav : selector === ".nimda-app-shell" ? shell : null };
  const workForms = [], operationOpens = [];
  let feature; const window = { NimdaCommon: { ensureFeatureRegistry: () => ({ register: value => { feature = value; } }) }, NimdaNewWorkDialog: { open: options => workForms.push(options) }, NimdaOperationCenter: { open: id => operationOpens.push(id) } };
  vm.runInNewContext(enumCode, { window });
  vm.runInNewContext(workFormCode, { window });
  vm.runInNewContext(directoryBrowserCode, { window, document: doc });
  vm.runInNewContext(sectionTabsCode, { window, document: doc });
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../frontend/structure-preview.js"), "utf8"), { window });
  vm.runInNewContext(code, { window, document: doc });
  const unsafe = '<img src=x onerror="bad()">', calls = [], requests = []; let mode = "normal", backgroundPreview, omitShortcutOperation = false;
  feature.init({ fetchJson: async (url, options) => {
    calls.push(url); const payload = JSON.parse(options.body); requests.push({ url, payload });
    if (url.endsWith("scan")) return { res: { ok: true }, data: { ok: true, root: payload.root, children: [{ id: "safe", name: unsafe, path: "U:/" + unsafe }].concat(mode === "background" ? [{ id: "B", name: "B", path: "U:/Test/B" }] : []), strategies: [] } };
    if (url.endsWith("preview") && mode === "background" && payload.child === "B") return backgroundPreview.promise;
    if (url.endsWith("execute")) return { res: { ok: true, headers: { get: name => name.toLowerCase() === "x-nimda-operation-id" ? "safe-execute-operation" : null } }, data: { ok: true, results: [{ plan_id: payload.plan_ids[0], status: "succeeded", shortcut_refs: [{ work_key: "a", press_key: "0:b" }], shortcut_preview: { plan_id: "shortcut-plan", creatable: 1, conflict_count: 0, items: [{ name: "Test Work", shortcut_path: "E:/Links/Test Work/BDRip.lnk", target_path: "U:/Test Work/BDRip", status: "planned" }] } }] } };
    if (url.endsWith("shortcuts")) return { res: { ok: true, headers: { get: name => name.toLowerCase() === "x-nimda-operation-id" && !omitShortcutOperation ? "safe-shortcut-operation" : null } }, data: { ok: true, created: 1 } };
    const files = mode === "many" ? Array.from({ length: 12000 }, (_, index) => ({ source_rel: "source-" + index + ".mkv", target_rel: "_Disc/Season01/" + String(index).padStart(5, "0") + ".mkv", changed: true })) :
      mode === "directory" ? [{ source_rel: "Folder/a.mkv", target_rel: "_Disc/Folder/a.mkv", changed: true }, { source_rel: "Folder/b.mkv", target_rel: "_Disc/Folder/b.mkv", changed: true }, { source_rel: "stays.mkv", target_rel: "stays.mkv", changed: false }] :
      mode === "complete" ? [{ source_rel: "original.mkv", target_rel: "original.mkv", changed: false }] :
      [{ source_rel: "source.mkv", target_rel: "_Disc/_01/A.mkv", changed: true }, { source_rel: "stays.mkv", target_rel: "stays.mkv", changed: false }];
    const record = window.NimdaDirectoryOrganizer.template("Work");
    const before = plain(record); before.attributes.find(row => row.type === "name").data = "Former Work";
    return { res: { ok: true, headers: { get: name => name.toLowerCase() === "x-nimda-operation-id" ? "safe-preview-operation" : null } }, data: { ok: true, plan: { id: "safeplan", can_execute: true, status: mode === "complete" ? "unresolved" : "needs_work", media_status: mode === "complete" ? "complete" : "needs_work", database_status: mode === "complete" ? "unresolved" : "matched",
      source_path: "U:/Test/A", target_path: "U:/Test/A", source_directories: mode === "directory" ? ["Folder"] : [], source_structure_complete: true,
      draft: payload.draft || { completed: mode === "complete", records: [{ ref: mode === "complete" ? null : { yaml_source_rel: "2005.yaml", index_in_file: 0 }, record }], work_root: "U:/Test", release_name: "A", file_targets: {} }, files, db_changes: mode === "normal" ? [{ action: "update", before, after: record }] : [], catalog_candidates: [], issues: [] } } };
  } });
  assert.equal(calls.length, 0); assert.ok(doc.getElementById("directory-organizer-view"));
  const root = descendants(body).find(node => node.attributes["aria-label"] === "待整理目录路径"); root.value = "U:/Test"; root.emit("input");
  descendants(body).find(node => node.tagName === "button" && node.textContent === "预览目录").emit("click");
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(descendants(body).some(node => node.textContent === unsafe)); assert.ok(!descendants(body).some(node => node.tagName === "img"));
  descendants(body).find(node => node.tagName === "button" && node.textContent === "重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  const inHiddenPanel = node => !!node && ((node.attributes.role === "tabpanel" && node.hidden) || inHiddenPanel(node.parentNode));
  const findButton = label => descendants(body).find(node => node.tagName === "button" && node.textContent === label && !inHiddenPanel(node));
  const selectTab = label => { const tab = descendants(body).find(node => node.attributes.role === "tab" && node.attributes["aria-label"] === label); assert.ok(tab, "detail tab exists: " + label); tab.emit("click"); };
  const directoryControl = key => descendants(body).find(node => node.attributes["data-directory-control"] === key);
  const structureSide = () => descendants(body).find(node => node.attributes["data-structure-side"]).attributes["data-structure-side"];
  const dialog = () => descendants(body).find(node => node.tagName === "dialog");
  assert.ok(descendants(body).some(node => node.tagName === "h3" && node.textContent === "目录结构预览"));
  assert.equal(descendants(body).filter(node => node.attributes["data-structure-side"]).length, 1);
  assert.equal(structureSide(), "after");
  findButton("整理前").emit("click"); assert.equal(structureSide(), "before");
  assert.ok(descendants(body).some(node => node.attributes["data-directory-entry"] === "U:/Test/A/stays.mkv"));
  assert.ok(!descendants(body).some(node => /\.mkv$/.test(node.attributes["data-directory-path"] || "")), "files belong in directory contents, never the navigation tree");
  findButton("整理后").emit("click"); assert.equal(structureSide(), "after");
  directoryControl("enter:u:/test/a/_disc").emit("click");
  assert.ok(descendants(body).some(node => node.attributes["data-directory-entry"] === "U:/Test/A/_Disc/_01"));
  findButton("整理前").emit("click"); findButton("整理后").emit("click");
  assert.ok(descendants(body).some(node => node.attributes["data-directory-entry"] === "U:/Test/A/_Disc/_01"), "each side retains its navigation state");
  directoryControl("up").emit("click");
  assert.ok(!descendants(body).some(node => node.attributes["data-move-kind"]));
  assert.ok(!descendants(body).some(node => node.attributes["data-file-source"]));
  assert.equal(dialog(), undefined);
  assert.ok(!descendants(body).some(node => node.tagName === "textarea"));
  selectTab("关联 DB");
  assert.ok(findButton("新增 DB 记录"), "new work remains available in the DB tab without expanding advanced DB editing");
  const beforeNewFormCalls = calls.length; findButton("新增 DB 记录").emit("click");
  assert.equal(workForms.at(-1).title, "新增 DB 记录"); assert.equal(workForms.at(-1).submitLabel, "添加到待保存记录"); assert.equal(calls.length, beforeNewFormCalls);
  assert.ok(!descendants(body).some(node => node.tagName === "textarea"), "opening and cancelling the form creates no inline draft row");
  assert.ok(descendants(body).some(node => node.tagName === "h3" && node.textContent === "DB 实际变更"));
  assert.ok(descendants(body).some(node => node.tagName === "code" && node.textContent === "Former Work"));
  assert.ok(!descendants(body).some(node => node.tagName === "input" && node.parentNode.children[0].textContent === "作品名"));
  selectTab("目录预览");
  const opener = findButton("查看逐文件移动详情…"); opener.emit("click");
  assert.equal(dialog().open, true); assert.equal(doc.activeElement.textContent, "关闭详情");
  assert.ok(descendants(dialog()).some(node => node.attributes["data-file-source"] === "source.mkv"));
  assert.ok(!descendants(dialog()).some(node => node.attributes["data-file-source"] === "stays.mkv"));
  assert.ok(descendants(dialog()).some(node => node.tagName === "code" && node.textContent === "_Disc/_01/A.mkv"));
  findButton("全部文件").emit("click");
  assert.ok(descendants(body).some(node => node.attributes["data-file-source"] === "stays.mkv"));
  dialog().emit("cancel"); assert.equal(dialog(), undefined); assert.equal(doc.activeElement, opener);
  opener.emit("click"); dialog().emit("keydown", { key: "Escape" }); assert.equal(dialog(), undefined); assert.equal(doc.activeElement, opener);
  opener.emit("click"); findButton("重新预览此目录").emit("click"); assert.equal(dialog(), undefined); await new Promise(resolve => setImmediate(resolve));
  selectTab("关联 DB"); (findButton("选择压制记录") || findButton("关联设置")).emit("click");
  assert.ok(descendants(body).some(node => node.tagName === "textarea"));
  assert.ok(!descendants(body).some(node => node.attributes["data-enum-field"] === "country"));
  findButton("编辑作品信息").emit("click");
  const withoutPress = plain(workForms.at(-1).initialData); withoutPress.collectioned_ordered = [];
  workForms.at(-1).onSubmit(withoutPress);
  assert.ok(descendants(body).some(node => node.textContent === "DB 待重新预览"));
  assert.ok(!descendants(body).some(node => node.tagName === "h3" && node.textContent === "DB 实际变更"));
  assert.ok(!descendants(body).some(node => node.tagName === "code" && node.textContent === "Former Work"));
  findButton("编辑作品信息").emit("click");
  const withPress = plain(workForms.at(-1).initialData); withPress.collectioned_ordered = [{ press_format: "BDRip", press_group: "", press_path: "A", segment: "main" }];
  workForms.at(-1).onSubmit(withPress);
  descendants(body).find(node => node.tagName === "button" && node.textContent === "重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  findButton("编辑作品信息").emit("click"); const editedName = plain(workForms.at(-1).initialData); editedName.name = "A changed title"; workForms.at(-1).onSubmit(editedName);
  assert.ok(descendants(body).some(node => node.tagName === "strong" && node.textContent === "DB 待重新预览"));
  assert.equal(descendants(body).find(node => node.tagName === "h3" && node.textContent === "DB 实际变更"), undefined);
  assert.ok(descendants(body).some(node => node.textContent === "草稿已修改 · 待重新预览"));
  assert.equal(descendants(body).find(node => node.tagName === "button" && node.textContent === "确认并锁定此预览").disabled, true);
  descendants(body).find(node => node.tagName === "button" && node.textContent === "重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  descendants(body).find(node => node.tagName === "button" && node.textContent === "确认并锁定此预览").emit("click");
  selectTab("目录预览"); findButton("查看逐文件移动详情…").emit("click"); assert.equal(findButton("编辑文件去向").disabled, true);
  const execute = descendants(body).find(node => node.tagName === "button" && node.textContent === "执行此目录"); assert.equal(execute.disabled, false); execute.emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(dialog(), undefined);
  findButton("查看处理详情").emit("click"); assert.equal(operationOpens.at(-1), "safe-execute-operation");
  findButton("查看快捷方式").emit("click");
  const shortcutPath = descendants(body).find(node => node.tagName === "code" && node.textContent === "E:/Links/Test Work/BDRip.lnk");
  const shortcutTarget = descendants(body).find(node => node.tagName === "code" && node.textContent === "U:/Test Work/BDRip");
  assert.ok(shortcutPath && shortcutTarget); assert.equal(shortcutPath.parentNode.tagName, "article");
  const confirm = descendants(body).find(node => node.tagName === "button" && node.textContent === "确认生成本目录快捷方式"); assert.equal(confirm.disabled, false); confirm.emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.ok(calls.at(-1).endsWith("shortcuts"));
  findButton("查看快捷方式处理详情").emit("click"); assert.equal(operationOpens.at(-1), "safe-shortcut-operation");
  findButton("查看处理详情").emit("click"); assert.equal(operationOpens.at(-1), "safe-execute-operation", "the persistent result action still opens execution details");
  const opensBeforeMissing = operationOpens.length;
  window.NimdaOperationCenter.get = () => undefined;
  findButton("查看处理详情").emit("click"); assert.equal(operationOpens.length, opensBeforeMissing, "an expired exact operation must not open unrelated details");
  delete window.NimdaOperationCenter.get;
  const fallbackRoutes = []; window.NimdaOperationCenter.latest = route => { fallbackRoutes.push(route); return { id: "safe-shortcut-fallback" }; };
  omitShortcutOperation = true; findButton("重新预检本目录快捷方式").emit("click"); await new Promise(resolve => setImmediate(resolve));
  findButton("查看快捷方式处理详情").emit("click"); assert.equal(fallbackRoutes.at(-1), "/api/directory-organizer/shortcuts"); assert.equal(operationOpens.at(-1), "safe-shortcut-fallback");
  mode = "many"; root.value = "U:/Large"; root.emit("input");
  descendants(body).find(node => node.tagName === "button" && node.textContent === "预览目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(descendants(body).filter(node => node.attributes["data-file-source"]).length, 0);
  assert.ok(!descendants(body).some(node => node.attributes["data-move-kind"]));
  assert.ok(descendants(body).filter(node => node.attributes["data-directory-entry"]).length <= 200);
  directoryControl("enter:u:/test/a/_disc").emit("click"); directoryControl("enter:u:/test/a/_disc/season01").emit("click");
  const directoryPageSize = descendants(body).filter(node => node.attributes["data-directory-entry"]).length;
  assert.ok(directoryPageSize > 0 && directoryPageSize <= 200, "directory pages remain bounded when their size follows the viewport");
  assert.ok(descendants(body).some(node => node.attributes["data-directory-entry"] === "U:/Test/A/_Disc/Season01/00000.mkv"));
  directoryControl("next").emit("click");
  assert.equal(descendants(body).filter(node => node.attributes["data-directory-entry"]).length, directoryPageSize);
  assert.ok(!descendants(body).some(node => node.attributes["data-directory-entry"] === "U:/Test/A/_Disc/Season01/00000.mkv"));
  directoryControl("expand-all").emit("click");
  assert.ok(descendants(body).filter(node => node.attributes["data-directory-path"]).length <= 400);
  findButton("查看逐文件移动详情…").emit("click");
  assert.equal(descendants(body).filter(node => node.attributes["data-file-source"]).length, 200);
  descendants(body).find(node => node.tagName === "button" && node.textContent === "下一页文件").emit("click");
  assert.equal(descendants(body).filter(node => node.attributes["data-file-source"]).length, 200);
  assert.ok(!descendants(body).some(node => node.attributes["data-file-source"] === "source-0.mkv"));
  feature.deactivate(); assert.equal(dialog(), undefined);
  mode = "background"; backgroundPreview = deferred(); root.value = "U:/Background"; root.emit("input");
  findButton("预览目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  findButton("查看逐文件移动详情…").emit("click"); const openWhileWaiting = dialog();
  backgroundPreview.resolve({ res: { ok: true }, data: { ok: true, plan: { id: "other-plan", can_execute: true, files: [],
    draft: { records: [], work_root: "U:/Test", release_name: "B" }, source_path: "U:/Test/B", source_directories: [], source_structure_complete: true } } });
  await new Promise(resolve => setImmediate(resolve)); assert.ok(dialog() === openWhileWaiting, "an unrelated background preview must not close the active details window");
  findButton("B").emit("click"); assert.equal(dialog(), undefined);
  mode = "directory"; root.value = "U:/Directory"; root.emit("input");
  findButton("预览目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(descendants(body).filter(node => node.attributes["data-structure-side"]).length, 1);
  assert.ok(!descendants(body).some(node => node.attributes["data-move-kind"]));
  findButton("查看逐文件移动详情…").emit("click"); assert.equal(descendants(dialog()).filter(node => node.attributes["data-file-source"]).length, 2);
  selectTab("整理目标");
  const completedControl = descendants(body).find(node => node.type === "checkbox" && node.parentNode.children.some(child => child.textContent === "媒体目录已整理完成"));
  completedControl.checked = true; completedControl.emit("change"); assert.equal(dialog(), undefined);
  selectTab("目录预览"); findButton("查看逐文件移动详情…").emit("click");
  mode = "complete"; root.value = "U:/Complete"; root.emit("input");
  assert.equal(dialog(), undefined);
  descendants(body).find(node => node.tagName === "button" && node.textContent === "预览目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.ok(descendants(body).some(node => node.textContent === "目录已整理")); assert.ok(descendants(body).some(node => node.textContent === "DB 待补齐"));
  assert.ok(!descendants(body).some(node => node.attributes["data-file-source"]));
  findButton("查看逐文件移动详情…").emit("click");
  descendants(body).find(node => node.tagName === "button" && node.textContent === "全部文件").emit("click");
  descendants(body).find(node => node.tagName === "button" && node.textContent === "编辑文件去向").emit("click");
  const destination = descendants(body).find(node => node.tagName === "input" && node.parentNode.children[0].textContent === "目标相对路径");
  destination.focus();
  destination.emit("input"); assert.equal(dialog().attributes["data-preview-stale"], undefined);
  destination.value = "_Disc/manual.mkv"; destination.emit("input");
  assert.equal(dialog().attributes["data-preview-stale"], "true"); assert.equal(doc.activeElement, destination);
  assert.ok(descendants(body).some(node => node.tagName === "h3" && node.textContent === "目录结构预览（待重新预览）"));
  assert.equal(findButton("确认并锁定此预览").disabled, true);
  descendants(body).find(node => node.tagName === "button" && node.textContent === "重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.at(-1).payload.draft.completed, false); assert.equal(requests.at(-1).payload.draft.file_targets["original.mkv"], "_Disc/manual.mkv");
  assert.equal(dialog(), undefined);
  findButton("查看逐文件移动详情…").emit("click");
  feature.dispose(); assert.equal(doc.getElementById("directory-organizer-view"), undefined);
  assert.equal(dialog(), undefined);
});

async function enumUiFixture(config, settings = {}) {
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.style = {}; this.textContent = ""; this.value = ""; }
    get value() { return this._value || ""; }
    set value(value) { this._value = this.tagName !== "select" || this.children.some(option => option.value === String(value)) ? String(value) : ""; }
    get firstChild() { return this.children[0]; }
    get isConnected() { return this === body || !!(this.parentNode && this.parentNode.isConnected); }
    appendChild(child) { this.children.push(child); child.parentNode = child.parentElement = this; return child; }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; }
    setAttribute(key, value) { this.attributes[key] = value; }
    getAttribute(key) { return this.attributes[key] ?? null; }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    emit(type, extra = {}) { (this.listeners[type] || []).forEach(callback => callback({ target: this, preventDefault() {}, ...extra })); }
    focus() { document.activeElement = this; }
  }
  const body = new Element("body"), nav = new Element("nav"), shell = new Element("div"); body.appendChild(nav); body.appendChild(shell);
  const descendants = node => [node, ...node.children.flatMap(descendants)], all = () => descendants(body);
  const document = { body, createElement: tag => new Element(tag), getElementById: id => all().find(node => node.id === id), querySelector: selector => selector === ".app-tabs" ? nav : selector === ".nimda-app-shell" ? shell : null };
  const workForms = [];
  let feature; const window = { NimdaCommon: { ensureFeatureRegistry: () => ({ register: value => { feature = value; } }) }, NimdaNewWorkDialog: { open: options => workForms.push(options) } }, requests = [];
  vm.runInNewContext(enumCode, { window });
  vm.runInNewContext(workFormCode, { window });
  vm.runInNewContext(directoryBrowserCode, { window, document });
  vm.runInNewContext(sectionTabsCode, { window, document });
  const browserLifecycle = { mounts: 0, destroys: 0, live: new Set(), peak: 0 }, mountBrowser = window.NimdaCommon.DirectoryBrowser.mount;
  window.NimdaCommon.DirectoryBrowser.mount = (host, options) => {
    const browser = mountBrowser(host, options), marker = {};
    browserLifecycle.mounts++; browserLifecycle.live.add(marker); browserLifecycle.peak = Math.max(browserLifecycle.peak, browserLifecycle.live.size);
    return { update: options => browser.update(options), destroy() { if (browserLifecycle.live.delete(marker)) browserLifecycle.destroys++; browser.destroy(); } };
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../frontend/structure-preview.js"), "utf8"), { window });
  vm.runInNewContext(code, { window, document });
  const record = window.NimdaDirectoryOrganizer.template("Existing"), attrs = window.NimdaDirectoryOrganizer.attribute;
  attrs(record, "country", "").data = "legacy-country"; record.extension = { preserve: ["yes"] };
  attrs(record, "date", {}).data = { start: "20260921", end: "20261001" };
  Object.assign(attrs(record, "collection-type", {}).data, { domain: "", release_type: "tv", markers: ["legacy-marker"],
    collectioned: [{ press_format: "UnknownFormat", press_group: "", press_path: "A", vendor: { id: "main-row" } }],
    continuations: [{ title: "Extra", vendor: "continuation", collectioned: [{ press_format: "WEB", press_group: "OldGroup", press_path: "Extra" }] }] });
  let planSerial = 0;
  const context = { config, fetchJson: async (url, options) => {
    const payload = JSON.parse(options.body); requests.push({ url, payload });
    if (url.endsWith("scan")) return { ok: true, root: payload.root, children: ["A", "B"].map(name => ({ id: name, name, path: "U:/Test/" + name })), strategies: [{ id: "auto", label: "自动" }, { id: "generic", label: "通用" }] };
    if (url.endsWith("preview")) return { ok: true, plan: { id: "enum-plan-" + ++planSerial, can_execute: !(settings.issues || []).some(issue => issue.blocking), source_path: "U:/Test/A", source_directories: settings.sourceDirectories || [], source_structure_complete: true, files: settings.files || [], catalog_candidates: [], issues: settings.issues || [],
      draft: payload.draft || { work_root: "U:/Test", release_name: "A", records: [{ ref: settings.newDraft ? null : { yaml_source_rel: "test.yaml", index_in_file: 0 }, confirmed: false, record: plain(record), press_keys: settings.pressKeys || [] }] } } };
    if (settings.allowExecute && url.endsWith("execute")) return { ok: true, results: payload.plan_ids.map(plan_id => ({ plan_id, status: "succeeded", message: "fixture only" })) };
    throw Error("Only read-only routes are allowed in this fixture: " + url);
  } };
  feature.init(context);
  const inHiddenPanel = node => !!node && ((node.attributes.role === "tabpanel" && node.hidden) || inHiddenPanel(node.parentNode));
  const button = label => all().find(node => node.tagName === "button" && node.textContent === label && !inHiddenPanel(node));
  const tab = label => all().find(node => node.attributes.role === "tab" && node.attributes["aria-label"] === label);
  const panel = label => document.getElementById(tab(label).attributes["aria-controls"]);
  const selectTab = label => { const found = tab(label); assert.ok(found, "detail tab exists: " + label); found.emit("click"); };
  const fields = key => all().filter(node => node.attributes["data-enum-field"] === key);
  const cards = () => all().filter(node => node.attributes["data-record-index"] !== undefined);
  function openSettings(index = 0) {
    const card = cards().find(node => String(node.attributes["data-record-index"]) === String(index));
    assert.ok(card, "record card exists at index " + index);
    const toggle = descendants(card).find(node => node.tagName === "button" && ["关联设置", "选择压制记录", "收起关联设置"].includes(node.textContent));
    assert.ok(toggle, "record card has an association settings control");
    if (toggle.attributes["aria-expanded"] !== "true") toggle.emit("click");
  }
  const root = all().find(node => node.attributes["aria-label"] === "待整理目录路径"); root.value = "U:/Test"; root.emit("input");
  button("预览目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  if (settings.activeTab !== false) selectTab(settings.activeTab || "关联 DB");
  if (settings.openAdvanced !== false) openSettings();
  return { feature, context, document, requests, all, descendants, button, tab, panel, selectTab, inHiddenPanel, fields, workForms, root, cards, openSettings, browserLifecycle, json: () => all().find(node => node.tagName === "textarea") };
}

test("organizer offers only auto and generic and changing strategy invalidates the preview", async () => {
  const f = await enumUiFixture({}, { activeTab: "整理目标", openAdvanced: false });
  const ruleSelect = () => f.all().find(node => node.attributes["aria-label"] === "整理规则");
  assert.deepEqual(ruleSelect().children.map(option => [option.value, option.textContent]), [["auto", "自动"], ["generic", "通用"]]);
  assert.equal(ruleSelect().value, "auto");
  for (const strategy of ["generic", "auto"]) {
    const select = ruleSelect(); select.value = strategy; select.emit("change");
    assert.equal(ruleSelect().value, strategy);
    assert.equal(f.button("确认并锁定此预览").disabled, true);
    assert.equal(f.button("执行此目录").disabled, true);
    f.button("重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.requests.at(-1).payload.strategy, strategy);
    assert.equal(ruleSelect().value, strategy);
    assert.equal(f.button("确认并锁定此预览").disabled, false);
  }
  f.button("确认并锁定此预览").emit("click");
  assert.equal(ruleSelect().disabled, true);
  assert.ok(f.requests.every(request => /\/(scan|preview)$/.test(request.url)));
  f.feature.dispose();
});

test("organizer edits work and main presses through the shared form with canonical enum config and preserves advanced data", async () => {
  const config = { enum_options: { domain: ["animation", { value: "live" }, "animation"], country: [{ value: "japan" }, "china"], release_type: ["tv", { value: "movie" }],
    press_format: ["BDRip", { value: "WEB" }], press_group: ["GroupA", { value: "GroupB" }], markers: ["favorite", { value: "complete" }] },
    enum_labels: { country: { japan: "日本", china: "中国" }, domain: { animation: "动画" }, press_group: { GroupA: "字幕组 A" }, markers: { favorite: "收藏" } } };
  const f = await enumUiFixture(config), before = JSON.parse(f.json().value), requestCount = f.requests.length;
  for (const key of ["domain", "country", "release_type", "markers"]) assert.equal(f.fields(key).length, 0, "basic fields are owned by the shared modal, not duplicated inline");
  assert.equal(f.fields("press_format").length, 1); assert.equal(f.fields("press_group").length, 1);
  assert.equal(f.fields("press_group")[0].value, "OldGroup", "only continuation controls stay inline");
  f.button("编辑作品信息").emit("click"); const form = f.workForms.at(-1);
  assert.equal(form.title, "编辑作品信息"); assert.equal(form.submitLabel, "应用到待保存记录"); assert.equal(form.hideHints, true);
  assert.equal(form.enumOptions, config.enum_options); assert.equal(form.enumLabels, config.enum_labels);
  assert.equal(form.initialData.country, "legacy-country"); assert.equal(form.initialData.domain, "");
  assert.equal(form.initialData.collectioned_ordered.length, 1); assert.equal(form.initialData.collectioned_ordered[0].press_group, "");
  assert.deepEqual(plain(form.initialData.collectioned_ordered[0].vendor), { id: "main-row" });
  assert.equal(f.requests.length, requestCount); assert.equal(JSON.stringify(JSON.parse(f.json().value)), JSON.stringify(before));
  const patch = plain(form.initialData); patch.country = "china"; patch.domain = "animation"; patch.name = "Changed in shared form"; patch.collectioned_ordered[0].press_format = "CustomFormat";
  form.onSubmit(patch);
  assert.equal(f.button("确认并锁定此预览").disabled, true);
  assert.equal(JSON.parse(f.json().value).attributes.find(row => row.type === "country").data, "china");
  let data = JSON.parse(f.json().value), collection = data.attributes.find(row => row.type === "collection-type").data;
  assert.equal(collection.collectioned[0].press_format, "CustomFormat"); assert.equal(collection.collectioned[0].press_group, "");
  assert.deepEqual(collection.collectioned[0].vendor, { id: "main-row" });
  assert.deepEqual(collection.continuations, before.attributes.find(row => row.type === "collection-type").data.continuations);
  assert.deepEqual(data.extension, { preserve: ["yes"] });
  assert.equal(f.requests.length, requestCount, "submitting the modal updates only the organizer draft");
  const extraGroup = f.fields("press_group")[0]; extraGroup.value = ""; extraGroup.emit("input");
  assert.equal(JSON.parse(f.json().value).attributes.find(row => row.type === "collection-type").data.continuations[0].collectioned[0].press_group, "");
  data = JSON.parse(f.json().value); collection = data.attributes.find(row => row.type === "collection-type").data;
  data.attributes.find(row => row.type === "country").data = "legacy-from-json"; collection.domain = "legacy-domain";
  f.json().value = JSON.stringify(data); f.json().emit("input"); f.json().emit("change");
  f.button("编辑作品信息").emit("click"); assert.equal(f.workForms.at(-1).initialData.country, "legacy-from-json"); assert.equal(f.workForms.at(-1).initialData.domain, "legacy-domain");
  f.button("重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.at(-1).payload.draft.records[0].record.attributes.find(row => row.type === "country").data, "legacy-from-json");
  f.button("确认并锁定此预览").emit("click");
  assert.equal(f.button("编辑作品信息").disabled, true); assert.equal(f.button("新增 DB 记录").disabled, true);
  const lockedFormCount = f.workForms.length; f.button("编辑作品信息").emit("click"); f.button("新增 DB 记录").emit("click"); assert.equal(f.workForms.length, lockedFormCount);
  assert.ok(f.all().filter(node => node.attributes["data-enum-field"]).every(node => node.disabled));
  f.feature.dispose();
});

test("config refresh preserves inline continuation drafts and supplies the latest shared config on opening the form", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} });
  const group = f.fields("press_group")[0]; group.value = "typed-group "; group.emit("input"); group.focus();
  const raw = f.json(), rawBefore = raw.value;
  const refreshed = { ...f.context, config: { enum_options: { domain: ["configured-domain"], country: ["new-country"], press_group: [{ value: "configured-group" }] }, enum_labels: { country: { "new-country": "新地区" } } } };
  f.feature.refreshAfterConfig(refreshed);
  assert.equal(f.fields("press_group")[0], group); assert.equal(group.value, "typed-group "); assert.equal(f.document.activeElement, group);
  assert.equal(f.json(), raw); assert.equal(f.json().value, rawBefore);
  assert.deepEqual(f.document.getElementById(group.attributes.list).children.map(option => option.value), ["configured-group"]);
  f.button("编辑作品信息").emit("click"); assert.equal(f.workForms.at(-1).enumOptions, refreshed.config.enum_options); assert.equal(f.workForms.at(-1).enumLabels, refreshed.config.enum_labels);
  const activated = { ...refreshed, config: { enum_options: { press_group: ["on-activation"] }, enum_labels: {} } };
  f.feature.activate(activated); assert.equal(group.value, "typed-group ");
  assert.deepEqual(f.document.getElementById(group.attributes.list).children.map(option => option.value), ["on-activation"]);
  f.button("编辑作品信息").emit("click"); assert.equal(f.workForms.at(-1).enumOptions, activated.config.enum_options);
  assert.ok(f.requests.every(request => /\/(scan|preview)$/.test(request.url)));
  f.feature.dispose();
});

test("cancelling the shared new-work form creates nothing and confirming it changes only the draft", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { openAdvanced: false }), count = f.cards().length, requests = f.requests.length;
  f.button("新增 DB 记录").emit("click");
  assert.equal(f.workForms.at(-1).title, "新增 DB 记录"); assert.equal(f.workForms.at(-1).submitLabel, "添加到待保存记录"); assert.equal(f.cards().length, count);
  assert.equal(f.requests.length, requests);
  if (f.workForms.at(-1).onClose) f.workForms.at(-1).onClose("cancel");
  assert.equal(f.cards().length, count);
  f.button("新增 DB 记录").emit("click"); const form = f.workForms.at(-1), patch = plain(form.initialData);
  patch.name = "New draft only"; patch.domain = "animation"; patch.country = "japan"; patch.release_type = "tv";
  patch.collectioned_ordered[0].press_format = "BDRip"; patch.collectioned_ordered[0].press_group = "";
  form.onSubmit(patch);
  assert.equal(f.cards().length, count + 1); assert.equal(f.all().filter(node => node.tagName === "textarea").length, 0, "adding a record must not open advanced settings");
  const card = f.cards().at(-1);
  assert.ok(f.descendants(card).some(node => node.textContent === "New draft only"));
  assert.ok(f.descendants(card).some(node => node.textContent === "待保存"));
  assert.equal(f.descendants(card).filter(node => node.tagName === "button" && node.textContent === "编辑作品信息").length, 1);
  assert.equal(f.requests.length, requests); assert.equal(f.button("确认并锁定此预览").disabled, true);
  assert.equal(f.button("执行此目录").disabled, true);
  f.openSettings(count); const saved = JSON.parse(f.json().value);
  assert.equal(saved.attributes.find(row => row.type === "name").data, "New draft only");
  f.feature.dispose();
});

test("stale shared-form callbacks cannot overwrite drafts after edits, preview, selection, lock, root changes or disposal", async () => {
  for (const action of ["edit", "preview", "previewing", "selection", "lock", "root", "deactivate", "dispose"]) {
    const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }); f.button("编辑作品信息").emit("click");
    const form = f.workForms.at(-1), patch = plain(form.initialData); patch.name = "Must not apply";
    if (action === "edit") { const data = JSON.parse(f.json().value); data.extension.keep = action; f.json().value = JSON.stringify(data); f.json().emit("input"); f.json().emit("change"); }
    if (action === "preview") { f.button("重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve)); }
    if (action === "previewing") f.button("重新预览此目录").emit("click");
    if (action === "selection") f.button("B").emit("click");
    if (action === "lock") f.button("确认并锁定此预览").emit("click");
    if (action === "root") { f.root.value = "U:/Other"; f.root.emit("input"); }
    if (action === "deactivate") f.feature.deactivate();
    if (action === "dispose") f.feature.dispose();
    assert.throws(() => form.onSubmit(patch), /目录或预览已变化|当前草稿不可编辑/, "reject stale callback after " + action);
    assert.ok(!f.all().some(node => node.tagName === "textarea" && node.value.includes("Must not apply")));
    if (action === "previewing") await new Promise(resolve => setImmediate(resolve));
    if (action !== "dispose") f.feature.dispose();
  }
});

test("compact DB record cards expose one edit action and lazy per-record settings independent of existing-DB search", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { openAdvanced: false });
  let card = f.cards()[0];
  assert.equal(f.cards().length, 1);
  assert.equal(f.descendants(card).filter(node => node.textContent === "Existing").length, 1);
  assert.ok(f.descendants(card).some(node => node.textContent === "已关联"));
  assert.ok(f.descendants(card).some(node => node.textContent === "2026-09-21 ～ 2026-10-01"));
  assert.ok(f.descendants(card).some(node => node.textContent === "压制记录尚未选择"));
  assert.equal(f.descendants(card).filter(node => node.tagName === "button" && node.textContent === "编辑作品信息").length, 1);
  assert.equal(f.button("选择压制记录").attributes["aria-expanded"], "false");
  assert.equal(f.json(), undefined); assert.equal(f.fields("press_group").length, 0);
  assert.equal(f.button("编辑 DB / 关联作品"), undefined); assert.equal(f.button("收起 DB 编辑"), undefined);
  assert.ok(!f.all().some(node => node.attributes["aria-label"] === "查找已有 DB 记录"));
  const requests = f.requests.length;
  f.button("关联已有 DB").emit("click");
  assert.ok(f.all().some(node => node.attributes["aria-label"] === "查找已有 DB 记录"));
  assert.equal(f.requests.length, requests, "opening search does not issue a catalog request");
  assert.equal(f.json(), undefined); assert.equal(f.cards().length, 1);
  f.button("收起已有 DB 查找").emit("click");
  assert.ok(!f.all().some(node => node.attributes["aria-label"] === "查找已有 DB 记录"));
  f.openSettings(); assert.ok(f.json()); assert.equal(f.fields("press_group").length, 1);
  card = f.cards()[0];
  assert.equal(f.descendants(card).filter(node => node.textContent === "Existing").length, 1);
  assert.equal(f.descendants(card).filter(node => node.tagName === "button" && node.textContent === "编辑作品信息").length, 1);
  f.button("关联已有 DB").emit("click"); assert.ok(f.json());
  f.button("收起关联设置").emit("click"); assert.equal(f.json(), undefined);
  assert.ok(f.all().some(node => node.attributes["aria-label"] === "查找已有 DB 记录"), "closing record settings does not close independent search");
  f.feature.dispose();
});

test("adding a pending card preserves existing open settings and expanding another card unmounts the previous advanced editor", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} });
  f.button("新增 DB 记录").emit("click"); const form = f.workForms.at(-1), patch = plain(form.initialData);
  patch.name = "Second record"; patch.collectioned_ordered[0].press_format = "BDRip"; form.onSubmit(patch);
  assert.equal(f.cards().length, 2); assert.equal(f.all().filter(node => node.tagName === "textarea").length, 1);
  assert.ok(f.descendants(f.cards()[0]).some(node => node.tagName === "textarea"));
  assert.ok(!f.descendants(f.cards()[1]).some(node => node.tagName === "textarea"));
  assert.ok(f.descendants(f.cards()[1]).some(node => node.textContent === "待保存"));
  f.openSettings(1);
  assert.equal(f.all().filter(node => node.tagName === "textarea").length, 1);
  assert.ok(!f.descendants(f.cards()[0]).some(node => node.tagName === "textarea"));
  assert.ok(f.descendants(f.cards()[1]).some(node => node.tagName === "textarea"));
  f.button("收起关联设置").emit("click"); assert.equal(f.json(), undefined);
  f.feature.dispose();
});

test("pending DB records retain the preview-lock-execute barrier and become saved only after explicit execution", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { openAdvanced: false, allowExecute: true });
  f.button("新增 DB 记录").emit("click"); const form = f.workForms.at(-1), patch = plain(form.initialData);
  patch.name = "Pending safe save"; patch.collectioned_ordered[0].press_format = "BDRip"; form.onSubmit(patch);
  assert.equal(f.requests.filter(request => request.url.endsWith("execute")).length, 0);
  assert.equal(f.button("确认并锁定此预览").disabled, true); assert.equal(f.button("执行此目录").disabled, true);
  f.button("执行此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.filter(request => request.url.endsWith("execute")).length, 0);
  f.button("重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  const preview = f.requests.at(-1); assert.ok(preview.url.endsWith("preview"));
  assert.equal(preview.payload.draft.records.length, 2); assert.equal(preview.payload.draft.records[1].ref, null); assert.equal(preview.payload.draft.records[1].confirmed, true);
  assert.equal(f.button("执行此目录").disabled, true);
  f.button("执行此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.requests.filter(request => request.url.endsWith("execute")).length, 0);
  f.button("确认并锁定此预览").emit("click"); assert.equal(f.button("执行此目录").disabled, false);
  f.button("执行此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  const executions = f.requests.filter(request => request.url.endsWith("execute")); assert.equal(executions.length, 1); assert.equal(executions[0].payload.confirm, true);
  assert.ok(f.descendants(f.cards()[1]).some(node => node.textContent === "已保存"));
  const result = f.all().find(node => (node.className || "").split(" ").includes("organizer-result-summary")); assert.ok(result);
  for (let parent = result.parentNode; parent; parent = parent.parentNode) assert.notEqual(parent.attributes.role, "tabpanel", "the execution result summary is not hidden in a detail tab");
  for (const label of ["目录预览", "整理目标", "关联 DB"]) { f.selectTab(label); assert.ok(f.button("查看处理详情")); assert.equal(f.inHiddenPanel(result), false); }
  assert.ok(!f.all().some(node => node.textContent === "完整执行结果 JSON"));
  f.feature.dispose();
});

test("unconfirmed new records show incomplete status without masquerading as saved records", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { openAdvanced: false, newDraft: true });
  const card = f.cards()[0]; assert.ok(f.descendants(card).some(node => node.textContent === "待补齐"));
  assert.ok(!f.descendants(card).some(node => ["待保存", "已保存", "已关联"].includes(node.textContent)));
  assert.equal(f.json(), undefined); f.feature.dispose();
});

test("completing inferred work information updates its pending card without appending a duplicate DB record", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { openAdvanced: false, newDraft: true });
  const requests = f.requests.length;
  assert.equal(f.button("编辑作品信息"), undefined); assert.ok(f.button("补齐作品信息"));
  f.button("补齐作品信息").emit("click"); const form = f.workForms.at(-1), patch = plain(form.initialData);
  assert.equal(form.submitLabel, "应用到待保存记录"); assert.equal(f.cards().length, 1);
  patch.name = "Completed inferred draft"; form.onSubmit(patch);
  assert.equal(f.cards().length, 1); assert.equal(f.requests.length, requests, "completion changes only the in-memory draft");
  assert.ok(f.descendants(f.cards()[0]).some(node => node.textContent === "Completed inferred draft"));
  assert.ok(f.descendants(f.cards()[0]).some(node => node.textContent === "待保存"));
  assert.equal(f.button("补齐作品信息"), undefined); assert.ok(f.button("编辑作品信息"));
  assert.equal(f.json(), undefined); assert.equal(f.button("确认并锁定此预览").disabled, true); assert.equal(f.button("执行此目录").disabled, true);
  f.button("重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  const preview = f.requests.at(-1); assert.ok(preview.url.endsWith("preview"));
  assert.equal(preview.payload.draft.records.length, 1); assert.equal(preview.payload.draft.records[0].ref, null); assert.equal(preview.payload.draft.records[0].confirmed, true);
  f.feature.dispose();
});

test("organizer destroys each directory browser before remounting and releases the final browser on root reset or disposal", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { openAdvanced: false });
  assert.equal(f.browserLifecycle.live.size, 1); assert.equal(f.browserLifecycle.peak, 1);
  f.button("关联已有 DB").emit("click"); f.openSettings(); f.selectTab("目录预览"); f.button("整理前").emit("click"); f.button("整理后").emit("click");
  assert.equal(f.browserLifecycle.live.size, 1); assert.equal(f.browserLifecycle.peak, 1); assert.ok(f.browserLifecycle.destroys >= 4);
  f.root.value = "U:/Changed"; f.root.emit("input"); assert.equal(f.browserLifecycle.live.size, 0);
  f.button("预览目录").emit("click"); await new Promise(resolve => setImmediate(resolve)); assert.equal(f.browserLifecycle.live.size, 1);
  f.feature.dispose(); assert.equal(f.browserLifecycle.live.size, 0); assert.equal(f.browserLifecycle.mounts, f.browserLifecycle.destroys);
});

test("detail tabs default to the preview and expose ARIA keyboard navigation without requests or browser remounts", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { activeTab: false, openAdvanced: false });
  const labels = ["目录预览", "整理目标", "关联 DB"], initialRequests = f.requests.length, mounts = f.browserLifecycle.mounts;
  const panels = labels.map(label => f.panel(label));
  const targetInput = f.descendants(f.panel("整理目标")).find(node => node.tagName === "input" && node.parentNode.children[0].textContent === "目标作品根目录"), originalCard = f.cards()[0];
  assert.equal(f.all().filter(node => node.attributes.role === "tablist").length, 1);
  assert.equal(new Set(panels.map(panel => panel.id)).size, 3);
  function assertActive(label) {
    labels.forEach(current => {
      const selected = current === label, tab = f.tab(current), panel = f.panel(current);
      assert.equal(tab.attributes["aria-selected"], String(selected));
      assert.equal(tab.tabIndex, selected ? 0 : -1);
      assert.equal(panel.attributes.role, "tabpanel"); assert.equal(panel.attributes["aria-labelledby"], tab.id);
      assert.equal(!!panel.hidden, !selected);
    });
  }
  assertActive("目录预览"); assert.equal(f.button("新增 DB 记录"), undefined);
  assert.ok(f.button("查看逐文件移动详情…"));
  f.tab("目录预览").emit("keydown", { key: "ArrowRight" }); assertActive("整理目标"); assert.equal(f.document.activeElement, f.tab("整理目标"));
  f.tab("整理目标").emit("keydown", { key: "Home" }); assertActive("目录预览");
  f.tab("目录预览").emit("keydown", { key: "End" }); assertActive("关联 DB");
  f.tab("关联 DB").emit("keydown", { key: "ArrowRight" }); assertActive("目录预览");
  f.tab("目录预览").emit("keydown", { key: "ArrowLeft" }); assertActive("关联 DB");
  f.selectTab("整理目标"); assertActive("整理目标");
  assert.equal(f.button("修改目标 / 整理规则"), undefined);
  assert.ok(f.descendants(f.panel("整理目标")).some(node => node.tagName === "select" && !f.inHiddenPanel(node)));
  assert.ok(f.descendants(f.panel("整理目标")).some(node => node.tagName === "input" && node.parentNode.children[0].textContent === "目标作品根目录"));
  f.selectTab("目录预览");
  assert.equal(f.requests.length, initialRequests); assert.equal(f.browserLifecycle.mounts, mounts);
  labels.forEach((label, index) => assert.equal(f.panel(label), panels[index], "switching tabs keeps mounted controls and their values"));
  assert.equal(f.descendants(f.panel("整理目标")).find(node => node.tagName === "input" && node.parentNode.children[0].textContent === "目标作品根目录"), targetInput);
  assert.equal(f.cards()[0], originalCard, "switching without edits keeps the actual DB card, not only its containing panel");
  f.feature.dispose();
});

test("blocking issues and preview-lock-execute controls remain outside all detail tabs", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { activeTab: false, openAdvanced: false,
    issues: [{ code: "press-selection", blocking: true, message: "先选择压制记录再预览" }] });
  const insidePanel = node => !!node && (node.attributes.role === "tabpanel" || insidePanel(node.parentNode));
  const requests = f.requests.length;
  for (const label of ["目录预览", "整理目标", "关联 DB"]) {
    f.selectTab(label);
    for (const caption of ["重新预览此目录", "确认并锁定此预览", "执行此目录"]) {
      const control = f.button(caption); assert.ok(control); assert.equal(insidePanel(control), false);
    }
    const issue = f.all().find(node => node.textContent.includes("先选择压制记录再预览"));
    assert.ok(issue); assert.equal(insidePanel(issue), false); assert.equal(f.inHiddenPanel(issue), false);
    for (const statusClass of ["organizer-media-status", "organizer-db-status"]) {
      const status = f.all().find(node => (node.className || "").split(" ").includes(statusClass));
      assert.ok(status); assert.equal(insidePanel(status), false);
    }
    assert.equal(f.button("确认并锁定此预览").disabled, true); assert.equal(f.button("执行此目录").disabled, true);
  }
  assert.equal(f.requests.length, requests); f.feature.dispose();
});

test("each child retains its selected detail tab, directory page and unsubmitted JSON across navigation", async () => {
  const files = Array.from({ length: 35 }, (_, index) => ({ source_rel: "source-" + index + ".mkv", target_rel: "episode-" + String(index).padStart(3, "0") + ".mkv", changed: true }));
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { activeTab: false, openAdvanced: false, files });
  const requests = f.requests.length, entries = () => f.all().filter(node => node.attributes["data-directory-entry"]).map(node => node.attributes["data-directory-entry"]);
  const firstPage = entries(); f.all().find(node => node.attributes["data-directory-control"] === "next").emit("click"); const nextPage = entries();
  assert.notDeepEqual(nextPage, firstPage);
  f.selectTab("整理目标"); f.selectTab("关联 DB"); f.selectTab("目录预览"); assert.deepEqual(entries(), nextPage);
  f.button("B").emit("click"); assert.equal(f.tab("目录预览").attributes["aria-selected"], "true");
  f.selectTab("整理目标"); f.button("A").emit("click"); assert.equal(f.tab("目录预览").attributes["aria-selected"], "true"); assert.deepEqual(entries(), nextPage);
  f.selectTab("关联 DB"); f.openSettings(); const raw = f.json(); raw.value = '{"unfinished":'; raw.emit("input");
  f.selectTab("目录预览");
  const jsonIssue = f.all().find(node => node.textContent.includes("[record-json]")); assert.ok(jsonIssue); assert.equal(f.inHiddenPanel(jsonIssue), false);
  f.selectTab("关联 DB"); assert.equal(f.json().value, '{"unfinished":', "a version-aware panel refresh preserves even invalid unsubmitted JSON");
  f.button("B").emit("click"); assert.equal(f.tab("整理目标").attributes["aria-selected"], "true");
  f.button("A").emit("click"); assert.equal(f.tab("关联 DB").attributes["aria-selected"], "true"); assert.equal(f.json().value, '{"unfinished":');
  assert.equal(f.button("确认并锁定此预览").disabled, true); assert.equal(f.button("执行此目录").disabled, true);
  assert.equal(f.requests.length, requests); f.feature.dispose();
});

test("target edits survive detail-tab switches and stay invalid until an explicit preview uses the edited draft", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { activeTab: "整理目标", openAdvanced: false });
  const field = label => f.descendants(f.panel("整理目标")).find(node => node.tagName === "input" && node.parentNode.children[0].textContent === label);
  const requests = f.requests.length, rootField = field("目标作品根目录"), releaseField = field("目标压制文件夹名称");
  rootField.value = "U:/Edited work"; rootField.emit("input"); releaseField.value = "Edited release"; releaseField.emit("input");
  assert.ok(f.all().some(node => node.textContent === "目标：U:/Edited work/Edited release"), "the common target summary follows edits immediately");
  f.selectTab("关联 DB"); f.selectTab("目录预览");
  assert.ok(f.all().some(node => node.textContent === "目录结构预览（待重新预览）"));
  assert.equal(f.button("确认并锁定此预览").disabled, true); assert.equal(f.button("执行此目录").disabled, true);
  f.selectTab("整理目标"); assert.equal(field("目标作品根目录").value, "U:/Edited work"); assert.equal(field("目标压制文件夹名称").value, "Edited release");
  assert.equal(f.requests.length, requests);
  f.selectTab("关联 DB"); f.button("重新预览此目录").emit("click"); await new Promise(resolve => setImmediate(resolve));
  const preview = f.requests.at(-1); assert.ok(preview.url.endsWith("preview"));
  assert.equal(preview.payload.draft.work_root, "U:/Edited work"); assert.equal(preview.payload.draft.release_name, "Edited release");
  assert.equal(f.tab("关联 DB").attributes["aria-selected"], "true");
  f.feature.dispose();
});

test("entering the DB tab after target typing refreshes bound press summaries while retaining search and advanced state", async () => {
  const f = await enumUiFixture({ enum_options: {}, enum_labels: {} }, { pressKeys: ["0:fixture-main"] });
  f.button("关联已有 DB").emit("click");
  const query = f.all().find(node => node.attributes["aria-label"] === "查找已有 DB 记录"); query.value = "unsent catalog query"; query.emit("input");
  const requests = f.requests.length, mounts = f.browserLifecycle.mounts, oldCard = f.cards()[0];
  const summary = () => f.descendants(f.cards()[0]).find(node => (node.className || "").split(" ").includes("organizer-db-press-summary"));
  assert.equal(summary().textContent, "UnknownFormat · 无压制组 · A");
  f.selectTab("整理目标");
  const release = f.descendants(f.panel("整理目标")).find(node => node.tagName === "input" && node.parentNode.children[0].textContent === "目标压制文件夹名称");
  release.value = "_EditedRelease"; release.emit("input"); f.selectTab("关联 DB");
  assert.equal(summary().textContent, "UnknownFormat · 无压制组 · _EditedRelease"); assert.notEqual(f.cards()[0], oldCard);
  assert.ok(f.json(), "the open record settings stay expanded after refreshing the entered panel");
  const record = JSON.parse(f.json().value), collection = record.attributes.find(row => row.type === "collection-type").data;
  assert.equal(collection.collectioned[0].press_path, "_EditedRelease"); assert.equal(collection.continuations[0].collectioned[0].press_path, "Extra");
  assert.deepEqual(record.extension, { preserve: ["yes"] });
  assert.equal(f.all().find(node => node.attributes["aria-label"] === "查找已有 DB 记录").value, "unsent catalog query");
  assert.equal(f.button("收起关联设置").attributes["aria-expanded"], "true");
  const refreshedCard = f.cards()[0], refreshedJson = f.json(); f.selectTab("目录预览"); f.selectTab("关联 DB");
  assert.equal(f.cards()[0], refreshedCard); assert.equal(f.json(), refreshedJson, "further switches with no draft change do not refresh again");
  assert.equal(f.requests.length, requests); assert.equal(f.browserLifecycle.mounts, mounts); assert.equal(f.browserLifecycle.live.size, 1);
  f.feature.dispose();
});
