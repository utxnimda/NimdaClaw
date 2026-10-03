"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "../frontend/index.js");

function successfulResponse(data) {
  return Promise.resolve({ res: { ok: true, status: 200 }, data });
}

for (const mode of ["normal", "landing", "repair"]) {
  test(`${mode} partial apply preserves recovery details and invalidates every executable retry`, async () => {
    const calls = [];
    const root = "S:\\Recovery Work";
    const planId = "aaaaaaaaaaaaaaaa";
    const specialPlanId = "bbbbbbbbbbbbbbbb";
    let hooks;
    const view = {
      innerHTML: "", addEventListener() {}, removeEventListener() {},
      querySelector() { return null; }, querySelectorAll() { return []; }, contains() { return true; },
    };
    const document = {
      getElementById(id) { return id === "media-directory-organizer-view" ? view : null; },
      querySelector() { return null; },
    };
    const window = {
      document, confirm() { return true; }, prompt() { return mode === "normal" ? planId : specialPlanId; },
      setTimeout, __NIMDA_ORGANIZER_TEST_HOOK__(value) { hooks = value; },
    };
    window.window = window;
    vm.runInNewContext(fs.readFileSync(FRONTEND, "utf8"), {
      window, document, console, setTimeout, clearTimeout,
    }, { filename: FRONTEND });
    const recovery = {
      ok: false, state: "partial", plan_id: planId, root,
      error: '<img src=x onerror="bad()"> rollback incomplete',
      recovery_required: true, retry_requires_preview: true,
      media: {
        ok: false, rollback_complete: false, moved_file_count: 2, rolled_back_file_count: 1,
        recovery_moves: [{ source: root + "\\incoming\\01.mkv", target: root + "\\Disc\\01.mkv", error: "source occupied" }],
        failed_move: { source: root + "\\incoming\\02.mkv", target: root + "\\Disc\\02.mkv", error: "move failed" },
      },
      catalog: { state: "preserved", rolled_back: false, changes: [], recovery_files: [
        { target: "E:\\Project\\db\\work.yaml", target_existed: true,
          history_path: "E:\\Project\\history\\work-history.yaml", written_sha256: "abc123" },
      ] },
    };
    const endpoint = mode === "normal" ? "/api/media-directory-organizer/apply"
      : mode === "landing" ? "/api/media-directory-organizer/landing/apply"
      : "/api/media-directory-organizer/landing/repair/apply";
    const feature = window.JpTvBrowseFeatureRegistry.features.find((item) => item.id === "media-directory-organizer");
    feature.init({ fetchJson(url, options) {
      calls.push({ url, body: JSON.parse(options.body) });
      return Promise.resolve({ res: { ok: false, status: 409 }, data: recovery });
    } });
    const plan = {
      ok: true, ready: true, root, plan_id: planId, shortcut_plan_id: "cccccccccccccccc",
      summary: { file_count: 2, bytes: 4 }, family_works: ["Recovery Work"], assignments: [], moves: [],
      issues: [], unresolved_files: [], shortcuts: [], shortcut_summary: { conflict_count: 0 },
    };
    Object.assign(hooks.state, {
      root, plan, config: { paths: { allowed_resource_roots: ["S:\\"] } }, dirty: false, busy: "",
    });
    if (mode === "landing") {
      recovery.landing_plan_id = specialPlanId;
      hooks.state.landing = { ready: true, landing_plan_id: specialPlanId, media_plan: plan };
      hooks.state.draftWork = { name: "Recovery Work" };
    }
    if (mode === "repair") {
      recovery.repair_plan_id = specialPlanId;
      hooks.state.shortcutPending = {
        kind: "landing", root, repairRequest: { root },
        repairPreview: { ok: true, ready: true, repair_plan_id: specialPlanId,
          media_move_planned: true, issues: [], repair: { apply_endpoint: endpoint } },
      };
    }
    await (mode === "repair" ? hooks.applyShortcutRepair() : hooks.applyCurrentPlan());
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, endpoint);
    assert.equal(hooks.state.plan, null);
    assert.equal(hooks.state.landing, null);
    assert.equal(hooks.state.shortcutPending, null);
    assert.equal(hooks.state.execution, null);
    assert.equal(hooks.state.dirty, true);
    assert.equal(hooks.planCanExecute(), false);
    assert.deepEqual(JSON.parse(JSON.stringify(hooks.state.recovery)), recovery);
    assert.match(view.innerHTML, /执行未完成：恢复信息/);
    assert.match(view.innerHTML, /work-history\.yaml/);
    assert.match(view.innerHTML, /recovery_moves/);
    assert.match(view.innerHTML, /failed_move/);
    assert.match(view.innerHTML, /incoming/);
    assert.match(view.innerHTML, /Disc/);
    assert.match(view.innerHTML, /&lt;img src=x/);
    assert.doesNotMatch(view.innerHTML, /<img src=x/);
    assert.doesNotMatch(view.innerHTML, /<h2>执行完成/);
    await (mode === "repair" ? hooks.applyShortcutRepair() : hooks.applyCurrentPlan());
    assert.equal(calls.length, 1, "old failed plan was retried without a new preview");
    assert.match(view.innerHTML, /work-history\.yaml/);
  });
}

test("normal organizer reviews shortcuts and retries only shortcuts after media moved", async () => {
  const calls = [];
  const root = "U:\\Example Work";
  const mediaPlanId = "aaaaaaaaaaaaaaaa";
  const shortcutPlanId = "bbbbbbbbbbbbbbbb";
  const retryPlanId = "cccccccccccccccc";
  const workRefs = [{ yaml_source_rel: "[JP][TVInfo][2020].yaml", index_in_file: 3 }];
  const shortcutPath = "E:\\LinkVideo\\[2020]\\Example Work\\BDRip(VCB).lnk";
  const sourceWorkOverrides = { [root + "\\incoming"]: "Example Work" };
  let hooks = null;
  let retryPreviewCount = 0;

  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return true;
    },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() {
      return null;
    },
  };
  const window = {
    document,
    confirm() {
      return true;
    },
    prompt(message) {
      return String(message).includes("重试") ? retryPlanId : mediaPlanId;
    },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) {
      hooks = value;
    },
  };
  window.window = window;

  const sandbox = vm.createContext({
    window,
    document,
    console,
    setTimeout,
    clearTimeout,
  });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  assert.ok(hooks, "frontend test hook was not registered");

  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  assert.ok(feature);

  const featureContext = {
    esc(value) {
      return String(value == null ? "" : value);
    },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/apply") {
        return successfulResponse({
          ok: false,
          state: "shortcut_pending",
          media: { moved_file_count: 1, moved_bytes: 4, cleanup_warnings: [] },
          shortcut_error: "shortcut writer unavailable",
          shortcut_retry: {
            preview_endpoint: "/api/media-directory-organizer/landing/shortcuts/preview",
            apply_endpoint: "/api/media-directory-organizer/landing/shortcuts/apply",
            root,
            work_refs: workRefs,
          },
        });
      }
      if (url === "/api/media-directory-organizer/landing/shortcuts/preview") {
        retryPreviewCount += 1;
        const alreadyComplete = retryPreviewCount > 1;
        return successfulResponse({
          ok: true,
          ready: true,
          root,
          work_refs: workRefs,
          retry_plan_id: retryPlanId,
          shortcut_summary: {
            total_count: 1,
            planned_count: alreadyComplete ? 0 : 1,
            already_exists_count: alreadyComplete ? 1 : 0,
            conflict_count: 0,
          },
          shortcuts: [
            {
              shortcut_path: shortcutPath,
              target_path: root + "\\Example Work_BDRip(VCB)",
              status: alreadyComplete ? "already_exists" : "planned",
            },
          ],
        });
      }
      if (url === "/api/media-directory-organizer/landing/shortcuts/apply") {
        return successfulResponse({ ok: true, state: "complete" });
      }
      throw new Error("unexpected request: " + url);
    },
  };

  feature.init(featureContext);
  Object.assign(hooks.state, {
    config: {
      paths: {
        catalog_root: "E:\\Project\\data",
        allowed_resource_roots: ["U:\\"],
      },
    },
    root,
    plan: {
      ok: true,
      ready: true,
      root,
      plan_id: mediaPlanId,
      summary: { file_count: 1, bytes: 4, assignment_count: 1 },
      family_works: ["Example Work"],
      assignments: [],
      moves: [],
      issues: [],
      unresolved_files: [],
      shortcuts: [
        {
          shortcut_path: shortcutPath,
          target_path: root + "\\Example Work_BDRip(VCB)",
          status: "planned",
        },
      ],
      shortcut_summary: {
        planned_count: 1,
        already_exists_count: 0,
        conflict_count: 0,
      },
      shortcut_plan_id: shortcutPlanId,
      shortcut_scope: { root, work_refs: workRefs },
    },
    sourceWorkOverrides,
    dirty: false,
    busy: "",
    notice: "",
    noticeError: false,
    execution: null,
    landing: null,
    draftWork: null,
    shortcutPending: null,
  });

  hooks.render();
  assert.equal(view.innerHTML.includes(shortcutPath), true);
  assert.match(view.innerHTML, /快捷方式确认 ID/);
  assert.match(view.innerHTML, new RegExp(shortcutPlanId));
  assert.equal(hooks.planCanExecute(), true);

  await hooks.applyCurrentPlan();
  const normalApply = calls.find(
    (call) => call.url === "/api/media-directory-organizer/apply",
  );
  assert.ok(normalApply, "normal apply was not requested");
  assert.equal(normalApply.body.shortcut_plan_id, shortcutPlanId);
  assert.equal(normalApply.body.shortcut_confirmation, shortcutPlanId);
  assert.equal(normalApply.body.acknowledge_shortcuts, true);
  assert.deepEqual(normalApply.body.source_work_overrides, sourceWorkOverrides);
  assert.match(view.innerHTML, /不要再次执行媒体移动/);

  await hooks.previewShortcutRetry();
  assert.equal(retryPreviewCount, 1);
  const firstRetryPreview = calls.filter(
    (call) => call.url === "/api/media-directory-organizer/landing/shortcuts/preview",
  )[0].body;
  assert.equal(firstRetryPreview.root, root);
  assert.deepEqual(firstRetryPreview.work_refs, workRefs);

  await hooks.applyShortcutRetry();
  const retryApply = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/shortcuts/apply",
  );
  assert.ok(retryApply, "shortcut-only apply was not requested");
  assert.equal(retryApply.body.retry_plan_id, retryPlanId);
  assert.equal(retryApply.body.confirmation, retryPlanId);
  assert.equal(retryApply.body.acknowledge_shortcuts, true);
  assert.deepEqual(retryApply.body.work_refs, workRefs);
  assert.equal(Object.hasOwn(retryApply.body, "plan_id"), false);
  assert.equal(Object.hasOwn(retryApply.body, "acknowledge_move"), false);

  await hooks.beginShortcutOnlyPreview();
  assert.equal(retryPreviewCount, 2);
  const manualPreview = calls.filter(
    (call) => call.url === "/api/media-directory-organizer/landing/shortcuts/preview",
  )[1].body;
  assert.deepEqual(Object.keys(manualPreview), ["root"]);
  assert.equal(manualPreview.root, root);
  assert.match(view.innerHTML, /快捷方式检查完成/);
  assert.match(view.innerHTML, /已完整/);
  assert.match(hooks.state.notice, /均已存在/);
});

