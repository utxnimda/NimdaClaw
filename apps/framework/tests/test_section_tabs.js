"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const code = fs.readFileSync(path.join(__dirname, "../frontend/src/common/section-tabs.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "../frontend/styles/section-tabs.css"), "utf8");

function environment() {
  const document = { activeElement: null };
  class Element {
    constructor(tag) { this.tagName = tag; this.ownerDocument = document; this.children = []; this.attributes = {}; this.listeners = {}; this._text = ""; this.hidden = false; this.id = ""; }
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; return child; }
    setAttribute(key, value) { this.attributes[key] = String(value); if (key === "id") this.id = String(value); }
    getAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attributes, key) ? this.attributes[key] : null; }
    removeAttribute(key) { delete this.attributes[key]; if (key === "id") this.id = ""; }
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    removeEventListener(type, callback) { this.listeners[type] = (this.listeners[type] || []).filter(value => value !== callback); }
    emit(type, extra = {}) { const event = { target: this, prevented: false, preventDefault() { this.prevented = true; }, ...extra }; for (const callback of this.listeners[type] || []) callback(event); return event; }
    focus() { document.activeElement = this; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
    set textContent(value) { this._text = String(value); this.children = []; }
    set innerHTML(_) { throw Error("HTML rendering is forbidden"); }
  }
  document.createElement = tag => new Element(tag);
  document.body = document.createElement("body");
  const all = (root = document.body) => { const pending = [root], result = []; while (pending.length) { const node = pending.pop(); result.push(node); pending.push(...node.children); } return result; };
  document.getElementById = id => all().find(node => node.id === id) || null;
  const window = { NimdaCommon: { existing: true } }; vm.runInNewContext(code, { window });
  function mount(settings = {}) {
    const host = document.createElement("nav"), parent = document.createElement("section"); document.body.appendChild(host); document.body.appendChild(parent);
    const panels = ["preview", "db", "details"].map(id => {
      const panel = document.createElement("section"), input = document.createElement("input"); input.value = id + " draft"; panel.appendChild(input); parent.appendChild(panel); return panel;
    });
    if (settings.prepare) settings.prepare(panels);
    const items = panels.map((panel, index) => ({ id: ["preview", "db", "details"][index], label: ["目录预览", "作品资料", "移动明细"][index], panel }));
    const state = settings.state || {}, changes = [];
    const controller = window.NimdaCommon.SectionTabs.mount(host, { items, state, defaultId: "preview", label: "当前目录功能", onChange: id => changes.push(id), ...settings });
    const tab = id => all(host).find(node => node.getAttribute("data-section-tab") === id);
    return { host, parent, panels, items, state, changes, controller, tab };
  }
  return { document, window, all, mount };
}

test("mount exposes shared tabs with correct ARIA and does not recreate panel contents", () => {
  const env = environment(), f = env.mount();
  assert.equal(env.window.NimdaCommon.existing, true);
  assert.equal(f.host.children[0].getAttribute("role"), "tablist");
  assert.equal(f.host.children[0].getAttribute("aria-label"), "当前目录功能");
  assert.equal(f.state.activeId, "preview"); assert.deepEqual(f.changes, []);
  f.items.forEach((item, index) => {
    const button = f.tab(item.id);
    assert.equal(button.getAttribute("role"), "tab"); assert.equal(button.getAttribute("aria-controls"), item.panel.id);
    assert.equal(item.panel.getAttribute("role"), "tabpanel"); assert.equal(item.panel.getAttribute("aria-labelledby"), button.id);
    assert.equal(button.getAttribute("aria-selected"), String(index === 0)); assert.equal(button.tabIndex, index === 0 ? 0 : -1);
    assert.equal(item.panel.hidden, index !== 0); assert.equal(item.panel.children[0].value, item.id + " draft");
  });
  assert.match(css, /\[data-section-tab-panel\]\[hidden\][^}]*display:\s*none\s*!important/);
});

