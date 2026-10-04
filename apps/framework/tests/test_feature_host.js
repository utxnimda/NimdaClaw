"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { withCommonRuntime } = require("./frontend_runtime_fixture");

function setup() {
  const elements = new Map();
  const calls = [];
  const nav = {
    children: [],
    appendChild(node) { this.children = this.children.filter((item) => item !== node); this.children.push(node); node.parentElement = this; },
  };
  for (const id of ["one", "two"]) {
    const listeners = new Set();
    const button = {
      textContent: "", attrs: {}, classes: {},
      classList: { toggle(name, enabled) { button.classes[name] = enabled; } },
      setAttribute(name, value) { this.attrs[name] = value; },
      addEventListener(_name, callback) { listeners.add(callback); },
      removeEventListener(_name, callback) { listeners.delete(callback); },
      click() { [...listeners].forEach((callback) => callback()); },
      listeners,
    };
    elements.set("tab-" + id, button);
    elements.set(id + "-view", { hidden: false });
    nav.appendChild(button);
  }
  const document = {
    getElementById(id) { return elements.get(id) || null; },
    querySelector(selector) { return selector === ".app-tabs" ? nav : null; },
    body: { attrs: {}, setAttribute(name, value) { this.attrs[name] = value; } },
  };
  const window = { document };
  vm.runInNewContext(withCommonRuntime(fs.readFileSync(path.resolve(__dirname, "../frontend/src/feature-host.js"), "utf8")), { window });
  const registry = window.NimdaFeatureHost.ensureRegistry(window);
  function feature(id, order) {
    return {
      id, order, label: "Feature " + id, tabId: "tab-" + id, viewId: id + "-view",
      init(context) { calls.push(["init", id, context]); },
      activate(context) { calls.push(["activate", id, context]); },
      deactivate(context) { calls.push(["deactivate", id, context]); },
      refreshAfterConfig(context) { calls.push(["refresh", id, context]); },
      dispose() { calls.push(["dispose", id]); },
    };
  }
  const one = feature("one", 10);
  const two = feature("two", 20);
  registry.register(one);
  registry.register(two);
  let context = { revision: 1 };
  const host = window.NimdaFeatureHost.create({
    document, registry, initialId: "one", getContext: () => context,
    onBeforeTabChange(previous, next) { calls.push(["before", previous, next]); },
    onActiveChanged(id) { calls.push(["active", id]); },
  });
  return { host, registry, window, elements, document, nav, calls, one, two,
    changeContext(value) { context = value; } };
}

test("host initializes registered views once and keeps the active tab accessible", () => {
  const app = setup();
  app.host.mount();
  app.host.mount();
  assert.equal(app.calls.filter((call) => call[0] === "init").length, 2);
  assert.equal(app.elements.get("tab-one").listeners.size, 1);
  assert.equal(app.elements.get("tab-one").attrs["aria-selected"], "true");
  assert.equal(app.elements.get("tab-two").attrs["aria-selected"], "false");
  assert.equal(app.elements.get("one-view").hidden, false);
  assert.equal(app.elements.get("two-view").hidden, true);
  assert.equal(app.document.body.attrs["data-active-tab"], "one");
});

test("switching tabs invokes callbacks with fresh context while preserving inactive feature state", () => {
  const app = setup();
  app.one.unsavedDraft = { name: "Keep draft" };
  app.host.mount();
  app.calls.length = 0;
  const context = { revision: 2 };
  app.changeContext(context);
  app.elements.get("tab-two").click();
  assert.deepEqual(app.calls, [
    ["before", "one", "two"], ["active", "two"], ["deactivate", "one", context], ["activate", "two", context],
  ]);
  assert.equal(app.host.getActiveId(), "two");
  assert.deepEqual(app.one.unsavedDraft, { name: "Keep draft" });
  app.elements.get("tab-two").click();
  assert.equal(app.calls.length, 4);
});

test("server labels and order update without mutating registered modules or their drafts", () => {
  const app = setup();
  app.host.mount();
  app.host.configure({ features: [{ id: "two", label: "Second first", order: 5 }, { id: "one", label: "First second", order: 30 }] });
  assert.deepEqual(app.nav.children, [app.elements.get("tab-two"), app.elements.get("tab-one")]);
  assert.equal(app.elements.get("tab-two").textContent, "Second first");
  assert.equal(app.one.label, "Feature one");
  assert.equal(app.one.order, 10);
  app.host.configure({ features: [{ id: "two", label: " ", order: "bad" }, null] });
  assert.equal(app.elements.get("tab-two").textContent, "Feature two");
  assert.deepEqual(app.nav.children, [app.elements.get("tab-one"), app.elements.get("tab-two")]);
});

test("configuration refresh affects only the active feature and uses the latest context", () => {
  const app = setup();
  app.host.mount();
  app.host.setActive("two");
  app.calls.length = 0;
  const context = { revision: 3 };
  app.changeContext(context);
  app.host.refresh();
  assert.deepEqual(app.calls, [["refresh", "two", context]]);
});

test("an unknown tab falls back to the first configured feature", () => {
  const app = setup();
  app.host.mount();
  app.host.configure({ features: [{ id: "two", order: 0 }] });
  app.host.setActive("missing");
  assert.equal(app.host.getActiveId(), "two");
  assert.equal(app.elements.get("two-view").hidden, false);
});

test("disposal removes host listeners and disposes each module once", () => {
  const app = setup();
  app.host.mount();
  app.host.dispose();
  app.host.dispose();
  assert.deepEqual(app.calls.filter((call) => call[0] === "dispose"), [["dispose", "one"], ["dispose", "two"]]);
  assert.equal(app.elements.get("tab-one").listeners.size, 0);
  app.calls.length = 0;
  app.elements.get("tab-two").click();
  app.host.setActive("two");
  app.host.refresh();
  app.host.configure({ features: [] });
  app.host.mount();
  assert.deepEqual(app.calls, []);
  assert.equal(app.host.getActiveId(), "one");
});

test("replacing a feature registration disposes the old module without duplicating its id", () => {
  const app = setup();
  const replacement = { id: "one", label: "Replacement" };
  app.registry.register(replacement);
  app.registry.register(replacement);
  assert.deepEqual(app.calls, [["dispose", "one"]]);
  assert.equal(app.registry.features.length, 2);
  assert.equal(app.registry.features[0], replacement);
  assert.equal(app.window.NimdaFeatureHost.ensureRegistry(app.window), app.registry);
});
