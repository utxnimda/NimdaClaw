"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const FRONTEND = path.resolve(__dirname, "../frontend/index.js");
const RESOURCE_SLOT = "collection-detail-resource-slot";
const INDEX_SLOT = "collection-detail-link-index-slot";
const flush = () => new Promise((resolve) => setImmediate(resolve));

function setup(options = {}) {
  const listeners = new Map();
  const slots = new Map([RESOURCE_SLOT, INDEX_SLOT].map((id) => [id, {
    innerHTML: "",
    contains(node) { return node.owner === id; },
    querySelector() { return null; },
  }]));
  const requests = [];
  const statuses = [];
  const document = {
    getElementById(id) { return slots.get(id) || null; },
    querySelectorAll() { return []; },
    addEventListener(type, callback) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(callback);
    },
    removeEventListener(type, callback) {
      listeners.set(type, (listeners.get(type) || []).filter((value) => value !== callback));
    },
    body: { classList: { add() {}, remove() {} } },
  };
  const window = { document };
  const localStorage = {
    getItem() {
      if (options.storageDenied) throw new Error("Storage access denied");
      return null;
    },
    setItem() {
      if (options.storageDenied) throw new Error("Storage quota exceeded");
    },
    removeItem() {},
  };
  const context = {
    fetchJson(url, options) {
      return new Promise((resolve, reject) => requests.push({
        url, options,
        respond(data) { resolve({ res: { ok: true, status: 200 }, data }); },
        reject,
      }));
    },
    setStatus(message, error) { statuses.push({ message, error }); },
  };
  vm.runInNewContext(fs.readFileSync(FRONTEND, "utf8"), {
    window, document, localStorage, URLSearchParams, console,
  }, { filename: FRONTEND });
  const feature = window.JpTvBrowseFeatureRegistry.features[0];
  feature.init(context);

  function emit(type, attributes, owner = RESOURCE_SLOT, value = "") {
    const target = {
      owner, value,
      getAttribute(name) { return attributes[name] ?? null; },
      matches(selector) { return Object.hasOwn(attributes, selector.slice(1, -1)); },
      closest(selector) { return this.matches(selector) ? this : null; },
    };
    for (const callback of listeners.get(type) || []) callback({ target, preventDefault() {} });
  }
  return {
    requests, statuses, feature, context,
    html: (id = RESOURCE_SLOT) => slots.get(id).innerHTML,
    tab(name) { emit("click", { "data-collection-detail-subtab": name }, ""); },
    action(name) { emit("click", { "data-resource-action": name }); },
    reloadIndex() { emit("click", { "data-link-index-action": "reload" }, INDEX_SLOT); },
    search(query) {
      emit("input", { "data-resource-tree-search-input": "" }, RESOURCE_SLOT, query);
      emit("click", { "data-resource-action": "search-tree" });
    },
    select(relpath) { emit("click", { "data-resource-tree-select": relpath }); },
    toggle(relpath, collapsed) {
      const row = { getAttribute() { return relpath; } };
      const wrap = { querySelector() { return row; } };
      const target = { owner: RESOURCE_SLOT,
        classList: { contains() { return collapsed; } },
        closest(selector) {
          if (selector === "[data-resource-tree-toggle]") return this;
          if (selector === ".resource-tree-folder-wrap") return wrap;
          return null;
        },
      };
      for (const callback of listeners.get("click") || []) callback({ target, preventDefault() {} });
    },
  };
}

function folder(name, childrenLoaded = true) {
  return { type: "folder", name, relpath: name, path: "S:\\" + name,
    children_loaded: childrenLoaded, children: [], files: [] };
}

function resourceData(name, extra = {}) {
  return { ok: true, scanned_at: name, config: { resource_roots: ["S:\\"] },
    tree: { type: "folder", name: "资源库", relpath: "", children_loaded: true,
      children: [folder(name)] }, ...extra };
}

test("collection detail initializes even when browser preference storage is unavailable", () => {
  const app = setup({ storageDenied: true });
  assert.match(app.html(), /资源库目录/);
  app.tab("index");
  assert.equal(app.requests.length, 1);
});

test("resource scanning uses an explicit JSON POST because it writes the scan cache", async () => {
  const app = setup();
  app.tab("resource");
  app.requests[0].respond(resourceData("cached-library"));
  await flush();
  app.action("scan");
  const scan = app.requests[1];
  assert.equal(scan.url, "/api/collection-detail/resource-libraries/scan");
  assert.equal(scan.options.method, "POST");
  assert.equal(scan.options.headers["Content-Type"], "application/json; charset=utf-8");
  assert.deepEqual(JSON.parse(scan.options.body), {});
  scan.respond(resourceData("fresh-library"));
  await flush();
  assert.match(app.html(), /fresh-library/);
});

test("collapsed resource trees omit hidden descendants and reveal one requested level", async () => {
  const app = setup();
  const parent = folder("root:0");
  const child = folder("root:0/child");
  child.children = [folder("root:0/child/grandchild")];
  parent.children = [child];
  app.tab("resource");
  app.requests[0].respond({ ok: true, tree: { type: "folder", relpath: "", children_loaded: true, children: [parent] } });
  await flush();
  app.action("collapse-tree");
  assert.doesNotMatch(app.html(), /data-resource-tree-select="root:0\/child"/);
  assert.doesNotMatch(app.html(), /data-resource-tree-select="root:0\/child\/grandchild"/);
  app.toggle("root:0", true);
  assert.match(app.html(), /data-resource-tree-select="root:0\/child"/);
  assert.doesNotMatch(app.html(), /data-resource-tree-select="root:0\/child\/grandchild"/);
  app.select("root:0/child");
  assert.match(app.html(), /data-resource-tree-select="root:0\/child\/grandchild"/);
  app.toggle("root:0/child", true);
  assert.match(app.html(), /resource-tree-folder-row[^>]+data-resource-tree-select="root:0\/child\/grandchild"/);
  app.select("");
  app.toggle("root:0", false);
  assert.doesNotMatch(app.html(), /data-resource-tree-select="root:0\/child"/);
  app.toggle("root:0", true);
  assert.match(app.html(), /resource-tree-folder-row[^>]+data-resource-tree-select="root:0\/child\/grandchild"/);
  assert.equal(app.requests.length, 1, "expanding loaded folders must not make redundant requests");
});