test("selection persists in caller state and repeated selections never duplicate callbacks", () => {
  const env = environment(), state = { activeId: "db" }, f = env.mount({ state });
  const originalInput = f.panels[1].children[0]; originalInput.value = "unsaved typed data";
  assert.equal(f.panels[1].hidden, false); assert.deepEqual(f.changes, []);
  f.tab("details").emit("click"); f.tab("details").emit("click");
  assert.deepEqual(f.changes, ["details"]); assert.equal(state.activeId, "details");
  assert.equal(f.controller.select("details"), true); assert.deepEqual(f.changes, ["details"]);
  f.controller.select("db", { focus: true }); assert.deepEqual(f.changes, ["details", "db"]);
  assert.equal(env.document.activeElement, f.tab("db")); assert.equal(f.panels[1].children[0], originalInput);
  assert.equal(originalInput.value, "unsaved typed data");
  f.controller.destroy(); const next = env.mount({ state }); assert.equal(next.panels[1].hidden, false); assert.equal(state.activeId, "db");
});

test("ArrowLeft and ArrowRight wrap, Home and End jump and unhandled keys remain untouched", () => {
  const env = environment(), f = env.mount();
  assert.equal(f.tab("preview").emit("keydown", { key: "ArrowLeft" }).prevented, true);
  assert.equal(f.state.activeId, "details"); assert.equal(env.document.activeElement, f.tab("details"));
  f.tab("details").emit("keydown", { key: "ArrowRight" }); assert.equal(f.state.activeId, "preview");
  f.tab("preview").emit("keydown", { key: "End" }); assert.equal(f.state.activeId, "details");
  f.tab("details").emit("keydown", { key: "Home" }); assert.equal(f.state.activeId, "preview");
  const prior = f.changes.length;
  f.tab("preview").emit("keydown", { key: "Home" }); assert.equal(f.changes.length, prior);
  assert.equal(f.tab("preview").emit("keydown", { key: "ArrowDown" }).prevented, false);
  assert.equal(f.tab("preview").emit("keydown", { key: "ArrowRight", ctrlKey: true }).prevented, false);
  assert.equal(f.state.activeId, "preview");
});

test("badges are separate literal spans and keep a stable accessible tab label", () => {
  const env = environment(), f = env.mount(), tab = f.tab("db");
  f.controller.setBadge("db", 0);
  assert.equal(tab.children[1].className, "nimda-section-tab-badge"); assert.equal(tab.children[1].textContent, "0"); assert.equal(tab.children[1].hidden, false);
  assert.equal(tab.textContent, "作品资料0"); assert.equal(tab.getAttribute("aria-label"), "作品资料"); assert.equal(tab.getAttribute("aria-description"), "0");
  f.controller.setBadge("db", '<img onerror="bad()">');
  assert.equal(tab.children[1].textContent, '<img onerror="bad()">'); assert.equal(env.all().some(node => node.tagName === "img"), false);
  f.controller.setBadge("db", null); assert.equal(tab.children[1].hidden, true); assert.equal(tab.getAttribute("aria-description"), null);
  assert.equal(f.controller.setBadge("missing", "1"), false); assert.deepEqual(f.changes, []);
});

test("instance IDs are unique while already named panels keep their valid IDs", () => {
  const env = environment(), first = env.mount({ prepare: panels => panels[0].setAttribute("id", "existing-preview") }), second = env.mount();
  assert.equal(first.panels[0].id, "existing-preview");
  const ids = env.all().map(node => node.id).filter(Boolean); assert.equal(new Set(ids).size, ids.length);
  second.controller.select("db"); assert.equal(first.state.activeId, "preview"); assert.equal(second.state.activeId, "db");
  assert.notEqual(first.tab("db").getAttribute("aria-controls"), second.tab("db").getAttribute("aria-controls"));
});

