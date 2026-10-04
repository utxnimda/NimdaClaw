"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { performance } = require("node:perf_hooks");
const code = fs.readFileSync(path.join(__dirname, "../frontend/src/common/directory-browser.js"), "utf8");
const stylesheet = fs.readFileSync(path.join(__dirname, "../frontend/styles/directory-browser.css"), "utf8");
const file = (path, status = "unchanged") => ({ kind: "file", path, name: path.split("/").at(-1), children: [], fileCount: 1, status, changed: status !== "unchanged" });
const dir = (path, children = []) => ({ kind: "directory", path, name: path.split("/").at(-1), children, fileCount: children.reduce((total, node) => total + node.fileCount, 0), status: "unchanged" });
const fixtureTree = () => dir("U:/Root", [dir("U:/Root/A", [dir("U:/Root/A/Nested", [file("U:/Root/A/Nested/a.mkv")]), file("U:/Root/A/inside.ass")]), dir("U:/Root/B", [file("U:/Root/B/b.mkv")]), file("U:/Root/top.mkv", "moved")]);

function fixture(roots = [fixtureTree()], state = {}, extra = {}) {
  const document = { activeElement: null };
  const layout = { tree: 0, content: 0, ...extra.layout }, observers = [], resizeListeners = [];
  class Element {
    constructor(tag) { this.tagName = tag; this.ownerDocument = document; this.children = []; this.attributes = {}; this.listeners = {}; this.style = {}; this.textContent = ""; this.scrollTop = 0; this.scrollLeft = 0; }
    get firstChild() { return this.children[0]; }
    get clientHeight() { return this.className === "nimda-directory-tree-scroll" ? layout.tree : this.className === "nimda-directory-content-scroll" ? layout.content : 0; }
    getBoundingClientRect() { return { height: String(this.className).includes("nimda-directory-tree-row") || this.tagName === "thead" ? 26 : this.className === "nimda-directory-file-row" ? 28 : this.clientHeight }; }
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; return child; }
    setAttribute(key, value) { this.attributes[key] = String(value); }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    emit(type, extra) { for (const listener of this.listeners[type] || []) listener({ target: this, preventDefault() {}, ...extra }); }
    focus() { document.activeElement = this; }
    set innerHTML(_) { throw Error("Raw HTML rendering is forbidden"); }
  }
  document.createElement = tag => new Element(tag);
  const body = document.createElement("body"), untouched = document.createElement("div"), host = document.createElement("div"); body.appendChild(untouched); body.appendChild(host);
  const window = { NimdaCommon: { unrelated: true } };
  if (extra.resizeObserver !== false) window.ResizeObserver = class {
    constructor(callback) { this.callback = callback; this.targets = []; this.disconnects = 0; observers.push(this); }
    observe(node) { this.targets.push(node); }
    disconnect() { this.targets = []; this.disconnects++; }
  };
  window.addEventListener = (type, callback) => { if (type === "resize") resizeListeners.push(callback); };
  window.removeEventListener = (type, callback) => { if (type === "resize") resizeListeners.splice(resizeListeners.indexOf(callback), 1); };
  vm.runInNewContext(code, { window });
  const api = window.NimdaCommon.DirectoryBrowser, browser = api.mount(host, { roots, state, ...extra });
  const all = (root = host) => { const result = [], pending = [root]; while (pending.length) { const node = pending.pop(); result.push(node); pending.push(...node.children); } return result; };
  const control = key => all().find(node => node.attributes["data-directory-control"] === key);
  const treePaths = () => all().filter(node => node.attributes["data-directory-path"]).map(node => node.attributes["data-directory-path"]);
  const rows = () => all().filter(node => node.attributes["data-directory-entry"]);
  const css = name => all().find(node => String(node.className || "").split(" ").includes(name));
  const text = root => all(root).map(node => node.textContent).join(" ");
  const resize = next => { Object.assign(layout, next); for (const item of observers) item.callback(); for (const callback of resizeListeners) callback(); };
  return { api, browser, body, host, untouched, document, window, state, all, control, treePaths, rows, css, text, resize, observers, resizeListeners };
}

