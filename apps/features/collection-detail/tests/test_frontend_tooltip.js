"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { loadFrontend } = require("../../../../scripts/benchmark-frontend.js");
const flush = () => new Promise((resolve) => setImmediate(resolve));

function setup() {
  const pending = [];
  let tooltip;
  const slot = { contains(button) { return !button.detached; } };
  const document = {
    getElementById(id) { return id === "collection-detail-link-index-slot" ? slot : null; },
    querySelectorAll() { return []; },
    body: { contains(element) { return element === tooltip; }, appendChild(element) { tooltip = element; } },
    createElement() { return { hidden: true, textContent: "", style: {}, classList: { toggle() {} },
      getBoundingClientRect() { return { width: 100, height: 40 }; } }; },
  };
  const app = loadFrontend("collection-detail",
    "{ show: showLinkTooltip, hide: hideLinkTooltip, move: placeLinkTooltip, release: releaseLinkIndexPayload, " +
    "configure: function(context) { featureCtx = context; activeSubtab = 'index'; featureActive = true; } }",
    { document, window: { innerWidth: 1200, innerHeight: 800 } });
  app.hooks.configure({ fetchJson() { return new Promise((resolve, reject) => pending.push({
    respond(target) { resolve({ res: { ok: true }, data: { ok: true, target_path: target, target_exists: true } }); }, reject,
  })); } });
  function button(name) {
    const attrs = { "data-link-shortcut-path": name };
    return { getAttribute(key) { return attrs[key] || null; }, setAttribute(key, value) { attrs[key] = value; },
      removeAttribute(key) { delete attrs[key]; }, querySelector() { return null; } };
  }
  return { ...app.hooks, pending, button, tooltip: () => tooltip };
}

test("an older shortcut resolution cannot overwrite the currently hovered target", async () => {
  const app = setup();
  const first = app.button("A.lnk");
  const second = app.button("B.lnk");
  app.show(first, { clientX: 10, clientY: 20 });
  app.show(second, { clientX: 200, clientY: 250 });
  app.pending[1].respond("S:/B");
  await flush();
  assert.equal(app.tooltip().textContent, "S:/B");
  const left = app.tooltip().style.left;
  app.pending[0].respond("S:/A");
  await flush();
  assert.equal(first.getAttribute("data-link-target-path"), "S:/A");
  assert.equal(app.tooltip().textContent, "S:/B");
  assert.equal(app.tooltip().style.left, left);
});

test("tooltip resolution preserves the current pointer position and stays hidden after leaving the index", async () => {
  const app = setup();
  app.show(app.button("Current.lnk"), { clientX: 10, clientY: 20 });
  app.move({ clientX: 300, clientY: 350 });
  app.pending[0].respond("S:/Current");
  await flush();
  assert.equal(app.tooltip().style.left, "314px");
  assert.equal(app.tooltip().style.top, "364px");
  app.show(app.button("Pending.lnk"), { clientX: 10, clientY: 20 });
  app.release();
  app.pending[1].reject(new Error("late failure"));
  await flush();
  assert.equal(app.tooltip().hidden, true);
  assert.notEqual(app.tooltip().textContent, "目标路径解析失败：late failure");
});
