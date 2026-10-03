"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "../frontend/index.js");
const AUTO = "__organizer_press_group_auto__";
const ROOT = "U:\\bLEACH";
const SOURCE = "BLEACH_DVDRip";
const REF = { yaml_source_rel: "[JP][TVInfo][2004].yaml", index_in_file: 0, work_name: "Bleach" };

function setup() {
  let hooks;
  const listeners = {};
  const calls = [];
  const view = {
    innerHTML: "", contains() { return true; },
    addEventListener(name, callback) { listeners[name] = callback; }, removeEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
  };
  const document = {
    getElementById(id) { return id === "media-directory-organizer-view" ? view : null; },
    querySelector() { return null; },
  };
  const window = { document, setTimeout, __NIMDA_ORGANIZER_TEST_HOOK__(value) { hooks = value; } };
  window.window = window;
  vm.runInNewContext(fs.readFileSync(FRONTEND, "utf8"), { window, document, console, setTimeout, clearTimeout }, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features.find(item => item.id === "media-directory-organizer");
  feature.init({ fetchJson(url, options) {
    calls.push({ url, body: options && options.body ? JSON.parse(options.body) : null });
    return Promise.resolve({ res: { ok: true }, data: plan() });
  } });
  Object.assign(hooks.state, {
    root: ROOT,
    config: {
      paths: { allowed_resource_roots: ["U:\\"], catalog_root: "fixture" },
      detection: { format_markers: { DVDRip: ["DVD"], BDRip: ["BD"] } },
      group_registry: { options: [{ code: "VCB", label: "VCB" }, { code: "JSUM", label: "JSUM" }] },
    },
    plan: plan(),
  });
  return { ...hooks, view, listeners, calls };
}

function plan(overrides = {}) {
  return { ok: true, ready: false, root: ROOT, plan_id: "aaaaaaaaaaaaaaaa", assignments: [], moves: [],
    issues: [], unresolved_files: [], summary: {}, ...overrides };
}

function press(overrides = {}) {
  return { source_names: [SOURCE], press_format: "DVDRip", press_group: "", press_path: SOURCE, ...overrides };
}

function draft(overrides = {}) {
  return { name: "Bleach", path: ROOT, date: { start: "20041005", end: "20120327" },
    domain: "animation", country: "japan", release_type: "tv", presses: [press()], ...overrides };
}

function bindingPlan(group = "") {
  return plan({ source_work_bindings: [{ source_name: SOURCE, source_path: ROOT + "\\" + SOURCE,
    state: "catalog_bound", catalog_ref: REF, work: draft({ presses: [press({ press_group: "VCB" })] }),
    inferred_press: [press({ press_group: group })], catalog_press_required: true, catalog_repair_required: true }] });
}

function input(app, selector, value, attrs = {}, eventType = "input") {
  app.listeners[eventType]({ type: eventType, target: {
    value, matches(candidate) { return candidate === selector; },
    getAttribute(name) { return Object.hasOwn(attrs, name) ? attrs[name] : ""; },
  } });
}

function plain(value) { return JSON.parse(JSON.stringify(value)); }

test("all group editors distinguish an explicit no-group choice from an unselected group", () => {
  const app = setup();
  for (const renderer of [app.renderGroupOptions, app.renderRepairGroupOptions]) {
    assert.match(renderer(undefined), new RegExp('<option value="' + AUTO + '" selected>'));
    assert.match(renderer(""), /<option value="" selected>—（无压制组）<\/option>/);
    for (const legacy of ["---", "----", "—"]) {
      const html = renderer(legacy);
      assert.match(html, /<option value="" selected>—（无压制组）<\/option>/);
      assert.doesNotMatch(html, /历史数据库值/);
    }
    assert.match(renderer("VCB"), /<option value="VCB" selected>/);
  }
});

test("source override payload retains explicit empty groups without converting automatic detection", () => {
  const app = setup();
  app.state.sourcePressOverrides = {
    empty: { press_group: "" }, legacy: { press_group: "----" },
    automatic: {}, unresolved: { press_group: "", press_group_confirmed: false },
    formatOnly: { press_format: "DVDRip" }, pending: { press_group: AUTO },
  };
  assert.deepEqual(plain(app.sourcePressOverridesPayload()), {
    empty: { press_group: "" }, legacy: { press_group: "" }, formatOnly: { press_format: "DVDRip" },
  });
});

test("automatically generated empty-group drafts require selection before source landing", () => {
  const app = setup();
  app.state.plan = bindingPlan();
  const pendingDraft = draft({ presses: [press({ press_group_confirmed: false })] });
  app.state.sourceWorkBindingEdits[SOURCE] = { mode: "draft", draft_work: pendingDraft };
  app.state.draftWork = pendingDraft;
  app.render();
  const select = app.view.innerHTML.match(/<select data-landing-press="press_group"[^>]*>[\s\S]*?<\/select>/)[0];
  assert.match(select, new RegExp('<option value="' + AUTO + '" selected>'));
  assert.doesNotMatch(select, /<option value="" selected>—（无压制组）<\/option>/);
  assert.match(app.sourceWorkLandingPayloadIssue(app.sourceWorkBindingsPayload({ forLanding: true })), /选择压制组/);
});

for (const code of ["group-unresolved", "group-ambiguous", "format-ambiguous"]) {
  test(`${code} offers no-group and sends an explicit empty selection, then automatic detection clears it`, async () => {
    const app = setup();
    const source = ROOT + "\\" + SOURCE;
    app.state.plan = plan({ issues: [{ code, message: "choose group", path: source,
      press_groups: ["", "VCB"], press_formats: ["DVDRip", "BDRip"] }] });
    app.render();
    assert.match(app.view.innerHTML, /自动识别压制组/);
    assert.match(app.view.innerHTML, /—（无压制组）/);
    const attrs = { "data-source-press-path": source, "data-source-press-field": "press_group" };
    input(app, "[data-source-press-field]", "", attrs, "change");
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(app.calls.at(-1).body.source_press_overrides[source].press_group, "");
    assert.equal(Object.hasOwn(app.calls.at(-1).body.source_press_overrides[source], "press_group"), true);
    input(app, "[data-source-press-field]", AUTO, attrs, "change");
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(Object.hasOwn(app.calls.at(-1).body.source_press_overrides, source), false);
  });
}

test("catalog press selection preserves explicit empty group instead of falling back to a named group", () => {
  const app = setup();
  app.state.plan = bindingPlan();
  const payload = app.sourceWorkBindingsPayload({ forLanding: true });
  assert.equal(payload[SOURCE].presses[0].press_group, "");
  assert.equal(app.sourceWorkLandingPayloadIssue(payload), "");
  assert.equal(app.updateSourceCatalogPress(SOURCE, "press_group", AUTO, 0), true);
  assert.match(app.sourceWorkLandingPayloadIssue(app.sourceWorkBindingsPayload({ forLanding: true })), /选择压制组/);
  assert.equal(app.updateSourceCatalogPress(SOURCE, "press_group", "", 0), true);
  assert.equal(app.sourceWorkLandingPayloadIssue(app.sourceWorkBindingsPayload({ forLanding: true })), "");
});

test("manual source draft confirms no-group explicitly and retains it through landing payload", () => {
  const app = setup();
  app.state.plan = bindingPlan();
  app.state.sourceWorkDrafts[SOURCE] = draft({ presses: [press({ press_group_confirmed: false })] });
  assert.equal(app.updateSourceWorkDraft(SOURCE, "press_group", "", "press", 0), true);
  const payload = app.sourceWorkBindingsPayload({ forLanding: true });
  assert.equal(payload[SOURCE].draft_work.presses[0].press_group, "");
  assert.equal(payload[SOURCE].draft_work.presses[0].press_group_confirmed, true);
  assert.equal(app.sourceWorkLandingPayloadIssue(payload), "");
});

test("landing and repair edit events permit no-group, while unresolved repair selection remains blocked", () => {
  const app = setup();
  app.state.draftWork = draft({ presses: [press({ press_group: "VCB" })] });
  input(app, "[data-landing-press]", "", { "data-landing-press": "press_group", "data-press-index": "0", "data-work-index": "0" });
  assert.equal(app.state.draftWork.presses[0].press_group, "");
  assert.equal(app.state.draftWork.presses[0].press_group_confirmed, true);
  app.state.shortcutPending = { root: ROOT, repairDraft: draft(), repairPreview: {}, repairRequest: {} };
  input(app, "[data-repair-press]", AUTO, { "data-repair-press": "press_group", "data-repair-press-index": "0" });
  assert.throws(() => app.shortcutRepairRequest(-1), /尚未选择压制组/);
  input(app, "[data-repair-press]", "", { "data-repair-press": "press_group", "data-repair-press-index": "0" });
  const payload = app.shortcutRepairRequest(-1);
  assert.equal(payload.draft_work.presses[0].press_group, "");
  assert.equal(payload.draft_work.presses[0].press_group_confirmed, true);
  assert.equal(app.state.shortcutPending.repairPreview, null);
  assert.equal(app.state.shortcutPending.repairRequest, null);
});

test("multiple broadcast records can share a physical release with no group", () => {
  const app = setup();
  const candidates = [0, 1].map(index => {
    const ref = { ...REF, index_in_file: index };
    return { label: "Bleach " + index, catalog_ref: ref, draft_work: draft(),
      shared_target_members: [{ catalog_ref: ref, press_key: "release-" + index,
        press_format: "DVDRip", press_group: "", press_path: SOURCE }] };
  });
  app.state.plan = plan({ registration: { existing_candidates: candidates, shared_target_supported: true } });
  const bundle = app.sharedTargetCandidateBundle(app.state.plan);
  assert.equal(bundle.members.length, 2);
  app.state.sharedTarget.confirmed = true;
  for (const member of bundle.members) app.state.sharedTarget.selectedMembers[member.key] = true;
  const result = app.buildSharedTargetBindings(app.state.plan);
  assert.equal(result.error, "");
  assert.equal(result.bindings.length, 1);
  assert.equal(result.bindings[0].press_group, "");
  assert.equal(result.bindings[0].members.length, 2);
});