test("shared component preserves the common namespace and shows directories only on the left", () => {
  const f = fixture();
  assert.equal(f.window.NimdaCommon.unrelated, true);
  assert.deepEqual(f.treePaths().sort(), ["U:/Root", "U:/Root/A", "U:/Root/B"]);
  assert.equal(f.rows().length, 3);
  assert.equal(f.all().filter(node => node.tagName === "th").length, 4);
  assert.ok(f.text(f.css("nimda-directory-content")).includes("top.mkv"));
  assert.ok(!f.text(f.css("nimda-directory-tree")).includes("top.mkv"));
  assert.ok(!f.all().some(node => node.attributes["data-file-source"] || node.attributes["data-file-target"]));
});

test("root collapse persists and selection is separate from expansion", () => {
  const f = fixture();
  f.control("toggle:u:/root").emit("click");
  assert.deepEqual(f.treePaths(), ["U:/Root"]);
  f.control("select:u:/root").emit("click");
  assert.deepEqual(f.treePaths(), ["U:/Root"]);
  f.browser.update({ roots: [fixtureTree()] });
  assert.deepEqual(f.treePaths(), ["U:/Root"]);
  assert.equal(f.rows().length, 3, "collapsing tree does not hide current-directory contents");
  f.control("toggle:u:/root").emit("click");
  f.control("select:u:/root/a").emit("click");
  assert.equal(f.state.selectedPath, "U:/Root/A");
  assert.ok(!f.treePaths().includes("U:/Root/A/Nested"));
  assert.equal(f.rows().length, 2);
  const selected = f.state.selectedPath;
  f.control("toggle:u:/root/a").emit("click");
  assert.equal(f.state.selectedPath, selected);
  assert.ok(f.treePaths().includes("U:/Root/A/Nested"));
});

test("folder entry, current path and up navigation operate without opening files", () => {
  const f = fixture();
  assert.equal(f.control("up").disabled, true);
  f.control("enter:u:/root/a").emit("click");
  assert.equal(f.state.selectedPath, "U:/Root/A");
  assert.equal(f.css("nimda-directory-current-path").textContent, "U:/Root/A");
  assert.equal(f.control("up").disabled, false);
  const row = f.rows().find(node => node.attributes["data-directory-entry"] === "U:/Root/A/inside.ass");
  assert.equal(f.all(row).filter(node => node.tagName === "button").length, 0);
  f.control("up").emit("click");
  assert.equal(f.state.selectedPath, "U:/Root");
  assert.equal(f.treePaths().includes("U:/Root/A/Nested"), false, "entering and returning must not autoexpand branches");
});

test("expand and collapse all include roots and never change the current directory", () => {
  const f = fixture();
  f.control("select:u:/root/a").emit("click");
  f.control("expand-all").emit("click");
  assert.deepEqual(f.treePaths().sort(), ["U:/Root", "U:/Root/A", "U:/Root/A/Nested", "U:/Root/B"]);
  f.control("collapse-all").emit("click");
  assert.deepEqual(f.treePaths(), ["U:/Root"]);
  assert.equal(f.state.selectedPath, "U:/Root/A");
  assert.equal(f.rows().length, 2);
});

test("tree pagination repeats ancestor context and makes every directory reachable once", () => {
  const root = dir("U:/Root", [dir("U:/Root/A", Array.from({ length: 15 }, (_, index) => dir("U:/Root/A/" + index))), dir("U:/Root/B")]);
  const f = fixture([root], {}, { branchPageSize: 5 });
  f.control("toggle:u:/root/a").emit("click");
  assert.equal(f.treePaths().length, 5);
  const payload = [];
  while (true) {
    assert.ok(f.treePaths().includes("U:/Root"));
    assert.ok(f.treePaths().length <= 5);
    for (const row of f.all().filter(node => node.attributes["data-directory-path"] && !node.attributes["data-directory-context"])) payload.push(row.attributes["data-directory-path"]);
    if (f.control("tree-next").disabled) break;
    f.control("tree-next").emit("click");
  }
  assert.equal(payload.length, 18); assert.equal(new Set(payload).size, 18);
  assert.ok(payload.includes("U:/Root/A")); assert.ok(payload.includes("U:/Root/B"));
  const lastPaths = f.treePaths();
  f.browser.update({ roots: [root] });
  assert.deepEqual(f.treePaths(), lastPaths);
});