test("already-organized roots never re-run media apply and use shortcut-only states", async () => {
  const calls = [];
  const root = "U:\\山田くんと7人の魔女";
  const target = root + "\\山田くんと7人の魔女_BDRip(VCBM)";
  const shortcutPath = "E:\\LinkVideo\\[2015]\\山田くんと7人の魔女\\BDRip(VCB).lnk";
  const workRefs = [{ yaml_source_rel: "[JP][TVInfo][2015].yaml", index_in_file: 7 }];
  let hooks = null;
  let mode = "pending";

  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return true;
    },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() {
      return null;
    },
  };
  const window = {
    document,
    confirm() {
      throw new Error("already-organized media apply must never ask for confirmation");
    },
    prompt() {
      throw new Error("already-organized media apply must never ask for a plan id");
    },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) {
      hooks = value;
    },
  };
  window.window = window;

  const sandbox = vm.createContext({
    window,
    document,
    console,
    setTimeout,
    clearTimeout,
  });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  assert.ok(hooks, "frontend test hook was not registered");

  function organizerPlan() {
    const complete = mode === "complete";
    const blocked = mode === "blocked";
    const issues = blocked
      ? [
          {
            code: "shortcut-catalog-path-missing",
            path: root,
            message: "数据库 path 没有指向当前作品根目录",
          },
        ]
      : complete
      ? [
          {
            code: "shortcut-scope-empty",
            path: root,
            message: "旧版空范围提示不应再显示",
          },
        ]
      : [];
    return {
      ready: false,
      root,
      plan_id: "settled-media-plan",
      media_state: "already_organized",
      shortcut_state: mode,
      summary: { file_count: 0, assignment_count: 0, bytes: 0 },
      family_works: ["山田くんと7人の魔女"],
      assignments: [],
      moves: [],
      unresolved_files: [],
      issues,
      shortcut_issues: issues,
      shortcuts: [
        {
          shortcut_path: shortcutPath,
          target_path: target,
          status: complete ? "already_exists" : "planned",
        },
      ],
      shortcut_summary: {
        total_count: 1,
        planned_count: complete ? 0 : blocked ? 0 : 1,
        already_exists_count: complete ? 1 : 0,
        conflict_count: 0,
      },
      shortcut_plan_id: "settled-shortcut-plan",
      shortcut_scope: { root, work_refs: workRefs },
    };
  }

  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  feature.init({
    esc(value) {
      return String(value == null ? "" : value);
    },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/preview") {
        return successfulResponse({ ok: true, plan: organizerPlan() });
      }
      if (url === "/api/media-directory-organizer/landing/shortcuts/preview") {
        return successfulResponse({
          ok: true,
          ready: true,
          root,
          work_refs: workRefs,
          retry_plan_id: "settled-retry-plan",
          shortcut_summary: {
            total_count: 1,
            planned_count: 1,
            already_exists_count: 0,
            conflict_count: 0,
          },
          shortcuts: [
            { shortcut_path: shortcutPath, target_path: target, status: "planned" },
          ],
          issues: [],
        });
      }
      throw new Error("unexpected request: " + url);
    },
  });
  Object.assign(hooks.state, {
    config: {
      paths: { catalog_root: "E:\\Project\\data", allowed_resource_roots: ["U:\\"] },
    },
    root,
    dirty: false,
    busy: "",
    shortcutPending: null,
  });

  await hooks.previewPlan();
  assert.equal(hooks.planCanExecute(), false);
  assert.equal(hooks.state.noticeError, false);
  assert.match(hooks.state.notice, /媒体已整理，仅快捷方式待补建/);
  assert.match(view.innerHTML, /仅待快捷方式/);
  assert.match(view.innerHTML, /检查并补建快捷方式/);
  assert.doesNotMatch(view.innerHTML, />执行移动并创建快捷方式<\/button>/);

  const callCountBeforeRejectedApply = calls.length;
  await hooks.applyCurrentPlan();
  assert.equal(calls.length, callCountBeforeRejectedApply);
  assert.equal(
    calls.some((call) => call.url === "/api/media-directory-organizer/apply"),
    false,
  );

  await hooks.beginShortcutOnlyPreview();
  const retryPreview = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/shortcuts/preview",
  );
  assert.ok(retryPreview);
  assert.equal(retryPreview.body.root, root);
  assert.deepEqual(retryPreview.body.work_refs, workRefs);
  assert.equal(hooks.state.shortcutPending.kind, "organizer");
  assert.match(view.innerHTML, /媒体归类已完成，快捷方式待补建/);
  assert.match(view.innerHTML, /不要重新执行媒体移动/);

  hooks.cancelShortcutRetry();
  mode = "complete";
  await hooks.previewPlan();
  assert.equal(hooks.planCanExecute(), false);
  assert.equal(hooks.state.noticeError, false);
  assert.equal(hooks.state.notice, "媒体归类和快捷方式均已完成。");
  assert.match(view.innerHTML, /媒体归类和快捷方式均已完成/);
  assert.match(view.innerHTML, /已完整/);
  assert.doesNotMatch(view.innerHTML, /shortcut-scope-empty/);
  assert.doesNotMatch(view.innerHTML, /旧版空范围提示不应再显示/);

  mode = "blocked";
  await hooks.previewPlan();
  assert.equal(hooks.planCanExecute(), false);
  assert.equal(hooks.state.noticeError, true);
  assert.match(hooks.state.notice, /数据库 path 没有指向当前作品根目录/);
  assert.match(view.innerHTML, /数据库 path 没有指向当前作品根目录/);
  assert.match(view.innerHTML, /需修复/);
  assert.equal(
    calls.some((call) => call.url === "/api/media-directory-organizer/apply"),
    false,
  );
});

test("unresolved works use one source-directory choice before per-file mode", async () => {
  const calls = [];
  const root = "U:\\Combined Work";
  const sourceDir = root + "\\[BDRip][JSUM] release";
  const firstFile = sourceDir + "\\[Unknown Title][01].mkv";
  const secondFile = sourceDir + "\\[Unknown Title][02].mkv";
  let hooks = null;
  let collectionTabClicks = 0;
  let collectionListClicks = 0;

  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return true;
    },
  };
  const document = {
    getElementById(id) {
      if (id === "media-directory-organizer-view") return view;
      if (id === "tab-collection-detail") {
        return {
          click() {
            collectionTabClicks += 1;
          },
        };
      }
      return null;
    },
    querySelector(selector) {
      if (selector === '[data-collection-detail-subtab="list"]') {
        return {
          click() {
            collectionListClicks += 1;
          },
        };
      }
      return null;
    },
  };
  const window = {
    document,
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) {
      hooks = value;
    },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  assert.ok(hooks);

  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  feature.init({
    esc(value) {
      return String(value == null ? "" : value);
    },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      assert.equal(url, "/api/media-directory-organizer/preview");
      return successfulResponse({
        ok: true,
        plan: {
          ready: false,
          root,
          plan_id: "",
          summary: {},
          family_works: ["Beta Work"],
          assignments: [],
          moves: [],
          unresolved_files: [],
          issues: [{ code: "group-unresolved", message: "still reviewing" }],
        },
      });
    },
  });

  Object.assign(hooks.state, {
    config: { paths: { catalog_root: "E:\\Project\\data", allowed_resource_roots: ["U:\\"] } },
    root,
    plan: {
      ready: false,
      root,
      plan_id: "",
      summary: { unresolved_file_count: 2 },
      family_works: ["Alpha Work", "Beta Work"],
      assignments: [],
      moves: [],
      issues: [],
      unresolved_files: [
        {
          source: firstFile,
          source_dir: sourceDir,
          source_relpath: "[Unknown Title][01].mkv",
          reason: "ambiguous",
          candidates: ["Alpha Work", "Beta Work"],
          unmatched_work_name_hints: ["Unknown Title", "Unregistered Special"],
        },
        {
          source: secondFile,
          source_dir: sourceDir,
          source_relpath: "[Unknown Title][02].mkv",
          reason: "ambiguous",
          candidates: ["Alpha Work", "Beta Work"],
          unmatched_work_name_hints: ["Unknown Title"],
        },
      ],
    },
    sourceWorkOverrides: Object.create(null),
    fileWorkOverrides: Object.create(null),
    multipleWorkSources: Object.create(null),
    busy: "",
    dirty: false,
  });

  hooks.render();
  assert.match(view.innerHTML, /目录作品结构/);
  assert.match(view.innerHTML, /整个目录归为：Beta Work/);
  assert.equal(view.innerHTML.includes("data-file-source"), false);

  assert.equal(
    hooks.selectSourceWorkMode(sourceDir, "__organizer_multiple_works__"),
    "multiple",
  );
  assert.equal(hooks.state.multipleWorkSources[sourceDir], true);
  assert.equal(view.innerHTML.includes("data-file-source"), true);
  assert.match(view.innerHTML, /存在尚未入库的作品候选/);
  assert.match(view.innerHTML, /Unknown Title、Unregistered Special/);
  assert.match(view.innerHTML, /前往作品数据补录/);

  hooks.guideUnresolvedCatalog(sourceDir);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(collectionTabClicks, 1);
  assert.equal(collectionListClicks, 1);
  assert.match(hooks.state.notice, /path 填写当前作品根目录/);
  assert.match(hooks.state.notice, /保存后返回“目录整理”重新预览/);

  hooks.state.fileWorkOverrides[firstFile] = "Alpha Work";
  assert.equal(hooks.selectSourceWorkMode(sourceDir, "Beta Work"), "single");
  assert.equal(hooks.state.sourceWorkOverrides[sourceDir], "Beta Work");
  assert.equal(Object.hasOwn(hooks.state.fileWorkOverrides, firstFile), false);
  assert.equal(Object.hasOwn(hooks.state.multipleWorkSources, sourceDir), false);

  await hooks.previewPlan();
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].body.source_work_overrides, { [sourceDir]: "Beta Work" });
  assert.deepEqual(calls[0].body.file_work_overrides, {});
});

