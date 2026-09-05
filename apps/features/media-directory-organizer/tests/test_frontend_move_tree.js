"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { loadFrontend, organizerPlanFixture } = require("../../../../scripts/benchmark-frontend.js");

function setup(plan) {
  const listeners = new Map();
  const view = { innerHTML: "", contains(node) { return !node.detached; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener(type, callback, capture) { listeners.set(type, { callback, capture }); },
    removeEventListener(type) { listeners.delete(type); },
  };
  const document = { getElementById(id) { return id === "media-directory-organizer-view" ? view : null; },
    querySelector() { return null; } };
  const runtime = loadFrontend("media-directory-organizer",
    "{ state: state, render: render, assignmentView: assignmentMoveView, canExecute: planCanExecute }", { document });
  const feature = runtime.window.JpTvBrowseFeatureRegistry.features[0];
  feature.init({});
  Object.assign(runtime.hooks.state, { root: "S:/Work", plan, config: {}, busy: "", dirty: false });
  runtime.hooks.render();
  function toggle(info, open, nodePath = null, body = { innerHTML: "" }) {
    const details = { open,
      getAttribute(name) { return name === "data-move-view" ? info.id : name === "data-move-node" ? nodePath : null; },
      querySelector() { return body; },
    };
    listeners.get("toggle").callback({ target: details });
    return body;
  }
  function page(info, nodePath, number, body) {
    const attrs = { "data-organizer-action": "move-files-page", "data-move-view": info.id,
      "data-move-node": nodePath, "data-move-page": String(number) };
    const button = {
      getAttribute(name) { return attrs[name] ?? null; },
      closest(selector) {
        if (selector === "[data-organizer-action]") return this;
        if (selector === "[data-move-body]") return body;
        return null;
      },
    };
    listeners.get("click").callback({ target: button });
  }
  return { ...runtime, view, toggle, page, feature, listeners };
}

test("closed move previews omit file rows, expand one level, and retain expansion across renders", () => {
  const plan = organizerPlanFixture(1, 2);
  const app = setup(plan);
  const info = app.hooks.assignmentView(plan, plan.assignments[0]);
  assert.match(app.view.innerHTML, /文件明细（2 个文件/);
  assert.doesNotMatch(app.view.innerHTML, /0\.mkv/);
  assert.equal(info.tree, null, "closed detail summaries do not need a recursive directory tree");
  assert.equal(app.listeners.get("toggle").capture, true, "native details toggles require capture delegation");
  const outer = app.toggle(info, true);
  assert.match(outer.innerHTML, /data-move-node="ready0\\disc"/);
  assert.doesNotMatch(outer.innerHTML, /0\.mkv/);
  const disc = app.toggle(info, true, "ready0\\disc");
  assert.match(disc.innerHTML, /data-move-node="ready0\\disc\\0"/);
  assert.doesNotMatch(disc.innerHTML, /0\.mkv/);
  const episode = app.toggle(info, true, "ready0\\disc\\0");
  assert.match(episode.innerHTML, /0\.mkv/);
  assert.match(episode.innerHTML, /1\.mkv/);
  app.hooks.render();
  assert.match(app.view.innerHTML, /0\.mkv/);
  app.toggle(info, false, "ready0\\disc", disc);
  assert.equal(disc.innerHTML, "");
  app.hooks.render();
  assert.doesNotMatch(app.view.innerHTML, /0\.mkv/);
  app.toggle(info, true, "ready0\\disc", disc);
  assert.match(disc.innerHTML, /0\.mkv/, "reopening a parent retains its child's expanded state");
  app.toggle(info, false, null, outer);
  assert.equal(outer.innerHTML, "");
  assert.equal(app.hooks.state.dirty, false, "browsing details does not edit the reviewed media plan");
});

test("new preview objects replace cached rows and ignore queued toggles from the previous preview", () => {
  const plan = organizerPlanFixture(1, 1);
  const app = setup(plan);
  const previous = app.hooks.assignmentView(plan, plan.assignments[0]);
  app.toggle(previous, true);
  const nextPlan = organizerPlanFixture(1, 3);
  app.hooks.state.plan = nextPlan;
  app.hooks.state.dirty = true;
  app.hooks.render();
  const next = app.hooks.assignmentView(nextPlan, nextPlan.assignments[0]);
  assert.notEqual(next.id, previous.id);
  assert.equal(next.tree, null);
  assert.match(app.view.innerHTML, /文件明细（3 个文件/);
  assert.doesNotMatch(app.view.innerHTML, /0\.mkv/);
  const staleBody = { innerHTML: "stale-event-must-not-render" };
  app.toggle(previous, false, null, staleBody);
  assert.equal(staleBody.innerHTML, "stale-event-must-not-render");
  assert.equal(app.hooks.canExecute(), false, "detail state cannot revive an invalidated plan");
  app.feature.dispose();
  assert.equal(app.listeners.has("toggle"), false);
});

test("route lookup retains exact route identity and legacy source path boundaries", () => {
  const plan = organizerPlanFixture(2, 1);
  const app = setup(plan);
  const first = app.hooks.assignmentView(plan, plan.assignments[0]);
  assert.equal(first.moves.length, 1);
  assert.equal(first.moves[0].route_id, "route0");
  const legacy = { source_dir: "S:/Work/1", target_dir: "S:/Work/Legacy" };
  plan.moves.push({ source: "S:/Work/10/extra.mkv", target: "S:/Work/Legacy/extra.mkv" });
  const legacyInfo = app.hooks.assignmentView(plan, legacy);
  assert.equal(legacyInfo.moves.length, 1);
  assert.equal(legacyInfo.moves[0].route_id, "route1");
});

test("large closed previews retain complete counts without materializing hidden file tables", () => {
  const plan = organizerPlanFixture();
  const app = setup(plan);
  assert.equal(plan.moves.length, 10000);
  assert.equal((app.view.innerHTML.match(/class="organizer-move-details"/g) || []).length, 40);
  assert.doesNotMatch(app.view.innerHTML, /class="organizer-move-table"/);
  assert.doesNotMatch(app.view.innerHTML, /class="organizer-directory-node"/);
  for (const assignment of plan.assignments) {
    const info = app.hooks.assignmentView(plan, assignment);
    assert.equal(info.moves.length, 250);
    assert.equal(info.targetDirectoryCount, 25);
  }
});

test("every file in a large directory can be reviewed in bounded pages without editing the plan", () => {
  const plan = organizerPlanFixture(1, 450);
  plan.moves.forEach((move, index) => { move.target = "S:/Work/Ready0/Disc/" + index + ".mkv"; });
  plan.moves[449].target = "S:/Work/Ready0/Disc/449<&>.mkv";
  const before = JSON.stringify(plan);
  const app = setup(plan);
  const info = app.hooks.assignmentView(plan, plan.assignments[0]);
  app.toggle(info, true);
  const focus = [];
  const body = { innerHTML: "", querySelector() { return { focus(options) { focus.push(options); } }; } };
  app.toggle(info, true, "ready0\\disc", body);
  assert.match(body.innerHTML, /第 1–200 条 \/ 共 450 条/);
  assert.match(body.innerHTML, /199\.mkv/);
  assert.doesNotMatch(body.innerHTML, /200\.mkv/);
  assert.match(body.innerHTML, /aria-label="Disc：下一页文件"/);
  app.page(info, "ready0\\disc", 1, body);
  assert.match(body.innerHTML, /第 201–400 条/);
  assert.match(body.innerHTML, /399\.mkv/);
  assert.doesNotMatch(body.innerHTML, /199\.mkv/);
  assert.equal((body.innerHTML.match(/<tr><td>/g) || []).length, 200);
  app.page(info, "ready0\\disc", 2, body);
  assert.match(body.innerHTML, /第 401–450 条/);
  assert.match(body.innerHTML, /449&lt;&amp;&gt;\.mkv/);
  assert.equal((body.innerHTML.match(/<tr><td>/g) || []).length, 50);
  assert.doesNotMatch(body.innerHTML, /449<&>/);
  app.toggle(info, false, "ready0\\disc", body);
  app.toggle(info, true, "ready0\\disc", body);
  assert.match(body.innerHTML, /第 401–450 条/, "reopening the directory retains its current file page");
  app.page(info, "ready0\\disc", 1, body);
  assert.match(body.innerHTML, /第 201–400 条/);
  assert.equal(focus.length, 3);
  assert.equal(focus.every((options) => options.preventScroll), true);
  assert.equal(JSON.stringify(plan), before);
  assert.equal(app.hooks.state.dirty, false);
});
