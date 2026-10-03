"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "../frontend/index.js");

function createHarness(configuredRoot = "") {
  const calls = [];
  const handlers = {};
  let hooks;
  const view = {
    innerHTML: "",
    addEventListener(name, handler) { handlers[name] = handler; },
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    contains() { return true; },
  };
  const document = {
    getElementById(id) { return id === "media-directory-organizer-view" ? view : null; },
    querySelector() { return null; },
  };
  const window = {
    document,
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) { hooks = value; },
  };
  window.window = window;
  vm.runInNewContext(fs.readFileSync(FRONTEND, "utf8"), {
    window, document, console, setTimeout, clearTimeout,
  }, { filename: FRONTEND });
  const config = {
    ok: true,
    paths: { allowed_resource_roots: ["U:\\"], default_work_root: configuredRoot },
  };
  window.JpTvBrowseFeatureRegistry.features[0].init({
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      const data = url.endsWith("/config") ? config : {
        ok: true,
        plan: {
          root: body.root, ready: false, assignments: [], moves: [], issues: [],
          family_works: [], unresolved_files: [], source_work_bindings: [],
          summary: { source_directory_count: 0, file_count: 0, bytes: 0 },
        },
      };
      return Promise.resolve({ res: { ok: true, status: 200 }, data });
    },
  });
  hooks.state.config = config;
  function editRoot(value, type = "input", inputType = "insertFromPaste") {
    const input = {
      id: "organizer-root-input", value,
      matches() { return false; }, getAttribute() { return ""; },
    };
    handlers[type]({ type, inputType, target: input });
    return input;
  }
  return { hooks, calls, view, editRoot };
}

test("copied work roots remove only surrounding quotes, whitespace and direction markers", () => {
  const { hooks } = createHarness();
  const clean = hooks.normalizeRootInput;
  const root = "U:\\Gun x Sword";
  for (const mark of ["\u061c", "\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff"]) {
    assert.equal(clean(` ${mark}" ${mark}${root}${mark} "${mark}\t`), root);
  }
  assert.equal(clean(`"\\\\server\\shared folder\\作品"`), "\\\\server\\shared folder\\作品");
  assert.equal(clean("U:\\作品\u202a 内部'引号'"), "U:\\作品\u202a 内部'引号'");
  assert.equal(clean("'U:\\Gun x Sword'"), "'U:\\Gun x Sword'");
  assert.equal(clean('"U:\\Gun x Sword'), '"U:\\Gun x Sword');
  assert.equal(clean('\u202a "\u202c \ufeff" \u2069'), "");
});

test("pasted path is cleaned in the input and sent as an absolute root for preview", async () => {
  const { hooks, editRoot, calls, view } = createHarness();
  const input = editRoot('\u202a"U:\\Gun x Sword"\u202c');
  assert.equal(input.value, "U:\\Gun x Sword");
  assert.equal(hooks.state.root, input.value);
  await hooks.previewPlan();
  assert.equal(calls[0].body.root, "U:\\Gun x Sword");
  assert.match(view.innerHTML, /value="U:\\Gun x Sword"/);
  assert.doesNotMatch(view.innerHTML, /\u202a|\u202c/);
});

test("typing spaces remains possible and change commits a clean root", () => {
  const { editRoot, hooks } = createHarness();
  const input = editRoot("U:\\Gun ", "input", "insertText");
  assert.equal(input.value, "U:\\Gun ");
  assert.equal(hooks.state.root, "U:\\Gun");
  assert.equal(editRoot(" U:\\Gun x Sword ", "change", "").value, "U:\\Gun x Sword");
});

test("equivalent copied root preserves the preview while a different root clears stale state", () => {
  const { hooks, editRoot } = createHarness();
  hooks.state.root = "U:\\Existing";
  const plan = { root: hooks.state.root };
  hooks.state.plan = plan;
  hooks.state.routeOverrides = { keep: "existing target" };
  editRoot('\u202a"U:\\Existing"\u202c');
  assert.equal(hooks.state.plan, plan);
  assert.equal(hooks.state.routeOverrides.keep, "existing target");
  editRoot('\u202a"U:\\Gun x Sword"\u202c');
  assert.equal(hooks.state.plan, null);
  assert.deepEqual(Object.keys(hooks.state.routeOverrides), []);
  assert.equal(hooks.state.dirty, true);
  assert.equal(hooks.planCanExecute(), false);
});

test("resource picker normalizes copied paths but still rejects roots outside the allowlist", () => {
  const { hooks, view } = createHarness();
  hooks.selectResourcePickerPath('\u202a"U:\\Gun x Sword"\u202c');
  assert.equal(hooks.state.root, "U:\\Gun x Sword");
  assert.match(view.innerHTML, /value="U:\\Gun x Sword"/);
  hooks.selectResourcePickerPath('\u202a"E:\\Unexpected"\u202c');
  assert.equal(hooks.state.root, "U:\\Gun x Sword");
  assert.match(hooks.state.resourcePicker.error, /允许范围/);
});

test("configured roots and retained state are normalized before preview", async () => {
  const { hooks, calls } = createHarness('\u202a"U:\\Gun x Sword"\u202c');
  await hooks.loadConfigAndPreview(true);
  assert.equal(calls[1].body.root, "U:\\Gun x Sword");
  assert.equal(hooks.state.root, "U:\\Gun x Sword");
  hooks.state.root = '\u202a"U:\\Retained work"\u202c';
  await hooks.previewPlan();
  assert.equal(calls.at(-1).body.root, "U:\\Retained work");
  assert.equal(hooks.state.root, "U:\\Retained work");
});

test("marker-only input clears the old root and cannot trigger a preview request", async () => {
  const { hooks, editRoot, calls } = createHarness();
  hooks.state.root = "U:\\Existing";
  hooks.state.plan = { root: hooks.state.root };
  const input = editRoot('\u202a "\u202c \ufeff" \u2069');
  assert.equal(input.value, "");
  assert.equal(hooks.state.root, "");
  assert.equal(hooks.state.plan, null);
  await hooks.previewPlan();
  assert.equal(calls.length, 0);
  assert.match(hooks.state.notice, /请先输入作品根目录/);
});