test("missing database shortcut preview preserves edits, explains issues, and uses repair-only contract", async () => {
  const calls = [];
  const root = "U:\\Fate／Zero";
  const repairPlanId = "dddddddddddddddd";
  const shortcutPath = "E:\\LinkVideo\\[2011]\\Fate／Zero\\BDRip(VCB).lnk";
  const candidates = [
    {
      label: "Fate／Zero 01-12",
      draft_work: {
        name: "Fate／Zero",
        date: { start: "2011-10-02", end: "2011-12-25" },
        domain: "animation",
        country: "japan",
        release_type: "tv",
        path: root,
        presses: [
          {
            press_format: "BDRip",
            press_group: "VCB",
            press_path: "Fate／Zero_BDRip(VCBM)_01-12",
          },
        ],
      },
      catalog_ref: {
        yaml_source_rel: "[JP][TVInfo][2011].yaml",
        index_in_file: 2,
        work_name: "Fate／Zero",
      },
    },
    {
      label: "Fate／Zero 13-25",
      draft_work: {
        name: "Fate／Zero",
        date: { start: "2012-04-08", end: "2012-06-24" },
        domain: "animation",
        country: "japan",
        release_type: "tv",
        path: root,
        presses: [
          {
            press_format: "BDRip",
            press_group: "----",
            press_path: "",
          },
        ],
      },
      catalog_ref: {
        yaml_source_rel: "[JP][TVInfo][2012].yaml",
        index_in_file: 1,
        work_name: "Fate／Zero",
      },
    },
  ];
  let hooks = null;
  const listeners = {};

  const view = {
    innerHTML: "",
    addEventListener(type, handler) {
      listeners[type] = handler;
    },
    removeEventListener(type) {
      delete listeners[type];
    },
    querySelector() {
      return null;
    },
    contains() {
      return true;
    },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() {
      return null;
    },
  };
  const window = {
    document,
    confirm() {
      return true;
    },
    prompt(message) {
      return String(message).includes("修复") ? repairPlanId : "";
    },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) {
      hooks = value;
    },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });

  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  assert.ok(feature);
  feature.init({
    esc(value) {
      return String(value == null ? "" : value);
    },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/landing/shortcuts/preview") {
        return successfulResponse({
          ok: true,
          state: "shortcut_retry_preview",
          root,
          ready: false,
          repair_required: true,
          shortcut_plan_id: "eeeeeeeeeeeeeeee",
          shortcut_summary: {
            total_count: 0,
            planned_count: 0,
            already_exists_count: 0,
            conflict_count: 0,
            missing_catalog_path_count: 1,
          },
          shortcuts: [],
          issues: [
            {
              code: "shortcut-catalog-path-missing",
              message: "数据库中没有权威 path 精确等于该作品根目录的作品",
              path: root,
            },
          ],
          repair: {
            preview_endpoint: "/api/media-directory-organizer/landing/repair/preview",
            apply_endpoint: "/api/media-directory-organizer/landing/repair/apply",
            reason_codes: ["shortcut-catalog-path-missing"],
            independent_append_allowed: true,
            draft_work: candidates[0].draft_work,
            candidates,
          },
        });
      }
      if (url === "/api/media-directory-organizer/landing/repair/preview") {
        return successfulResponse({
          ok: true,
          state: "catalog_shortcut_repair_preview",
          repair_required: true,
          root,
          draft_work: body.draft_work,
          catalog_ref: body.catalog_ref,
          catalog_change: { action: "update", target: "[JP][TVInfo][2012].yaml" },
          shortcuts: [
            {
              shortcut_path: shortcutPath,
              target_path: root + "\\Fate／Zero_BDRip(Jsum)_13-25",
              status: "planned",
            },
          ],
          shortcut_summary: {
            total_count: 1,
            planned_count: 1,
            already_exists_count: 0,
            conflict_count: 0,
          },
          issues: [],
          ready: true,
          repair_plan_id: repairPlanId,
          repair: {
            preview_endpoint: "/api/media-directory-organizer/landing/repair/preview",
            apply_endpoint: "/api/media-directory-organizer/landing/repair/apply",
          },
        });
      }
      if (url === "/api/media-directory-organizer/landing/repair/apply") {
        return successfulResponse({ ok: true, state: "complete" });
      }
      throw new Error("unexpected request: " + url);
    },
  });

  const originalPlan = {
    ready: false,
    registration_required: false,
    plan_id: "ffffffffffffffff",
    assignments: [],
    moves: [],
    issues: [],
    unresolved_files: [],
  };
  const originalDraft = {
    name: "人工填写仍需保留",
    date: { start: "2011-10-02", end: "" },
    presses: [],
  };
  const routeOverrides = { routeA: "人工目标" };
  const fileWorkOverrides = { "U:\\source\\episode.mkv": "Fate／Zero" };
  const sourcePressOverrides = { "U:\\source": { press_group: "VCB" } };
  Object.assign(hooks.state, {
    config: {
      paths: { catalog_root: "E:\\Project\\data", allowed_resource_roots: ["U:\\"] },
      detection: { format_markers: { BDRip: ["BD"] } },
      group_registry: {
        options: [
          { code: "VCB", label: "VCB" },
          { code: "JSUM", label: "JSUM" },
        ],
      },
    },
    root,
    plan: originalPlan,
    draftWork: originalDraft,
    routeOverrides,
    fileWorkOverrides,
    sourcePressOverrides,
    dirty: true,
    busy: "",
    notice: "",
    noticeError: false,
    execution: { moved_file_count: 9 },
    landing: null,
    shortcutPending: null,
  });

  await hooks.beginShortcutOnlyPreview();
  assert.equal(hooks.state.plan, originalPlan, "shortcut preview discarded the media plan");
  assert.equal(hooks.state.draftWork, originalDraft, "shortcut preview discarded the manual draft");
  assert.equal(hooks.state.routeOverrides, routeOverrides);
  assert.equal(hooks.state.fileWorkOverrides, fileWorkOverrides);
  assert.equal(hooks.state.sourcePressOverrides, sourcePressOverrides);
  assert.match(view.innerHTML, /数据库中没有权威 path 精确等于该作品根目录的作品/);
  assert.match(view.innerHTML, /缺少数据库作品路径/);
  assert.match(view.innerHTML, /选择并填写：Fate／Zero 01-12/);
  assert.match(view.innerHTML, /选择并填写：Fate／Zero 13-25/);
  assert.match(view.innerHTML, /Fate／Zero_BDRip\(VCBM\)_01-12/);
  assert.match(view.innerHTML, /请继续核对作品信息和 press_path/);
  assert.match(view.innerHTML, /明确新增为独立作品/);
  assert.doesNotMatch(hooks.state.notice, /仍有冲突/);

  hooks.selectShortcutRepairIndependent();
  assert.equal(hooks.state.shortcutPending.repairCatalogIntent, "append_independent");
  assert.equal(hooks.state.shortcutPending.repairCatalogRef, null);
  assert.match(hooks.state.notice, /规范化同名重复/);

  hooks.selectShortcutRepairCandidate(1);
  assert.equal(hooks.state.shortcutPending.repairCatalogIntent, "");
  assert.match(view.innerHTML, /核对数据库补录信息/);
  assert.match(view.innerHTML, /数据库 press_path \/ 已整理目标/);
  assert.match(view.innerHTML, /<option value="" selected>—（无压制组）<\/option>/);
  assert.match(view.innerHTML, /\[JP\]\[TVInfo\]\[2012\]\.yaml#1/);
  assert.equal(hooks.state.shortcutPending.repairDraft.presses[0].press_path, "");
  await hooks.previewShortcutRepair(-1);
  assert.equal(
    calls.filter((call) => call.url === "/api/media-directory-organizer/landing/repair/preview").length,
    0,
  );
  assert.match(hooks.state.notice, /缺少 press_path/);
  listeners.input({
    target: {
      value: "Fate／Zero_BDRip(Jsum)_13-25",
      matches(selector) {
        return selector === "[data-repair-press]";
      },
      getAttribute(name) {
        if (name === "data-repair-press") return "press_path";
        if (name === "data-repair-press-index") return "0";
        return "";
      },
    },
  });
  assert.equal(
    hooks.state.shortcutPending.repairDraft.presses[0].press_path,
    "Fate／Zero_BDRip(Jsum)_13-25",
  );
  await hooks.previewShortcutRepair(-1);
  const repairPreview = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/repair/preview",
  );
  assert.ok(repairPreview);
  assert.equal(repairPreview.body.root, root);
  assert.equal(repairPreview.body.draft_work.presses[0].press_path, "Fate／Zero_BDRip(Jsum)_13-25");
  assert.equal(repairPreview.body.draft_work.presses[0].press_group, "");
  assert.equal(repairPreview.body.catalog_ref.yaml_source_rel, "[JP][TVInfo][2012].yaml");
  assert.equal(Object.hasOwn(repairPreview.body, "acknowledge_move"), false);
  assert.match(view.innerHTML, new RegExp(repairPlanId));
  assert.match(view.innerHTML, /确认补录数据库并创建快捷方式/);

  await hooks.applyShortcutRepair();
  const repairApply = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/repair/apply",
  );
  assert.ok(repairApply);
  assert.equal(repairApply.body.repair_plan_id, repairPlanId);
  assert.equal(repairApply.body.confirmation, repairPlanId);
  assert.equal(repairApply.body.acknowledge_catalog_write, true);
  assert.equal(repairApply.body.acknowledge_shortcuts, true);
  assert.equal(Object.hasOwn(repairApply.body, "acknowledge_move"), false);
  assert.equal(Object.hasOwn(repairApply.body, "plan_id"), false);
  assert.match(hooks.state.notice, /媒体文件没有移动/);
});

test("registration-required shortcut action uses repair preview and landing action is near summary", async () => {
  const calls = [];
  const root = "U:\\New Work";
  const repairPlanId = "1111111111111111";
  let hooks = null;
  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    contains() {
      return true;
    },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() {
      return null;
    },
  };
  const window = {
    document,
    confirm() {
      return false;
    },
    prompt() {
      return null;
    },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) {
      hooks = value;
    },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  feature.init({
    esc(value) {
      return String(value == null ? "" : value);
    },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/landing/repair/preview") {
        return successfulResponse({
          ok: true,
          ready: true,
          repair_plan_id: repairPlanId,
          repair: {
            preview_endpoint: url,
            apply_endpoint: "/api/media-directory-organizer/landing/repair/apply",
          },
          catalog_change: { action: "append" },
          shortcut_summary: { total_count: 1, planned_count: 1, conflict_count: 0 },
          shortcuts: [{ shortcut_path: "E:\\New Work.lnk", target_path: root, status: "planned" }],
          issues: [],
        });
      }
      throw new Error("unexpected request: " + url);
    },
  });

  const draft = {
    name: "New Work",
    date: { start: "2026-01-01", end: "" },
    domain: "animation",
    country: "japan",
    release_type: "tv",
    path: root,
    presses: [{ press_format: "BDRip", press_group: "VCB", press_path: "New Work_BDRip(VCBM)" }],
  };
  const registrationPlan = {
    ready: false,
    registration_required: true,
    registration: { draft },
    repair_required: true,
    repair: {
      preview_endpoint: "/api/media-directory-organizer/landing/repair/preview",
      apply_endpoint: "/api/media-directory-organizer/landing/repair/apply",
      candidates: [{ label: "New Work", draft_work: draft, catalog_ref: null }],
    },
    plan_id: "",
    assignments: [],
    moves: [],
    issues: [{ code: "catalog-work-not-found", message: "缺少数据库记录" }],
    unresolved_files: [],
  };
  const routeOverrides = { preserved: "yes" };
  Object.assign(hooks.state, {
    config: { paths: { catalog_root: "E:\\Project\\data", allowed_resource_roots: ["U:\\"] } },
    root,
    plan: registrationPlan,
    draftWork: draft,
    routeOverrides,
    fileWorkOverrides: { file: "work" },
    sourcePressOverrides: { source: { press_group: "VCB" } },
    dirty: true,
    busy: "",
    notice: "",
    noticeError: false,
    landing: null,
    shortcutPending: null,
  });
  hooks.render();
  assert.match(view.innerHTML, /检查 DB 候选 \/ 补录快捷方式（不移动）/);

  await hooks.beginShortcutOnlyPreview();
  assert.equal(
    calls.filter((call) => call.url === "/api/media-directory-organizer/landing/shortcuts/preview").length,
    0,
  );
  assert.equal(
    calls.filter((call) => call.url === "/api/media-directory-organizer/landing/repair/preview").length,
    0,
  );
  assert.equal(hooks.state.plan, registrationPlan);
  assert.equal(hooks.state.draftWork, draft);
  assert.equal(hooks.state.routeOverrides, routeOverrides);
  assert.match(view.innerHTML, /选择并核对补录信息/);
  hooks.selectShortcutRepairCandidate(0);
  assert.match(view.innerHTML, /核对数据库补录信息/);
  await hooks.previewShortcutRepair(-1);
  assert.equal(
    calls.filter((call) => call.url === "/api/media-directory-organizer/landing/repair/preview").length,
    1,
  );

  hooks.state.shortcutPending = null;
  hooks.state.landing = {
    ready: true,
    landing_plan_id: "2222222222222222",
    catalog_change: { action: "append", target: "[JP][TVInfo][2026].yaml" },
    shortcut_summary: { planned_count: 1, conflict_count: 0 },
    shortcuts: [],
  };
  hooks.state.plan = {
    ready: true,
    plan_id: "3333333333333333",
    summary: {},
    assignments: [],
    moves: [],
    issues: [],
    unresolved_files: [],
  };
  hooks.state.dirty = false;
  hooks.render();
  const summaryStart = view.innerHTML.indexOf("organizer-landing-summary");
  const nearbyApply = view.innerHTML.indexOf('data-organizer-action="apply"', summaryStart);
  const longPlanStart = view.innerHTML.indexOf('class="organizer-plan"', summaryStart);
  assert.ok(summaryStart >= 0);
  assert.ok(nearbyApply > summaryStart, "landing summary has no nearby apply action");
  assert.ok(nearbyApply < longPlanStart, "landing apply action is still after the long media plan");
});