test("right side pagination is bounded, complete and resets only on directory changes", () => {
  const root = dir("U:/Root", [...Array.from({ length: 215 }, (_, index) => file("U:/Root/" + index + ".mkv")), dir("U:/Root/Folder")]);
  const f = fixture([root], {}, { pageSize: 100 });
  assert.equal(f.rows().length, 10); assert.equal(f.control("previous").disabled, true);
  f.control("next").emit("click"); assert.equal(f.rows().length, 10); assert.equal(f.state.page, 1);
  f.control("toggle:u:/root").emit("click"); assert.equal(f.state.page, 1);
  while (!f.control("next").disabled) f.control("next").emit("click");
  assert.equal(f.rows().length, 6); assert.equal(f.control("next").disabled, true);
  f.control("enter:u:/root/folder").emit("click"); assert.equal(f.state.page, 0); assert.equal(f.rows().length, 0);
  f.control("up").emit("click"); assert.equal(f.state.page, 0);
});

test("selection and expansion survive remounts without restoring obsolete internal scroll", () => {
  const state = {}, f = fixture(undefined, state);
  f.control("toggle:u:/root/a").emit("click"); f.control("select:u:/root/a").emit("click");
  let tree = f.css("nimda-directory-tree-scroll"), content = f.css("nimda-directory-content-scroll");
  tree.scrollTop = 120; tree.scrollLeft = 19; content.scrollTop = 85; content.scrollLeft = 7;
  tree.emit("scroll"); content.emit("scroll");
  f.control("toggle:u:/root").focus(); f.control("toggle:u:/root").emit("click");
  assert.equal(f.css("nimda-directory-tree-scroll").scrollTop, 0);
  assert.equal(f.css("nimda-directory-content-scroll").scrollTop, 0);
  assert.equal(f.document.activeElement, f.control("toggle:u:/root"));
  f.browser.update({ roots: [fixtureTree()] });
  assert.equal(state.selectedPath, "U:/Root/A"); assert.equal(f.css("nimda-directory-tree-scroll").scrollLeft, 0);
  assert.equal(f.css("nimda-directory-content-scroll").scrollLeft, 0);
  f.browser.destroy(); assert.equal(f.host.children.length, 0);
  const remount = f.api.mount(f.host, { roots: [fixtureTree()], state });
  assert.equal(f.css("nimda-directory-tree-scroll").scrollTop, 0);
  assert.equal(f.css("nimda-directory-content-scroll").scrollTop, 0);
  assert.deepEqual(f.treePaths(), ["U:/Root"]);
  assert.equal(state.selectedPath, "U:/Root/A"); remount.destroy();
});

test("component actions do not rebuild outside the host and destroy is idempotent", () => {
  const f = fixture(), outside = f.untouched;
  f.control("expand-all").emit("click"); f.control("collapse-all").emit("click");
  assert.equal(f.body.children[0], outside); assert.equal(outside.parentNode, f.body);
  const oldControl = f.control("expand-all");
  f.browser.destroy(); f.browser.destroy(); f.browser.update({ title: "ignored" }); oldControl.emit("click");
  assert.equal(f.host.children.length, 0); assert.equal(f.body.children[0], outside);
});

test("untrusted names and paths are only rendered as literal text and attributes", () => {
  const malicious = '<img src=x onerror="alert(1)">';
  const root = dir("U:/" + malicious, [dir("U:/" + malicious + "/<script>"), file("U:/" + malicious + "/a<&\".mkv")]);
  const f = fixture([root]);
  assert.ok(f.all().some(node => node.textContent === malicious));
  assert.equal(f.all().some(node => ["img", "script"].includes(node.tagName)), false);
  assert.ok(f.text(f.css("nimda-directory-content")).includes('a<&".mkv'));
  assert.ok(!code.includes("innerHTML"));
});