test("older index reloads cannot replace the most recent visible index", async () => {
  const app = setup();
  app.tab("index");
  app.reloadIndex();
  assert.equal(app.requests.length, 2);
  app.requests[1].respond({ ok: true, plan_summary: { total: 222 } });
  await flush();
  assert.match(app.html(INDEX_SLOT), /索引 222 项/);
  app.requests[0].respond({ ok: true, plan_summary: { total: 111 } });
  await flush();
  assert.match(app.html(INDEX_SLOT), /索引 222 项/);
  assert.doesNotMatch(app.html(INDEX_SLOT), /索引 111 项/);
});

test("search result folding works independently and clearing search restores ordinary folding", async () => {
  const app = setup();
  const parent = folder("root:0");
  parent.children = [folder("root:0/matched")];
  const data = { ok: true, tree: { type: "folder", relpath: "", children_loaded: true, children: [parent] } };
  const childRow = /resource-tree-folder-row[^>]+data-resource-tree-select="root:0\/matched"/;
  app.tab("resource");
  app.requests[0].respond(data);
  await flush();
  app.action("collapse-tree");
  assert.doesNotMatch(app.html(), childRow);
  app.search("matched");
  app.requests[1].respond({ ...data, query: "matched" });
  await flush();
  assert.match(app.html(), childRow, "fresh matches should be visible even if the ordinary tree was folded");
  app.toggle("root:0", false);
  assert.doesNotMatch(app.html(), childRow, "the user's fold must override initial search expansion");
  app.toggle("root:0", true);
  assert.match(app.html(), childRow);
  app.action("collapse-tree");
  assert.doesNotMatch(app.html(), childRow);
  app.action("expand-tree");
  assert.match(app.html(), childRow);
  app.action("clear-search");
  assert.doesNotMatch(app.html(), childRow, "search expansion must not overwrite ordinary tree folding");
  assert.equal(app.requests.length, 2);
});

test("reopening the resource tab starts a fresh read and discards the former tab's response", async () => {
  const app = setup();
  app.tab("resource");
  app.tab("list");
  app.tab("resource");
  assert.equal(app.requests.length, 2);
  app.requests[1].respond(resourceData("current-library"));
  await flush();
  app.requests[0].respond(resourceData("stale-library"));
  await flush();
  assert.match(app.html(), /current-library/);
  assert.doesNotMatch(app.html(), /stale-library/);
  assert.doesNotMatch(app.html(), /data-resource-action="scan" disabled/);
});

test("an old search cannot stop the current loading indicator or surface its error", async () => {
  const app = setup();
  app.tab("resource");
  app.requests[0].respond(resourceData("base-library"));
  await flush();
  app.search("old-query");
  app.search("new-query");
  app.requests[1].reject(new Error("outdated failure"));
  await flush();
  assert.match(app.html(), /data-resource-action="search-tree" disabled/);
  assert.equal(app.statuses.some((value) => value.message.includes("outdated failure")), false);
  app.requests[2].respond(resourceData("new-result", { query: "new-query" }));
  await flush();
  assert.match(app.html(), /new-result/);
  assert.doesNotMatch(app.html(), /data-resource-action="search-tree" disabled/);
});

test("clearing and repeating the same search ignores its earlier response", async () => {
  const app = setup();
  app.tab("resource");
  app.requests[0].respond(resourceData("base-library"));
  await flush();
  app.search("repeat");
  app.action("clear-search");
  app.search("repeat");
  app.requests[2].respond(resourceData("new-result", { query: "repeat" }));
  await flush();
  app.requests[1].respond(resourceData("stale-result", { query: "repeat" }));
  await flush();
  assert.match(app.html(), /new-result/);
  assert.doesNotMatch(app.html(), /stale-result/);
});

test("a slow directory response does not switch selection back from the user's newer folder", async () => {
  const app = setup();
  app.tab("resource");
  const data = resourceData("base");
  data.tree.children = [folder("slow", false), folder("current", true)];
  app.requests[0].respond(data);
  await flush();
  app.select("slow");
  app.select("current");
  app.requests[1].respond({ ok: true, node: folder("slow") });
  await flush();
  assert.match(app.html(), /link-index-list-location">S:\\current</);
  assert.doesNotMatch(app.html(), /link-index-list-location">S:\\slow</);
});

test("a directory read from a previous scan cannot modify a freshly reopened resource tree", async () => {
  const app = setup();
  app.tab("resource");
  const data = resourceData("shared");
  data.tree.children = [folder("shared", false)];
  app.requests[0].respond(data);
  await flush();
  app.select("shared");
  app.tab("list");
  app.tab("resource");
  app.requests[2].respond(resourceData("shared"));
  await flush();
  const staleNode = folder("shared");
  staleNode.name = "stale-node";
  app.requests[1].respond({ ok: true, node: staleNode });
  await flush();
  assert.doesNotMatch(app.html(), /stale-node/);
});