test("partitioned multi-work registration keeps draft_works through preview and apply", async () => {
  const calls = [];
  const confirmations = [];
  const root = "U:\\Zombie Land Saga";
  const landingPlanId = "1234567890abcdef";
  const mediaPlanId = "fedcba0987654321";
  const workRefs = [
    {
      yaml_source_rel: "[JP][TVInfo][2018].yaml",
      index_in_file: 0,
      work_name: "Zombie Land Saga",
      presses: [{ press_key: "0:main::BDRip:JSUM" }, { press_key: "1:main::BDRip:VCBM" }],
    },
    {
      yaml_source_rel: "[JP][TVInfo][2021].yaml",
      index_in_file: 0,
      work_name: "Zombie Land Saga Revenge",
      presses: [{ press_key: "0:main::BDRip:JSUM" }, { press_key: "1:main::BDRip:VCB" }],
    },
  ];
  const drafts = [
    {
      name: "Zombie Land Saga",
      date: { start: "2018-10-04", end: "" },
      domain: "animation",
      country: "japan",
      release_type: "tv",
      path: root,
      presses: [
        { source_names: ["base-jsum"], press_format: "BDRip", press_group: "JSUM", press_path: "Zombie Land Saga_BDRip(Jsum)" },
        { source_names: ["base-vcbm"], press_format: "BDRip", press_group: "VCBM", press_path: "Zombie Land Saga_BDRip(VCBM)" },
      ],
    },
    {
      name: "Zombie Land Saga Revenge",
      date: { start: "2021-04-08", end: "" },
      domain: "animation",
      country: "japan",
      release_type: "tv",
      path: root,
      presses: [
        { source_names: ["revenge-jsum"], press_format: "BDRip", press_group: "JSUM", press_path: "Zombie Land Saga Revenge_BDRip(Jsum)" },
        { source_names: ["revenge-vcb"], press_format: "BDRip", press_group: "VCB", press_path: "Zombie Land Saga Revenge_BDRip(VCB)" },
      ],
    },
  ];
  let hooks = null;
  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    contains() { return true; },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() { return null; },
  };
  const window = {
    document,
    confirm(message) {
      confirmations.push(String(message));
      return true;
    },
    prompt() { return landingPlanId; },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) { hooks = value; },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  feature.init({
    esc(value) { return String(value == null ? "" : value); },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/landing/preview") {
        return successfulResponse({
          ok: true,
          ready: true,
          root,
          state: "multi_work_landing_preview",
          landing_plan_id: landingPlanId,
          draft_works: body.draft_works,
          catalog_changes: [
            { action: "append", target: "[JP][TVInfo][2018].yaml" },
            { action: "append", target: "[JP][TVInfo][2021].yaml" },
          ],
          organizer_plan: {
            ready: true,
            plan_id: mediaPlanId,
            summary: { file_count: 4, bytes: 16, assignment_count: 4 },
            assignments: [],
            moves: [],
            issues: [],
            unresolved_files: [],
          },
          shortcuts: [],
          shortcut_summary: { total_count: 4, planned_count: 4, conflict_count: 0 },
        });
      }
      if (url === "/api/media-directory-organizer/landing/apply") {
        return successfulResponse({
          ok: false,
          state: "shortcut_pending",
          media: { moved_file_count: 4, moved_bytes: 16 },
          shortcut_error: "simulated shortcut failure",
          shortcut_retry: {
            preview_endpoint: "/api/media-directory-organizer/landing/shortcuts/preview",
            apply_endpoint: "/api/media-directory-organizer/landing/shortcuts/apply",
            root,
            work_refs: workRefs,
          },
        });
      }
      if (url === "/api/media-directory-organizer/landing/shortcuts/preview") {
        return successfulResponse({
          ok: true,
          ready: true,
          root,
          work_refs: body.work_refs,
          retry_plan_id: "aaaaaaaaaaaaaaaa",
          shortcut_plan_id: "aaaaaaaaaaaaaaaa",
          shortcuts: [],
          shortcut_summary: { total_count: 4, planned_count: 4, conflict_count: 0 },
          issues: [],
        });
      }
      throw new Error("unexpected request: " + url);
    },
  });
  Object.assign(hooks.state, {
    config: {
      paths: { catalog_root: "E:\\Project\\data", allowed_resource_roots: ["U:\\"] },
      group_registry: { options: [{ code: "JSUM" }, { code: "VCBM" }, { code: "VCB" }] },
    },
    root,
    plan: {
      ready: false,
      registration_required: true,
      registration: { work_drafts: drafts },
      assignments: [],
      moves: [],
      issues: [{ code: "catalog-work-not-found", message: "missing" }],
      unresolved_files: [],
    },
    draftWork: null,
    draftWorks: drafts,
    landing: null,
    shortcutPending: null,
    dirty: false,
    busy: "",
    notice: "",
    noticeError: false,
  });

  hooks.render();
  assert.match(view.innerHTML, /Zombie Land Saga/);
  assert.match(view.innerHTML, /Zombie Land Saga Revenge/);
  assert.match(view.innerHTML, /data-work-index="0"/);
  assert.match(view.innerHTML, /data-work-index="1"/);
  assert.match(view.innerHTML, /多作品请使用完整落地/);

  await hooks.previewLandingPlan();
  const previewCall = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/preview",
  );
  assert.ok(previewCall);
  assert.equal(Object.hasOwn(previewCall.body, "draft_work"), false);
  assert.equal(previewCall.body.draft_works.length, 2);

  await hooks.applyCurrentLanding();
  const applyCall = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/apply",
  );
  assert.ok(applyCall);
  assert.equal(Object.hasOwn(applyCall.body, "draft_work"), false);
  assert.equal(applyCall.body.draft_works.length, 2);
  assert.equal(applyCall.body.acknowledge_catalog_write, true);
  assert.equal(applyCall.body.acknowledge_move, true);
  assert.equal(applyCall.body.acknowledge_shortcuts, true);
  assert.equal(hooks.state.draftWork, null);
  assert.equal(hooks.state.draftWorks.length, 0);
  assert.match(confirmations[0], /Zombie Land Saga \/ Zombie Land Saga Revenge/);
  assert.match(confirmations[0], /append \/ append/);
  assert.match(confirmations[0], /2018.*2021/s);

  await hooks.previewShortcutRetry();
  const retryCall = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/shortcuts/preview",
  );
  assert.ok(retryCall);
  assert.deepEqual(retryCall.body.work_refs, workRefs);
});

test("catalog repair from an active organizer plan carries media overrides and retries only shortcuts", async () => {
  const calls = [];
  const root = "U:\\Planned Work";
  const sourceDir = root + "\\incoming";
  const targetRelpath = "Planned Work_BDRip(VCB)";
  const targetDir = root + "\\" + targetRelpath;
  const repairPlanId = "4444444444444444";
  const workRefs = [{ yaml_source_rel: "[JP][TVInfo][2024].yaml", index_in_file: 4 }];
  let hooks = null;

  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return true;
    },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() {
      return null;
    },
  };
  const window = {
    document,
    confirm() {
      return true;
    },
    prompt() {
      return repairPlanId;
    },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) {
      hooks = value;
    },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  feature.init({
    esc(value) {
      return String(value == null ? "" : value);
    },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/landing/repair/preview") {
        return successfulResponse({
          ok: true,
          ready: true,
          media_move_planned: true,
          repair_plan_id: repairPlanId,
          repair: {
            preview_endpoint: url,
            apply_endpoint: "/api/media-directory-organizer/landing/repair/apply",
          },
          catalog_change: { action: "update" },
          shortcut_summary: {
            total_count: 1,
            planned_count: 1,
            already_exists_count: 0,
            conflict_count: 0,
          },
          shortcuts: [
            {
              shortcut_path: "E:\\LinkVideo\\Planned Work.lnk",
              target_path: targetDir,
              status: "planned",
            },
          ],
          issues: [],
        });
      }
      if (url === "/api/media-directory-organizer/landing/repair/apply") {
        return successfulResponse({
          ok: false,
          state: "shortcut_pending",
          media: { moved_file_count: 1, moved_bytes: 10, cleanup_warnings: [] },
          shortcut_error: "shortcut writer unavailable",
          shortcut_retry: {
            preview_endpoint: "/api/media-directory-organizer/landing/shortcuts/preview",
            apply_endpoint: "/api/media-directory-organizer/landing/shortcuts/apply",
            root,
            work_refs: workRefs,
          },
        });
      }
      throw new Error("unexpected request: " + url);
    },
  });

  const draft = {
    name: "Planned Work",
    date: { start: "2024-01-01", end: "" },
    domain: "animation",
    country: "japan",
    release_type: "tv",
    path: root,
    presses: [{ press_format: "BDRip", press_group: "VCB", press_path: targetRelpath }],
  };
  const routeOverrides = { [sourceDir]: targetRelpath, routeA: "route-target" };
  const fileWorkOverrides = { [sourceDir + "\\01.mkv"]: "Planned Work" };
  const sourceWorkOverrides = { [sourceDir]: "Planned Work" };
  const sourcePressOverrides = {
    [sourceDir]: { press_format: "BDRip", press_group: "VCB" },
  };
  Object.assign(hooks.state, {
    config: {
      paths: { catalog_root: "E:\\Project\\data", allowed_resource_roots: ["U:\\"] },
      detection: { format_markers: { BDRip: ["BD"] } },
      group_registry: { options: [{ code: "VCB", label: "VCB" }] },
    },
    root,
    plan: {
      ready: false,
      registration_required: false,
      repair_required: true,
      repair: {
        preview_endpoint: "/api/media-directory-organizer/landing/repair/preview",
        apply_endpoint: "/api/media-directory-organizer/landing/repair/apply",
        candidates: [
          {
            label: "Planned Work",
            draft_work: draft,
            catalog_ref: {
              yaml_source_rel: "[JP][TVInfo][2024].yaml",
              index_in_file: 4,
              work_name: "Planned Work",
            },
          },
        ],
      },
      plan_id: "5555555555555555",
      assignments: [
        {
          source_dir: sourceDir,
          target_dir: targetDir,
          target_relpath: targetRelpath,
          work_name: "Planned Work",
        },
      ],
      moves: [
        {
          source: sourceDir + "\\01.mkv",
          target: targetDir + "\\Disc\\01.mkv",
        },
      ],
      issues: [{ code: "shortcut-catalog-path-missing", message: "missing path" }],
      unresolved_files: [],
    },
    routeOverrides,
    fileWorkOverrides,
    sourceWorkOverrides,
    sourcePressOverrides,
    multipleWorkSources: Object.create(null),
    draftWork: null,
    landing: null,
    shortcutPending: null,
    execution: null,
    busy: "",
    dirty: true,
    notice: "",
    noticeError: false,
  });

  hooks.render();
  assert.match(view.innerHTML, /修复数据库 \+ 整理媒体 \+ 快捷方式/);
  await hooks.beginShortcutOnlyPreview();
  assert.equal(hooks.state.shortcutPending.includeMediaMove, true);
  assert.match(hooks.state.notice, /串联预览/);
  hooks.selectShortcutRepairCandidate(0);
  assert.match(view.innerHTML, /数据库 press_path \/ 本次整理目标/);
  assert.match(view.innerHTML, /可填写本计划将创建的目标目录名/);

  await hooks.previewShortcutRepair(-1);
  const previewCall = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/repair/preview",
  );
  assert.ok(previewCall);
  assert.equal(previewCall.body.include_media_move, true);
  assert.deepEqual(previewCall.body.target_overrides, { [sourceDir]: targetRelpath });
  assert.deepEqual(previewCall.body.route_target_overrides, routeOverrides);
  assert.deepEqual(previewCall.body.file_work_overrides, fileWorkOverrides);
  assert.deepEqual(previewCall.body.source_work_overrides, sourceWorkOverrides);
  assert.deepEqual(previewCall.body.source_press_overrides, sourcePressOverrides);
  assert.match(view.innerHTML, /数据库修复 \+ 媒体归类 \+ 快捷方式/);
  assert.match(view.innerHTML, /确认修复、归类并创建快捷方式/);

  await hooks.applyShortcutRepair();
  const applyCall = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/repair/apply",
  );
  assert.ok(applyCall);
  assert.equal(applyCall.body.include_media_move, true);
  assert.equal(applyCall.body.acknowledge_move, true);
  assert.equal(applyCall.body.acknowledge_catalog_write, true);
  assert.equal(applyCall.body.acknowledge_shortcuts, true);
  assert.equal(hooks.state.plan, null);
  assert.equal(hooks.state.shortcutPending.kind, "landing");
  assert.deepEqual(hooks.state.shortcutPending.work_refs, workRefs);
  assert.equal(hooks.state.execution.moved_file_count, 1);
  assert.match(hooks.state.notice, /数据库修复和媒体归类已经完成/);
  assert.match(hooks.state.notice, /不要再次移动媒体/);
  assert.match(view.innerHTML, /不要重新执行媒体移动/);
});