test("12,000-file and 12,000-folder plans retain bounded DOM and navigable content", () => {
  const manyFiles = dir("U:/Files", Array.from({ length: 12000 }, (_, index) => file("U:/Files/" + index + ".mkv")));
  let started = performance.now(); const files = fixture([manyFiles]);
  assert.equal(files.treePaths().length, 1); assert.equal(files.rows().length, 10);
  assert.ok(files.all().length < 1100); assert.ok(performance.now() - started < 2000);
  const manyFolders = dir("U:/Folders", Array.from({ length: 12000 }, (_, index) => dir("U:/Folders/" + index)));
  started = performance.now(); const folders = fixture([manyFolders], {}, { branchPageSize: 200, maxTreeNodes: 220 });
  folders.control("expand-all").emit("click"); assert.equal(folders.treePaths().length, 10);
  folders.control("tree-next").emit("click");
  assert.equal(folders.treePaths().length, 10); assert.ok(folders.treePaths().includes("U:/Folders"));
  assert.equal(folders.control("tree-next").disabled, false);
  folders.control("next").emit("click"); assert.equal(folders.rows().length, 10);
  assert.equal(folders.state.page, 1); assert.ok(performance.now() - started < 3000);
});

test("multiple roots page completely and collapse independently", () => {
  const roots = Array.from({ length: 8 }, (_, index) => dir("U:/" + index, [dir("U:/" + index + "/child")]));
  const f = fixture(roots, {}, { branchPageSize: 3 });
  assert.equal(f.treePaths().length, 3);
  f.control("toggle:u:/0").emit("click");
  assert.ok(!f.treePaths().includes("U:/0/child")); assert.ok(f.treePaths().includes("U:/1/child"));
  const visited = new Set();
  while (true) { f.treePaths().forEach(value => visited.add(value)); if (f.control("tree-next").disabled) break; f.control("tree-next").emit("click"); }
  assert.equal(visited.size, 15); assert.ok(visited.has("U:/7/child"));
});

test("missing selection falls back safely and a new state object is honored", () => {
  const f = fixture(); f.control("select:u:/root/a").emit("click");
  f.browser.update({ roots: [dir("V:/New")] });
  assert.equal(f.state.selectedPath, "V:/New");
  const nextState = { selectedPath: "V:/New", expandedPaths: [], treeScrollTop: 22 };
  f.browser.update({ state: nextState });
  assert.equal(f.css("nimda-directory-tree-scroll").scrollTop, 0);
  f.browser.update({ roots: [] }); assert.equal(nextState.selectedPath, ""); assert.equal(f.rows().length, 0);
  assert.equal(f.control("up").disabled, true);
});

test("available heights determine row capacity including table header and resize preserves item position", () => {
  const root = dir("U:/Root", Array.from({ length: 80 }, (_, index) => dir("U:/Root/" + index)));
  const f = fixture([root], {}, { layout: { tree: 261, content: 307 } });
  assert.equal(f.treePaths().length, 10); assert.equal(f.rows().length, 10);
  f.control("tree-next").emit("click"); f.control("next").emit("click");
  const oldTreeStart = f.state.treePageStart;
  assert.equal(f.state.page, 1);
  f.resize({ tree: 131, content: 167 });
  assert.equal(f.treePaths().length, 5); assert.equal(f.rows().length, 5);
  assert.ok(f.treePaths().includes(oldTreeStart));
  assert.equal(f.state.page, 2); assert.ok(f.rows().some(node => node.attributes["data-directory-entry"] === "U:/Root/10"));
  assert.ok(f.treePaths().includes("U:/Root"));
  assert.equal(f.css("nimda-directory-tree-scroll").attributes["data-directory-page-capacity"], "5");
  assert.equal(f.css("nimda-directory-content-scroll").attributes["data-directory-page-capacity"], "5");
  f.resize({ tree: 209, content: 251 });
  assert.equal(f.rows().length, 8); assert.ok(f.rows().some(node => node.attributes["data-directory-entry"] === "U:/Root/10"));
});

test("very deep tree pages compress ancestors with full path context and never lose payload rows", () => {
  const root = dir("U:/Root"); let parent = root;
  for (let index = 0; index < 45; index++) { const child = dir(parent.path + "/" + index); parent.children.push(child); parent = child; }
  const f = fixture([root], {}, { branchPageSize: 4 }); f.control("expand-all").emit("click");
  const payload = [];
  while (true) {
    assert.ok(f.treePaths().length <= 4); assert.ok(f.treePaths().includes("U:/Root"));
    f.all().filter(node => node.attributes["data-directory-path"] && !node.attributes["data-directory-context"]).forEach(node => payload.push(node.attributes["data-directory-path"]));
    if (f.control("tree-next").disabled) break;
    f.control("tree-next").emit("click");
  }
  assert.equal(payload.length, 46); assert.equal(new Set(payload).size, 46);
  assert.ok(f.css("nimda-directory-tree-context").title.includes("/40"));
  assert.ok(f.all().some(node => node.textContent.startsWith("… / ") && node.title.includes("U:/Root/0/1")));
});