test("detached mounts also avoid caller IDs that collide with other panels or generated tabs", () => {
  const env = environment(), firstHost = env.document.createElement("nav"), secondHost = env.document.createElement("nav");
  const firstPanel = env.document.createElement("section"), secondPanel = env.document.createElement("section"), thirdPanel = env.document.createElement("section");
  firstPanel.setAttribute("id", "shared-detached"); secondPanel.setAttribute("id", "shared-detached"); thirdPanel.setAttribute("id", "nimda-section-tabs-2-tab-0");
  const first = env.window.NimdaCommon.SectionTabs.mount(firstHost, { items: [{ id: "one", label: "One", panel: firstPanel }] });
  const second = env.window.NimdaCommon.SectionTabs.mount(secondHost, { items: [{ id: "two", label: "Two", panel: secondPanel }, { id: "three", label: "Three", panel: thirdPanel }] });
  const ids = [...env.all(firstHost), ...env.all(secondHost), firstPanel, secondPanel, thirdPanel].map(node => node.id).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length); assert.equal(firstPanel.id, "shared-detached"); assert.notEqual(secondPanel.id, "shared-detached");
  second.destroy(); first.destroy(); assert.equal(secondPanel.id, "shared-detached");
});

test("invalid active and default IDs fall back safely and invalid selections do nothing", () => {
  const env = environment(), first = env.mount({ state: { activeId: "unknown" }, defaultId: "db" });
  assert.equal(first.state.activeId, "db");
  const f = env.mount({ state: { activeId: "missing" }, defaultId: "also-missing" });
  assert.equal(f.state.activeId, "preview"); assert.equal(f.controller.select("missing", { focus: true }), false);
  assert.equal(f.state.activeId, "preview"); assert.deepEqual(f.changes, []);
  const empty = env.mount({ items: [], state: { activeId: "old" } }); assert.equal(empty.state.activeId, ""); assert.equal(empty.controller.select("old"), false);
});

test("destroy removes listeners and its own DOM, restores panel attributes and leaves caller content intact", () => {
  const env = environment(), f = env.mount({ prepare: panels => {
    panels[0].setAttribute("id", "original"); panels[0].setAttribute("role", "region"); panels[0].setAttribute("aria-labelledby", "old-title"); panels[0].hidden = true;
  } });
  const button = f.tab("db"), input = f.panels[0].children[0], untouched = env.document.createElement("span"); f.host.appendChild(untouched);
  f.controller.select("db"); f.panels[2].setAttribute("role", "article");
  f.controller.destroy(); f.controller.destroy();
  assert.deepEqual(f.host.children, [untouched]); assert.equal(f.parent.children.length, 3); assert.equal(f.panels[0].children[0], input);
  assert.equal(f.panels[0].id, "original"); assert.equal(f.panels[0].getAttribute("role"), "region"); assert.equal(f.panels[0].getAttribute("aria-labelledby"), "old-title"); assert.equal(f.panels[0].hidden, true);
  assert.equal(f.panels[1].id, ""); assert.equal(f.panels[1].getAttribute("role"), null); assert.equal(f.panels[1].getAttribute("data-section-tab-panel"), null);
  assert.equal(f.panels[2].getAttribute("role"), "article", "external attribute edits must not be overwritten");
  assert.equal(button.listeners.click.length, 0); assert.equal(button.listeners.keydown.length, 0);
  button.emit("click"); assert.deepEqual(f.changes, ["db"]); assert.equal(f.controller.select("preview"), false); assert.equal(f.controller.setBadge("db", "2"), false);
});

test("malformed and duplicate items are ignored without adding orphan tabs", () => {
  const env = environment(), fixture = env.mount(); fixture.controller.destroy();
  const state = {}, controller = env.window.NimdaCommon.SectionTabs.mount(fixture.host, { state, items: [null, {}, { id: "a", label: "A", panel: fixture.panels[0] }, { id: "a", panel: fixture.panels[1] }, { id: "b", panel: fixture.panels[0] }] });
  assert.equal(state.activeId, "a"); assert.equal(env.all(fixture.host).filter(node => node.getAttribute("role") === "tab").length, 1);
  controller.destroy();
});