test("last unresolved binding in a multi-work organizer stays database-only", async () => {
  const calls = [];
  const root = "U:\\グリザイアの果実";
  const repairPlanId = "6666666666666666";
  let hooks = null;
  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    contains() {
      return true;
    },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() {
      return null;
    },
  };
  const window = {
    document,
    confirm() {
      return true;
    },
    prompt() {
      return repairPlanId;
    },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) {
      hooks = value;
    },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  feature.init({
    esc(value) {
      return String(value == null ? "" : value);
    },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/landing/repair/preview") {
        return successfulResponse({
          ok: true,
          ready: true,
          media_move_planned: false,
          repair_plan_id: repairPlanId,
          repair: {
            preview_endpoint: url,
            apply_endpoint: "/api/media-directory-organizer/landing/repair/apply",
          },
          catalog_change: { action: "update" },
          shortcut_summary: {
            total_count: 2,
            planned_count: 2,
            already_exists_count: 0,
            conflict_count: 0,
          },
          shortcuts: [],
          issues: [],
        });
      }
      if (url === "/api/media-directory-organizer/landing/repair/apply") {
        return successfulResponse({ ok: true, state: "complete" });
      }
      throw new Error("unexpected request: " + url);
    },
  });

  function candidate(name, index) {
    return {
      label: name,
      draft_work: {
        name,
        date: { start: "2015-01-01", end: "2015-03-01" },
        domain: "animation",
        country: "japan",
        release_type: "tv",
        path: root,
        presses: [
          {
            press_format: "BDRip",
            press_group: "VCB",
            press_path: name + "_BDRip(VCB)",
          },
        ],
      },
      catalog_ref: {
        yaml_source_rel: "[JP][TVInfo][2015].yaml",
        index_in_file: index,
        work_name: name,
      },
    };
  }
  const candidates = [candidate("グリザイアの迷宮", 1)];
  Object.assign(hooks.state, {
    config: {
      paths: { catalog_root: "E:\\Project\\data", allowed_resource_roots: ["U:\\"] },
      detection: { format_markers: { BDRip: ["BD"] } },
      group_registry: { options: [{ code: "VCB", label: "VCB" }] },
    },
    root,
    plan: {
      ready: false,
      registration_required: false,
      repair_required: true,
      repair: {
        preview_endpoint: "/api/media-directory-organizer/landing/repair/preview",
        apply_endpoint: "/api/media-directory-organizer/landing/repair/apply",
        candidates,
      },
      assignments: [
        {
          source_dir: root + "\\fruit-source",
          target_dir: root + "\\fruit-target",
          catalog_file: "[JP][TVInfo][2014].yaml",
          work_name: "グリザイアの果実",
        },
        {
          source_dir: root + "\\maze-source",
          target_dir: root + "\\maze-target",
          catalog_file: "[JP][TVInfo][2015].yaml",
          work_name: "グリザイアの迷宮",
        },
        {
          source_dir: root + "\\eden-source",
          target_dir: root + "\\eden-target",
          catalog_file: "[JP][TVInfo][2015].yaml",
          work_name: "グリザイアの楽園",
        },
      ],
      moves: [{ source: root + "\\source\\01.mkv", target: root + "\\target\\01.mkv" }],
      issues: [{ code: "shortcut-catalog-path-missing", message: "missing path" }],
      unresolved_files: [],
    },
    routeOverrides: { routeA: "target" },
    fileWorkOverrides: { fileA: "グリザイアの果実" },
    sourceWorkOverrides: { sourceA: "グリザイアの果実" },
    sourcePressOverrides: { sourceA: { press_format: "BDRip", press_group: "VCB" } },
    multipleWorkSources: Object.create(null),
    shortcutPending: null,
    busy: "",
    dirty: true,
    notice: "",
    noticeError: false,
  });

  await hooks.beginShortcutOnlyPreview();
  assert.equal(hooks.state.shortcutPending.includeMediaMove, false);
  assert.equal(hooks.state.shortcutPending.stagedMultiWorkRepair, true);
  assert.match(hooks.state.notice, /逐个确认并修复/);
  assert.match(view.innerHTML, /不会移动媒体/);

  hooks.selectShortcutRepairCandidate(0);
  assert.match(view.innerHTML, /数据库 press_path \/ 已整理目标/);
  await hooks.previewShortcutRepair(-1);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].body.catalog_ref.work_name, "グリザイアの迷宮");
  assert.equal(Object.hasOwn(calls[0].body, "include_media_move"), false);
  assert.equal(Object.hasOwn(calls[0].body, "route_target_overrides"), false);
  assert.match(hooks.state.notice, /不会移动媒体文件/);

  await hooks.applyShortcutRepair();
  const applyCall = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/repair/apply",
  );
  assert.ok(applyCall);
  assert.equal(applyCall.body.catalog_ref.work_name, "グリザイアの迷宮");
  assert.equal(Object.hasOwn(applyCall.body, "include_media_move"), false);
  assert.equal(Object.hasOwn(applyCall.body, "acknowledge_move"), false);
  assert.equal(applyCall.body.acknowledge_catalog_write, true);
  assert.equal(applyCall.body.acknowledge_shortcuts, true);
  assert.match(hooks.state.notice, /媒体文件没有移动/);
});