test("ResizeObserver tracks current panes and is disconnected on destroy", () => {
  const f = fixture([dir("U:/Root", Array.from({ length: 50 }, (_, index) => file("U:/Root/" + index)))], {}, { layout: { tree: 157, content: 195 } });
  assert.equal(f.rows().length, 6); assert.equal(f.observers.length, 1);
  const observer = f.observers[0]; assert.equal(observer.targets.length, 2);
  assert.ok(observer.targets.includes(f.css("nimda-directory-tree-scroll")));
  f.control("next").emit("click"); assert.equal(observer.targets.length, 2);
  f.browser.destroy(); assert.equal(observer.targets.length, 0);
  observer.callback(); assert.equal(f.host.children.length, 0);
  f.browser.destroy(); assert.equal(observer.targets.length, 0);
});

test("hidden panes retain their last measured capacity and page until visible again", () => {
  const f = fixture([dir("U:/Root", Array.from({ length: 41 }, (_, index) => file("U:/Root/" + index)))], {}, { layout: { tree: 209, content: 251 } });
  f.control("next").emit("click"); f.control("next").emit("click");
  const paths = f.rows().map(node => node.attributes["data-directory-entry"]);
  f.resize({ tree: 0, content: 0 }); assert.equal(f.state.page, 2); assert.equal(f.rows().length, 8);
  f.resize({ tree: 209, content: 251 }); assert.deepEqual(f.rows().map(node => node.attributes["data-directory-entry"]), paths);
});

test("measured page positions survive destroying and remounting with the same state", () => {
  const root = dir("U:/Root", Array.from({ length: 80 }, (_, index) => dir("U:/Root/" + index)));
  const f = fixture([root], {}, { layout: { tree: 209, content: 251 } });
  for (let index = 0; index < 2; index++) { f.control("tree-next").emit("click"); f.control("next").emit("click"); }
  const beforeTree = f.treePaths(), beforeRows = f.rows().map(node => node.attributes["data-directory-entry"]);
  assert.equal(f.state.page, 2); assert.equal(f.state.treePage, 2);
  f.browser.destroy(); const remounted = f.api.mount(f.host, { roots: [root], state: f.state });
  assert.equal(f.state.page, 2); assert.equal(f.state.treePage, 2);
  assert.deepEqual(f.treePaths(), beforeTree); assert.deepEqual(f.rows().map(node => node.attributes["data-directory-entry"]), beforeRows);
  remounted.destroy();
});

test("window resize fallback works and removes its listener on destroy", () => {
  const f = fixture([dir("U:/Root", Array.from({ length: 30 }, (_, index) => file("U:/Root/" + index)))], {}, { resizeObserver: false, layout: { tree: 131, content: 167 } });
  assert.equal(f.rows().length, 5); assert.equal(f.resizeListeners.length, 1);
  f.resize({ content: 111 }); assert.equal(f.rows().length, 3);
  f.browser.destroy(); assert.equal(f.resizeListeners.length, 0);
});

test("long names are literal single-line labels with full titles and panes cannot vertically scroll", () => {
  const name = "非常长的文件名".repeat(80) + ".mkv", f = fixture([dir("U:/Root", [file("U:/Root/" + name)])]);
  const label = f.all().find(node => node.className === "nimda-directory-entry-text");
  assert.equal(label.textContent, name); assert.equal(label.title, name);
  assert.match(stylesheet, /\.nimda-directory-tree-scroll, \.nimda-directory-content-scroll[^}]*overflow: visible/);
  assert.doesNotMatch(stylesheet, /overflow(?:-y)?:\s*(auto|scroll)/);
  assert.match(stylesheet, /\.nimda-directory-entry-text[^}]*text-overflow: ellipsis[^}]*white-space: nowrap/);
});
