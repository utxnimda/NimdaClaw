"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { withCommonRuntime } = require("./frontend_runtime_fixture");

function setup(stored = {}, failStorage = false) {
  const attributes = {};
  const changes = [];
  const values = { ...stored };
  const selectors = new Map();
  for (const id of ["theme-select", "font-select"]) {
    const listeners = new Set();
    selectors.set(id, {
      value: "", listeners,
      addEventListener(_type, callback) { listeners.add(callback); },
      removeEventListener(_type, callback) { listeners.delete(callback); },
      change(value) { this.value = value; [...listeners].forEach((callback) => callback()); },
    });
  }
  const document = {
    documentElement: { setAttribute(name, value) { attributes[name] = value; }, removeAttribute(name) { delete attributes[name]; } },
    getElementById(id) { return selectors.get(id) || null; },
  };
  const storage = {
    getItem(key) { if (failStorage) throw new Error("blocked"); return values[key] || null; },
    setItem(key, value) { if (failStorage) throw new Error("blocked"); values[key] = value; },
  };
  const window = { document };
  vm.runInNewContext(withCommonRuntime(fs.readFileSync(path.resolve(__dirname, "../frontend/src/appearance.js"), "utf8")), { window });
  const controller = window.NimdaAppearance.create({
    document, storage,
    onThemeChanged(value) { changes.push(["theme", value]); },
    onFontChanged(value) { changes.push(["font", value]); },
  });
  return { controller, attributes, selectors, changes, values };
}

test("stored appearance is applied on mount without notifying collection state", () => {
  const app = setup({ "jp-tv-browse-theme": "forest", "jp-tv-browse-font": "mono" });
  assert.deepEqual(app.attributes, {});
  app.controller.mount();
  assert.deepEqual(app.attributes, { "data-theme": "forest", "data-font": "mono" });
  assert.equal(app.selectors.get("theme-select").value, "forest");
  assert.equal(app.selectors.get("font-select").value, "mono");
  assert.deepEqual(app.changes, []);
});

test("invalid preferences and blocked browser storage use safe visible defaults", () => {
  for (const app of [setup({ "jp-tv-browse-theme": "invalid", "jp-tv-browse-font": "invalid" }), setup({}, true)]) {
    app.controller.mount();
    assert.deepEqual(app.attributes, { "data-font": "reference" });
    assert.equal(app.selectors.get("theme-select").value, "midnight");
    assert.equal(app.selectors.get("font-select").value, "reference");
    app.selectors.get("theme-select").change("paper");
    assert.equal(app.attributes["data-theme"], "paper");
  }
});

test("theme and font changes notify only their injected callbacks and persist validated choices", () => {
  const app = setup();
  app.controller.mount();
  app.selectors.get("theme-select").change("sakura");
  app.selectors.get("font-select").change("song");
  assert.deepEqual(app.changes, [["theme", "sakura"], ["font", "song"]]);
  assert.equal(app.values["jp-tv-browse-theme"], "sakura");
  assert.equal(app.values["jp-tv-browse-font"], "song");
  app.selectors.get("theme-select").change("not-a-theme");
  assert.equal(app.attributes["data-theme"], undefined);
  assert.deepEqual(app.changes[2], ["theme", "midnight"]);
});

test("repeated mounting does not duplicate listeners and disposal detaches preference controls", () => {
  const app = setup();
  app.controller.mount();
  app.controller.mount();
  assert.equal(app.selectors.get("theme-select").listeners.size, 1);
  app.selectors.get("theme-select").change("ocean");
  assert.equal(app.changes.length, 1);
  app.controller.dispose();
  app.controller.dispose();
  app.selectors.get("font-select").change("mono");
  assert.equal(app.changes.length, 1);
  assert.equal(app.selectors.get("font-select").listeners.size, 0);
  app.controller.mount();
  app.selectors.get("font-select").change("mono");
  assert.deepEqual(app.changes[1], ["font", "mono"]);
});

test("missing appearance controls still allow programmatic preferences", () => {
  const app = setup();
  app.selectors.clear();
  app.controller.mount();
  app.controller.applyTheme("contrast");
  app.controller.applyFont("yahei");
  assert.deepEqual(app.attributes, { "data-theme": "contrast", "data-font": "yahei" });
});

test("theme rules change palette only, never the component geometry or typography", () => {
  const css = fs.readFileSync(path.resolve(__dirname, "../frontend/styles/app.css"), "utf8");
  const rules = [...css.matchAll(/([^{}]*html\[data-theme=[^{}]+)\{([^{}]*)\}/g)];
  assert.ok(rules.length >= 8, "all built-in color palettes are inspected");
  const visualProperties = new Set(["color-scheme", "color", "background", "background-color", "border-color", "box-shadow"]);
  for (const [, selector, body] of rules) {
    for (const declaration of body.split(";")) {
      const match = declaration.trim().match(/^([\w-]+)\s*:/);
      if (!match) continue;
      const property = match[1];
      if (property.startsWith("--")) {
        assert.doesNotMatch(property, /(?:font|width|height|padding|margin|radius|display|position|grid|size)|-gap$/, selector);
      } else {
        assert.ok(visualProperties.has(property), selector + " changes " + property);
      }
    }
  }
});

test("appearance remains a compact tone/font control with stable public IDs", () => {
  const app = fs.readFileSync(path.resolve(__dirname, "../frontend/src/App.js"), "utf8");
  assert.match(app, /id="theme-select"[^>]+aria-label="界面色调"/);
  assert.match(app, /id="font-select"[^>]+aria-label="界面字体"/);
  assert.match(app, /只改变色调，页面布局保持一致/);
  assert.match(app, /class="app-brand"/);
});