test("shared physical target requires explicit multi-record preview and keeps two shortcuts", async () => {
  const calls = [];
  const root = "U:\\Fate／Zero";
  const landingPlanId = "7777777777777777";
  const source2011 = "[20111001][20111224] Fate／Zero 01-12";
  const source2012 = "[20120407][20120623] Fate／Zero 13-25";
  let hooks = null;
  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    contains() { return true; },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() { return null; },
  };
  const window = {
    document,
    confirm() { return true; },
    prompt() { return landingPlanId; },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) { hooks = value; },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );

  function candidate(year, index, workName) {
    const catalogRef = {
      yaml_source_rel: `[JP][TVInfo][${year}].yaml`,
      index_in_file: index,
      work_name: workName,
      source_sha256: year.repeat(16),
    };
    return {
      catalog_ref: catalogRef,
      draft_work: {
        name: workName,
        date: { start: `${year}-01-01`, end: `${year}-03-01` },
        domain: "animation",
        country: "japan",
        release_type: "tv",
        path: root,
        presses: [
          {
            press_key: "0:main::BDRip:VCB",
            press_format: "BDRip",
            press_group: "VCB",
            press_path: "",
          },
        ],
      },
      shared_target_members: [
        {
          catalog_ref: catalogRef,
          work_name: workName,
          press_key: "0:main::BDRip:VCB",
          press_format: "BDRip",
          press_group: "VCB",
          press_path: "",
          source_names: [],
        },
      ],
    };
  }

  const candidates = [
    candidate("2011", 10, "Fate／Zero 01-12"),
    candidate("2012", 11, "Fate／Zero 13-25"),
  ];
  feature.init({
    esc(value) { return String(value == null ? "" : value); },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/landing/preview") {
        const hasIncomingSource = body.shared_target_bindings.some((binding) =>
          binding.members.some((member) => member.source_names.length > 0),
        );
        const normalizedBindings = body.shared_target_bindings.map((binding) => ({
          ...binding,
          target_path: root + "\\" + binding.press_path,
          members: binding.members.map((member, memberIndex) => ({
            ...member,
            work_name: memberIndex === 0 ? "Fate／Zero 01-12" : "Fate／Zero 13-25",
          })),
        }));
        return successfulResponse({
          ok: true,
          ready: true,
          state: "shared_target_landing_preview",
          root,
          landing_plan_id: landingPlanId,
          shared_no_move: !hasIncomingSource,
          shared_target_bindings: normalizedBindings,
          shared_target_summary: {
            binding_count: 1,
            database_record_count: 2,
            physical_target_count: 1,
            shortcut_count: 2,
          },
          catalog_changes: [
            { action: "update", target: "db\\[JP][TVInfo][2011].yaml" },
            { action: "update", target: "db\\[JP][TVInfo][2012].yaml" },
          ],
          organizer_plan: {
            ready: true,
            plan_id: "8888888888888888",
            root,
            media_state: hasIncomingSource ? "pending" : "already_organized",
            summary: { file_count: hasIncomingSource ? 24 : 0, bytes: hasIncomingSource ? 1024 : 0 },
            assignments: [],
            moves: [],
            issues: [],
            unresolved_files: [],
          },
          shortcut_summary: {
            total_count: 2,
            planned_count: 2,
            already_exists_count: 0,
            conflict_count: 0,
          },
          shortcuts: [
            {
              shortcut_path: "E:\\LinkVideo\\[2011]\\Fate／Zero 01-12\\BDRip(VCB).lnk",
              target_path: root + "\\Fate／Zero_BDRip(VCBM)",
              status: "planned",
            },
            {
              shortcut_path: "E:\\LinkVideo\\[2012]\\Fate／Zero 13-25\\BDRip(VCB).lnk",
              target_path: root + "\\Fate／Zero_BDRip(VCBM)",
              status: "planned",
            },
          ],
          issues: [],
        });
      }
      if (url === "/api/media-directory-organizer/landing/apply") {
        return successfulResponse({ ok: true, state: "complete", media: { moved_file_count: 24 } });
      }
      throw new Error("unexpected request: " + url);
    },
  });

  const draft = {
    name: "Fate／Zero",
    date: { start: "", end: "" },
    domain: "animation",
    country: "japan",
    release_type: "tv",
    path: root,
    presses: [],
  };
  Object.assign(hooks.state, {
    config: {
      paths: { catalog_root: "E:\\Project\\nimda\\data", allowed_resource_roots: ["U:\\"] },
      group_registry: { options: [{ code: "VCB", label: "VCB" }] },
    },
    root,
    plan: {
      ready: false,
      registration_required: true,
      registration: {
        draft,
        existing_candidates: candidates,
        shared_target_supported: true,
        sources: [{ name: source2011 }, { name: source2012 }],
      },
      repair: { required: true, candidates },
      assignments: [],
      moves: [],
      issues: [{ code: "catalog-work-not-found" }],
      unresolved_files: [],
      summary: {},
    },
    draftWork: draft,
    draftWorks: [],
    sharedTarget: null,
    busy: "",
    dirty: false,
    landing: null,
    shortcutPending: null,
    notice: "",
    noticeError: false,
  });
  hooks.render();
  assert.match(view.innerHTML, /已有播出记录共享实体目录/);
  assert.match(view.innerHTML, /默认关闭/);
  assert.doesNotMatch(view.innerHTML, /data-shared-target-confirm checked/);

  hooks.setSharedTargetConfirmed(true);
  const bundle = hooks.sharedTargetCandidateBundle(hooks.state.plan, true);
  assert.equal(bundle.members.length, 2);
  hooks.setSharedTargetMemberSelected(bundle.members[0].key, true);
  hooks.setSharedTargetMemberSelected(bundle.members[1].key, true);
  hooks.setSharedTargetPressPath(bundle.members[0].groupKey, "Fate／Zero_BDRip(VCBM)");
  hooks.setSharedTargetSourceOwner(source2011, bundle.members[0].key);
  hooks.setSharedTargetSourceOwner(source2012, bundle.members[1].key);

  await hooks.previewLandingPlan();
  const firstPreview = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/preview",
  );
  assert.ok(firstPreview);
  assert.equal(firstPreview.body.acknowledge_shared_targets, true);
  assert.equal(Object.hasOwn(firstPreview.body, "draft_work"), false);
  assert.equal(firstPreview.body.shared_target_bindings.length, 1);
  assert.equal(firstPreview.body.shared_target_bindings[0].members.length, 2);
  assert.deepEqual(
    firstPreview.body.shared_target_bindings[0].members.map((member) => member.source_names),
    [[source2011], [source2012]],
  );
  assert.match(view.innerHTML, /2 条数据库记录 \/ 1 个实体目录 \/ 2 个快捷方式/);
  assert.equal(hooks.planCanExecute(), true);

  hooks.setSharedTargetPressPath(bundle.members[0].groupKey, "Fate／Zero_BDRip(VCBM)-edited");
  assert.equal(hooks.state.landing, null);
  assert.equal(hooks.planCanExecute(), false);
  await hooks.applyCurrentLanding();
  assert.equal(
    calls.filter((call) => call.url === "/api/media-directory-organizer/landing/apply").length,
    0,
  );

  hooks.setSharedTargetPressPath(bundle.members[0].groupKey, "Fate／Zero_BDRip(VCBM)");
  await hooks.previewLandingPlan();
  await hooks.applyCurrentLanding();
  const applyCall = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/apply",
  );
  assert.ok(applyCall);
  assert.equal(applyCall.body.acknowledge_shared_targets, true);
  assert.equal(applyCall.body.acknowledge_catalog_write, true);
  assert.equal(applyCall.body.acknowledge_move, true);
  assert.equal(applyCall.body.acknowledge_shortcuts, true);
  assert.equal(applyCall.body.shared_target_bindings.length, 1);
  assert.match(hooks.state.notice, /媒体只归类一次/);

  Object.assign(hooks.state, {
    plan: {
      ready: false,
      registration_required: false,
      repair: { required: true, candidates },
      assignments: [
        { source_dir: root + "\\" + source2011, work_name: "Fate／Zero 01-12" },
        { source_dir: root + "\\" + source2012, work_name: "Fate／Zero 13-25" },
      ],
      moves: [],
      issues: [],
      unresolved_files: [],
      summary: {},
    },
    draftWork: null,
    draftWorks: [],
    sharedTarget: null,
    landing: null,
    busy: "",
    dirty: false,
  });
  hooks.render();
  assert.match(view.innerHTML, /已有播出记录共享实体目录/);
  assert.match(
    view.innerHTML,
    /data-organizer-action="landing-preview" disabled/,
    "repair candidates must not be applicable before shared mode is explicitly enabled",
  );
  hooks.setSharedTargetConfirmed(true);
  assert.doesNotMatch(view.innerHTML, /data-organizer-action="landing-preview" disabled/);

  const landedTarget = "Fate／Zero_BDRip(VCBM)";
  Object.assign(hooks.state, {
    plan: {
      ready: false,
      registration_required: true,
      registration: {
        draft,
        existing_candidates: candidates,
        shared_target_supported: true,
        sources: [{ name: landedTarget }],
      },
      repair: { required: true, candidates },
      assignments: [],
      moves: [],
      issues: [{ code: "catalog-work-not-found" }],
      unresolved_files: [],
      summary: {},
    },
    draftWork: draft,
    sharedTarget: null,
    landing: null,
    busy: "",
    dirty: false,
  });
  hooks.render();
  hooks.setSharedTargetConfirmed(true);
  const landedBundle = hooks.sharedTargetCandidateBundle(hooks.state.plan, true);
  hooks.setSharedTargetMemberSelected(landedBundle.members[0].key, true);
  hooks.setSharedTargetMemberSelected(landedBundle.members[1].key, true);
  hooks.setSharedTargetSourceOwner(landedTarget, landedBundle.members[0].key);
  hooks.setSharedTargetPressPath(landedBundle.members[0].groupKey, landedTarget);
  const landedBinding = hooks.buildSharedTargetBindings(hooks.state.plan);
  assert.equal(landedBinding.error, "");
  assert.deepEqual(
    JSON.parse(
      JSON.stringify(
        landedBinding.bindings[0].members.map((member) => member.source_names),
      ),
    ),
    [[], []],
  );
  assert.match(view.innerHTML, /已落地目标（不分配 owner）/);
  hooks.setSharedTargetSourceOwner(landedTarget, landedBundle.members[0].key);
  const landedOwnedBinding = hooks.buildSharedTargetBindings(hooks.state.plan);
  assert.equal(landedOwnedBinding.error, "");
  assert.deepEqual(
    JSON.parse(
      JSON.stringify(
        landedOwnedBinding.bindings[0].members.map((member) => member.source_names),
      ),
    ),
    [[landedTarget], []],
  );
  hooks.setSharedTargetSourceOwner(landedTarget, "");
  await hooks.previewLandingPlan();
  const landedPreviewCall = calls
    .filter((call) => call.url === "/api/media-directory-organizer/landing/preview")
    .at(-1);
  assert.deepEqual(
    landedPreviewCall.body.shared_target_bindings[0].members.map(
      (member) => member.source_names,
    ),
    [[], []],
  );
  assert.equal(hooks.state.plan.summary.file_count, 0);
  assert.equal(hooks.planCanExecute(), true);
  assert.match(view.innerHTML, /data-organizer-action="apply">确认完整落地/);
  assert.match(view.innerHTML, /2 条数据库记录 \/ 1 个实体目录 \/ 2 个快捷方式/);
  hooks.state.landing.ready = false;
  hooks.state.landing.issues = [
    { code: "shared-target-empty", message: "共享实体目标中没有普通文件" },
  ];
  hooks.render();
  assert.match(view.innerHTML, /共享实体目录为空/);
});

test("per-source work bindings keep exact refs, mixed landing snapshots, and legacy diagnostics separate", async () => {
  const calls = [];
  const confirms = [];
  const root = "U:\\Mixed Root";
  const sourceA = "Existing.A.BDRip";
  const sourceB = "Ambiguous.BDRip";
  const sourceC = "Missing.WebRip";
  const sourceAPath = root + "\\" + sourceA;
  const sourceBPath = root + "\\" + sourceB;
  const sourceCPath = root + "\\" + sourceC;
  const landingPlanId = "dddddddddddddddd";
  const sha = (character) => character.repeat(64);
  const refA = {
    yaml_source_rel: "[JP][TVInfo][2020].yaml",
    index_in_file: 0,
    work_name: "Existing Work",
    source_sha256: sha("a"),
  };
  const refB1 = {
    yaml_source_rel: "[JP][TVInfo][2021-a].yaml",
    index_in_file: 1,
    work_name: "Same Work",
    source_sha256: sha("b"),
  };
  const refB2 = {
    yaml_source_rel: "[JP][TVInfo][2021-b].yaml",
    index_in_file: 4,
    work_name: "Same Work",
    source_sha256: sha("c"),
  };
  const candidate = (catalogRef, pressPath) => ({
    catalog_ref: catalogRef,
    work: {
      name: catalogRef.work_name,
      date: { start: "2021-01-01", end: "2021-03-31" },
      domain: "animation",
      country: "japan",
      release_type: "tv",
      path: root,
      presses: [
        {
          press_format: "BDRip",
          press_group: "VCB",
          press_path: pressPath,
        },
      ],
    },
  });
  const initialPlan = {
    root,
    plan_id: "aaaaaaaaaaaaaaaa",
    ready: false,
    registration_required: false,
    family_works: ["Existing Work", "Same Work"],
    assignments: [],
    moves: [],
    unresolved_files: [
      {
        source_dir: sourceCPath,
        source: sourceCPath + "\\episode.mkv",
        reason: "无法自动确定所属作品",
        candidates: [],
      },
    ],
    issues: [{ code: "source-work-selection-required", path: sourceBPath }],
    summary: { source_directory_count: 3, file_count: 3, bytes: 12 },
    legacy_shortcut_authoritative: false,
    legacy_shortcut_diagnostics: [
      {
        source_name: sourceA,
        status: "conflict",
        shortcut_path: "E:\\LinkVideo\\wrong.lnk",
        existing_target_path: "U:\\Wrong Target",
        message: "旧快捷方式目标错误",
      },
    ],
    source_work_bindings: [
      {
        source_name: sourceA,
        source_path: sourceAPath,
        selected: true,
        state: "automatic_matched",
        requested_mode: "automatic",
        suggested_work_name: "Existing Work",
        suggestion_authority: "directory_press_suffix",
        catalog_ref: refA,
        resolved_works: [candidate(refA, "Existing Work_BDRip(VCBM)")],
        candidates: [candidate(refA, "Existing Work_BDRip(VCBM)")],
        inferred_press: [{ press_format: "BDRip", press_group: "VCB" }],
        catalog_press_required: true,
        catalog_repair_required: true,
        registration_required: true,
        next_action: "complete_manual",
        registration: {
          draft: {
            name: "Existing Work",
            path: root,
            presses: [
              {
                source_names: [sourceA],
                press_format: "BDRip",
                press_group: "VCB",
                press_path: "Existing Work_BDRip(VCBM)",
              },
            ],
          },
        },
      },
      {
        source_name: sourceB,
        source_path: sourceBPath,
        selected: true,
        state: "automatic_multiple",
        requested_mode: "automatic",
        resolved_works: [
          candidate(refB1, "Same Work_BDRip(VCBM)"),
          candidate(refB2, "Same Work_BDRip(VCBM)"),
        ],
        candidates: [
          candidate(refB1, "Same Work_BDRip(VCBM)"),
          candidate(refB2, "Same Work_BDRip(VCBM)"),
        ],
        inferred_press: [{ press_format: "BDRip", press_group: "VCB" }],
      },
      {
        source_name: sourceC,
        source_path: sourceCPath,
        selected: true,
        state: "manual_unresolved",
        requested_mode: "manual",
        requested_work_name: "New Missing Work",
        resolved_works: [],
        candidates: [],
        inferred_press: [{ press_format: "WebRip", press_group: "JSUM" }],
        registration_required: true,
        next_action: "search",
        registration: {
          draft: {
            name: "New Missing Work",
            date: { start: "", end: "" },
            domain: "animation",
            country: "japan",
            release_type: "tv",
            path: root,
            presses: [
              {
                source_names: [sourceC],
                press_format: "WebRip",
                press_group: "JSUM",
                press_path: "New Missing Work_WebRip(JSUM)",
              },
            ],
          },
        },
      },
    ],
  };
  let hooks = null;
  let landingPreviewCount = 0;
  const view = {
    innerHTML: "",
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    contains() { return true; },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() { return null; },
  };
  const window = {
    document,
    confirm(message) { confirms.push(String(message || "")); return true; },
    prompt() { return landingPlanId; },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) { hooks = value; },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  assert.ok(hooks);
  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  feature.init({
    esc(value) { return String(value == null ? "" : value); },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      if (url === "/api/media-directory-organizer/preview") {
        return successfulResponse({ ok: true, plan: JSON.parse(JSON.stringify(initialPlan)) });
      }
      if (url === "/api/media-directory-organizer/landing/preview") {
        landingPreviewCount += 1;
        const normalized = JSON.parse(JSON.stringify(body.source_work_bindings));
        Object.keys(normalized).forEach((sourceName) => {
          const binding = normalized[sourceName];
          binding.work_name = binding.mode === "catalog"
            ? binding.catalog_ref.work_name
            : binding.draft_work.name;
        });
        return successfulResponse({
          ok: true,
          state: "mixed_source_landing_preview",
          root,
          source_work_bindings: normalized,
          source_work_binding_summary: {
            total_count: 3,
            catalog_count: 2,
            draft_count: 1,
            existing_work_count: 2,
            new_work_count: 1,
            press_count: 3,
          },
          catalog_changes: [
            { action: "update", target: "[JP][TVInfo][2020].yaml" },
            { action: "update", target: "[JP][TVInfo][2021-b].yaml" },
            { action: "append", target: "[JP][TVInfo][2022].yaml" },
          ],
          organizer_plan: {
            ...JSON.parse(JSON.stringify(initialPlan)),
            ready: true,
            plan_id: "eeeeeeeeeeeeeeee",
            issues: [],
            unresolved_files: [],
            summary: { source_directory_count: 3, file_count: 3, bytes: 12 },
          },
          shortcuts: [
            { status: "planned", shortcut_path: "E:\\LinkVideo\\a.lnk" },
            { status: "planned", shortcut_path: "E:\\LinkVideo\\b.lnk" },
            { status: "planned", shortcut_path: "E:\\LinkVideo\\c.lnk" },
          ],
          shortcut_summary: { planned_count: 3, conflict_count: 0 },
          ready: true,
          landing_plan_id: landingPlanId,
        });
      }
      if (url === "/api/media-directory-organizer/landing/apply") {
        return successfulResponse({ ok: true, state: "complete" });
      }
      throw new Error("unexpected request: " + url);
    },
  });
  Object.assign(hooks.state, {
    config: { paths: { catalog_root: "E:\\Project\\data", allowed_resource_roots: ["U:\\"] } },
    root,
    plan: JSON.parse(JSON.stringify(initialPlan)),
    busy: "",
    dirty: false,
    landing: null,
    shortcutPending: null,
    sourceWorkOverrides: Object.create(null),
    sourceWorkBindingEdits: Object.create(null),
    sourceWorkDrafts: Object.create(null),
  });
  hooks.render();

  assert.equal((view.innerHTML.match(/data-source-work-card=/g) || []).length, 3);
  assert.equal((view.innerHTML.match(/data-source-work-catalog=/g) || []).length, 3);
  assert.equal((view.innerHTML.match(/data-source-work-manual=/g) || []).length, 3);
  assert.match(view.innerHTML, /已有作品、压制记录待补/);
  assert.match(view.innerHTML, /查找作品信息/);
  assert.match(view.innerHTML, /手动填写完整信息/);
  assert.match(view.innerHTML, /2021-a.*#2/);
  assert.match(view.innerHTML, /2021-b.*#5/);
  assert.match(view.innerHTML, /旧快捷方式诊断（不作为作品绑定依据）/);
  assert.match(view.innerHTML, /不会反向替你选择数据库作品/);
  assert.match(view.innerHTML, /目录名推导作品：<strong>Existing Work<\/strong>（已精确匹配 DB）/);
  assert.doesNotMatch(view.innerHTML, /<select data-source-work-dir=/);
  assert.match(view.innerHTML, /确认目录包含多个作品/);

  const rows = hooks.sourceWorkBindingRows(hooks.state.plan);
  assert.equal(rows[0].work.name, "Existing Work");
  assert.equal(rows[0].suggestedWorkName, "Existing Work");
  assert.equal(rows[0].inferredPresses[0].press_group, "VCB");
  let payload = JSON.parse(JSON.stringify(hooks.sourceWorkBindingsPayload()));
  assert.equal(payload[sourceA].mode, "catalog");
  assert.equal(payload[sourceB].mode, "automatic");
  assert.equal(payload[sourceC].mode, "automatic");

  hooks.openSourceWorkEditor(sourceC, "manual");
  hooks.state.landing = { ready: true };
  hooks.state.landingSourceWorkBindings = { stale: true };
  hooks.setSourceMultipleWorkMode(sourceCPath, true);
  assert.equal(Object.hasOwn(hooks.state.sourceWorkBindingEdits, sourceC), false);
  assert.equal(Object.hasOwn(hooks.state.sourceWorkDrafts, sourceC), false);
  assert.equal(hooks.state.sourceWorkEditorSource, "");
  assert.equal(hooks.state.landing, null);
  assert.equal(hooks.state.landingSourceWorkBindings, null);
  payload = JSON.parse(JSON.stringify(hooks.sourceWorkBindingsPayload()));
  assert.equal(Object.hasOwn(payload, sourceC), false);
  assert.match(view.innerHTML, /多作品逐文件分流/);
  hooks.state.fileWorkOverrides[sourceCPath + "\\episode.mkv"] = "Existing Work";
  await hooks.previewPlan();
  const multiPreview = calls.filter(
    (call) => call.url === "/api/media-directory-organizer/preview",
  ).at(-1);
  assert.equal(Object.hasOwn(multiPreview.body.source_work_bindings, sourceC), false);
  assert.equal(
    multiPreview.body.file_work_overrides[sourceCPath + "\\episode.mkv"],
    "Existing Work",
  );
  hooks.setSourceMultipleWorkMode(sourceCPath, false);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(Object.hasOwn(hooks.state.multipleWorkSources, sourceCPath), false);

  const rowB = rows.find((row) => row.sourceName === sourceB);
  const secondKey = hooks.sourceWorkCandidateKey(rowB.candidates.find(
    (item) => item.catalogRef.index_in_file === refB2.index_in_file,
  ));
  hooks.selectSourceCatalogBinding(sourceB, secondKey);
  await new Promise((resolve) => setImmediate(resolve));
  payload = JSON.parse(JSON.stringify(hooks.sourceWorkBindingsPayload()));
  assert.deepEqual(payload[sourceB].catalog_ref, refB2);
  const firstBindingPreview = calls.find(
    (call) => call.url === "/api/media-directory-organizer/preview" &&
      call.body.source_work_bindings[sourceB] &&
      call.body.source_work_bindings[sourceB].mode === "catalog",
  );
  assert.deepEqual(
    Object.keys(firstBindingPreview.body.source_work_bindings).sort(),
    [sourceA, sourceB, sourceC].sort(),
  );
  assert.equal(firstBindingPreview.body.source_work_bindings[sourceC].mode, "automatic");

  hooks.setSourceWorkBinding(sourceB, "Same Work");
  await new Promise((resolve) => setImmediate(resolve));
  payload = JSON.parse(JSON.stringify(hooks.sourceWorkBindingsPayload()));
  assert.equal(payload[sourceB].mode, "manual");
  assert.equal(payload[sourceB].work_name, "Same Work");
  hooks.selectSourceCatalogBinding(sourceB, secondKey);
  await new Promise((resolve) => setImmediate(resolve));

  hooks.state.sourceWorkOverrides[sourceBPath] = "stale legacy choice";
  hooks.state.sourceWorkOverrides["U:\\Unrelated\\source"] = "Legacy Work";
  payload = hooks.sourceWorkBindingsPayload();
  const filteredLegacy = hooks.sourceWorkOverridesPayload(payload);
  assert.equal(Object.hasOwn(filteredLegacy, sourceBPath), false);
  assert.equal(filteredLegacy["U:\\Unrelated\\source"], "Legacy Work");
  delete hooks.state.sourceWorkOverrides[sourceBPath];
  delete hooks.state.sourceWorkOverrides["U:\\Unrelated\\source"];

  hooks.openSourceWorkEditor(sourceC, "manual");
  hooks.updateSourceWorkDraft(sourceC, "start", "2022-01-01", "date", -1);
  hooks.updateSourceWorkDraft(sourceC, "press_path", "New Missing Work_WebRip(JSUM)", "press", 0);
  const landingPayload = JSON.parse(JSON.stringify(
    hooks.sourceWorkBindingsPayload({ forLanding: true }),
  ));
  assert.deepEqual(Object.keys(landingPayload).sort(), [sourceA, sourceB, sourceC].sort());
  assert.equal(landingPayload[sourceA].presses.length, 1);
  assert.equal(landingPayload[sourceB].presses.length, 1);
  assert.equal(landingPayload[sourceC].mode, "draft");
  assert.equal(hooks.sourceWorkLandingPayloadIssue(landingPayload), "");

  await hooks.previewLandingPlan();
  assert.equal(landingPreviewCount, 1);
  const firstLandingRequest = calls.filter(
    (call) => call.url === "/api/media-directory-organizer/landing/preview",
  )[0].body;
  assert.deepEqual(Object.keys(firstLandingRequest.source_work_bindings).sort(), [sourceA, sourceB, sourceC].sort());
  assert.deepEqual(
    firstLandingRequest.source_work_bindings[sourceB].catalog_ref,
    refB2,
  );
  const firstSnapshot = JSON.parse(JSON.stringify(hooks.state.landingSourceWorkBindings));
  assert.equal(firstSnapshot[sourceA].work_name, "Existing Work");

  hooks.updateSourceCatalogPress(sourceA, "press_path", "Existing Work_BDRip(VCBM)-fixed", 0);
  assert.equal(hooks.state.landing, null);
  assert.equal(hooks.state.landingSourceWorkBindings, null);
  await hooks.previewLandingPlan();
  const secondSnapshot = JSON.parse(JSON.stringify(hooks.state.landingSourceWorkBindings));
  assert.equal(
    secondSnapshot[sourceA].presses[0].press_path,
    "Existing Work_BDRip(VCBM)-fixed",
  );

  await hooks.applyCurrentLanding();
  const applyCall = calls.find(
    (call) => call.url === "/api/media-directory-organizer/landing/apply",
  );
  assert.ok(applyCall);
  assert.deepEqual(applyCall.body.source_work_bindings, secondSnapshot);
  assert.equal(applyCall.body.acknowledge_catalog_write, true);
  assert.equal(applyCall.body.acknowledge_move, true);
  assert.equal(applyCall.body.acknowledge_shortcuts, true);
  assert.equal(
    confirms.some((message) => message.includes("3 个一级目录（已有 2 / 新增 1 / 未决 0）")),
    true,
  );
});

test("changing the work root clears every plan-scoped choice before the first preview", async () => {
  const calls = [];
  const handlers = Object.create(null);
  const oldRoot = "U:\\Little Busters!";
  const typedRoot = "U:\\Fate／Apocrypha";
  const pickedRoot = "U:\\Zombie Land Saga";
  const defensiveRoot = "U:\\グリザイアの果実";
  let hooks = null;

  const view = {
    innerHTML: "",
    addEventListener(type, handler) { handlers[type] = handler; },
    removeEventListener(type, handler) {
      if (handlers[type] === handler) delete handlers[type];
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    contains() { return true; },
  };
  const document = {
    getElementById(id) {
      return id === "media-directory-organizer-view" ? view : null;
    },
    querySelector() { return null; },
  };
  const window = {
    document,
    confirm() { return true; },
    prompt() { return ""; },
    setTimeout,
    __NIMDA_ORGANIZER_TEST_HOOK__(value) { hooks = value; },
  };
  window.window = window;
  const sandbox = vm.createContext({ window, document, console, setTimeout, clearTimeout });
  vm.runInContext(fs.readFileSync(FRONTEND, "utf8"), sandbox, { filename: FRONTEND });
  assert.ok(hooks);

  const feature = window.JpTvBrowseFeatureRegistry.features.find(
    (item) => item.id === "media-directory-organizer",
  );
  feature.init({
    esc(value) { return String(value == null ? "" : value); },
    fetchJson(url, options = {}) {
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ url, body });
      assert.equal(url, "/api/media-directory-organizer/preview");
      return successfulResponse({
        ok: true,
        plan: {
          root: body.root,
          plan_id: "9999999999999999",
          ready: false,
          registration_required: false,
          family_works: [],
          assignments: [],
          moves: [],
          unresolved_files: [],
          issues: [{ code: "preview-test", message: "no files in test root" }],
          summary: { source_directory_count: 0, file_count: 0, bytes: 0 },
          source_work_bindings: [],
          shortcuts: [],
          shortcut_summary: {
            planned_count: 0,
            already_exists_count: 0,
            conflict_count: 0,
          },
        },
      });
    },
  });

  hooks.state.config = {
    paths: {
      catalog_root: "E:\\Project\\data",
      allowed_resource_roots: ["U:\\"],
    },
  };

  function seedStaleState(root) {
    const source = root + "\\[VCB-Studio] stale release";
    const staleRef = {
      yaml_source_rel: "[JP][TVInfo][2012].yaml",
      index_in_file: 7,
      work_name: "Little Busters!",
      source_sha256: "a".repeat(64),
    };
    hooks.state.root = root;
    hooks.state.plan = {
      root,
      plan_id: "1111111111111111",
      ready: true,
      registration_required: false,
      family_works: ["Little Busters!"],
      assignments: [],
      moves: [],
      unresolved_files: [],
      issues: [],
      summary: { source_directory_count: 1, file_count: 1, bytes: 4 },
      source_work_bindings: [
        {
          source_name: "[VCB-Studio] stale release",
          source_path: source,
          selected: true,
          state: "automatic_matched",
          requested_mode: "automatic",
          catalog_ref: staleRef,
          resolved_works: [{ catalog_ref: staleRef, work: { name: "Little Busters!" } }],
          candidates: [{ catalog_ref: staleRef, work: { name: "Little Busters!" } }],
          inferred_press: [{ press_format: "BDRip", press_group: "VCB" }],
        },
      ],
      shortcut_plan_id: "2222222222222222",
      shortcut_scope: { root, work_refs: [staleRef] },
      shortcuts: [{ status: "planned", shortcut_path: "E:\\stale.lnk" }],
      shortcut_summary: { planned_count: 1, conflict_count: 0 },
    };
    hooks.state.execution = { moved_file_count: 99 };
    hooks.state.routeOverrides = { [source]: "Little Busters!_BDRip(VCBM)" };
    hooks.state.fileWorkOverrides = { [source + "\\01.mkv"]: "Little Busters!" };
    hooks.state.sourceWorkOverrides = { [source]: "Little Busters!" };
    hooks.state.sourceWorkBindingEdits = {
      "[VCB-Studio] stale release": { mode: "catalog", catalog_ref: staleRef },
    };
    hooks.state.sourceWorkDrafts = {
      "[VCB-Studio] stale release": { name: "Little Busters! EX", path: root },
    };
    hooks.state.sourceWorkEditorSource = "[VCB-Studio] stale release";
    hooks.state.sourceWorkEditorMode = "draft";
    hooks.state.landingSourceWorkBindings = { stale: true };
    hooks.state.multipleWorkSources = { [source]: true };
    hooks.state.sourcePressOverrides = {
      [source]: { press_format: "BDRip", press_group: "VCB" },
    };
    hooks.state.landing = { ready: true, landing_plan_id: "3333333333333333" };
    hooks.state.draftWork = { name: "Little Busters!", path: root };
    hooks.state.draftWorks = [{ name: "Little Busters! EX", path: root }];
    hooks.state.sharedTarget = {
      confirmed: true,
      selectedMembers: { stale: true },
      pressPaths: { stale: "Little Busters!_BDRip(VCBM)" },
      sourceOwners: { [source]: "stale" },
    };
    hooks.state.shortcutPending = { state: "shortcut_pending", root };
    hooks.state.recognition = {
      query: "Little Busters!",
      loading: false,
      error: "",
      notice: "stale recognition",
      results: { candidates: [{ name: "Little Busters!" }] },
      requestSnapshot: "stale",
      inputFingerprint: "stale",
    };
    hooks.state.dirty = false;
    hooks.state.busy = "";
    hooks.state.notice = "old plan ready";
    hooks.state.noticeError = false;
    return source;
  }

  function assertScopedStateCleared(oldSource, expectedRoot, noticePattern) {
    assert.equal(hooks.state.root, expectedRoot);
    assert.equal(hooks.state.plan, null);
    assert.equal(hooks.state.execution, null);
    assert.deepEqual(Object.keys(hooks.state.routeOverrides), []);
    assert.deepEqual(Object.keys(hooks.state.fileWorkOverrides), []);
    assert.deepEqual(Object.keys(hooks.state.sourceWorkOverrides), []);
    assert.deepEqual(Object.keys(hooks.state.sourceWorkBindingEdits), []);
    assert.deepEqual(Object.keys(hooks.state.sourceWorkDrafts), []);
    assert.equal(hooks.state.sourceWorkEditorSource, "");
    assert.equal(hooks.state.sourceWorkEditorMode, "");
    assert.equal(hooks.state.landingSourceWorkBindings, null);
    assert.deepEqual(Object.keys(hooks.state.multipleWorkSources), []);
    assert.deepEqual(Object.keys(hooks.state.sourcePressOverrides), []);
    assert.equal(hooks.state.landing, null);
    assert.equal(hooks.state.draftWork, null);
    assert.equal(Array.isArray(hooks.state.draftWorks), true);
    assert.equal(hooks.state.draftWorks.length, 0);
    assert.equal(hooks.state.sharedTarget.confirmed, false);
    assert.deepEqual(Object.keys(hooks.state.sharedTarget.selectedMembers), []);
    assert.deepEqual(Object.keys(hooks.state.sharedTarget.pressPaths), []);
    assert.deepEqual(Object.keys(hooks.state.sharedTarget.sourceOwners), []);
    assert.equal(hooks.state.shortcutPending, null);
    assert.equal(hooks.state.recognition.query, "");
    assert.equal(hooks.state.recognition.results, null);
    assert.equal(Object.hasOwn(hooks.sourceWorkBindingsPayload(), oldSource), false);
    assert.equal(hooks.planCanExecute(), false);
    assert.equal(hooks.state.dirty, true);
    assert.equal(hooks.state.noticeError, false);
    assert.match(hooks.state.notice, noticePattern);
  }

  function assertFreshPreviewBody(call, expectedRoot, oldSource) {
    assert.equal(call.url, "/api/media-directory-organizer/preview");
    assert.equal(call.body.root, expectedRoot);
    assert.deepEqual(call.body.target_overrides, {});
    assert.deepEqual(call.body.route_target_overrides, {});
    assert.deepEqual(call.body.file_work_overrides, {});
    assert.deepEqual(call.body.source_work_overrides, {});
    assert.deepEqual(call.body.source_work_bindings, {});
    assert.deepEqual(call.body.source_press_overrides, {});
    assert.equal(JSON.stringify(call.body).includes(oldSource), false);
  }

  const typedOldSource = seedStaleState(oldRoot);
  assert.equal(hooks.planCanExecute(), true);
  assert.equal(typeof handlers.input, "function");
  handlers.input({
    type: "input",
    target: {
      id: "organizer-root-input",
      value: typedRoot,
      matches() { return false; },
      getAttribute() { return ""; },
    },
  });
  assertScopedStateCleared(typedOldSource, typedRoot, /作品根目录已编辑/);
  await hooks.previewPlan();
  assertFreshPreviewBody(calls.at(-1), typedRoot, typedOldSource);
  assert.equal(hooks.state.dirty, false);

  const pickedOldSource = seedStaleState(typedRoot);
  hooks.state.resourcePicker = {
    open: true,
    loading: false,
    searching: false,
    error: "",
    cache: null,
    current: null,
    history: [],
    searchDraft: "",
    searchQuery: "",
    searchResults: null,
  };
  hooks.selectResourcePickerPath(pickedRoot);
  assertScopedStateCleared(pickedOldSource, pickedRoot, /资源库选择新的作品根目录/);
  assert.equal(hooks.state.resourcePicker.open, false);
  await hooks.previewPlan();
  assertFreshPreviewBody(calls.at(-1), pickedRoot, pickedOldSource);

  const defensiveOldSource = seedStaleState(pickedRoot);
  hooks.state.root = defensiveRoot;
  await hooks.previewPlan();
  assertFreshPreviewBody(calls.at(-1), defensiveRoot, defensiveOldSource);

  const preservedPlan = hooks.state.plan;
  hooks.state.routeOverrides = { keep: "same-root" };
  hooks.state.dirty = false;
  hooks.selectResourcePickerPath(defensiveRoot + " ");
  assert.equal(hooks.state.plan, preservedPlan);
  assert.equal(hooks.state.routeOverrides.keep, "same-root");
  assert.equal(hooks.state.dirty, false);
  assert.match(hooks.state.notice, /仍使用当前作品根目录/);
});
