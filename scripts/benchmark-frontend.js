"use strict";

// Synthetic rendering only: no servers, requests, database, or media access.
// Run with: node scripts/benchmark-frontend.js
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { performance } = require("node:perf_hooks");

const ROOT = path.resolve(__dirname, "..");

function loadFrontend(feature, hookSource, overrides = {}) {
  const filename = path.join(ROOT, "apps/features", feature, "frontend/index.js");
  let hooks;
  const document = overrides.document || {
    getElementById() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; }, addEventListener() {}, removeEventListener() {},
    body: { classList: { remove() {} } },
  };
  const window = { document, __captureTreeBenchmark(value) { hooks = value; }, ...overrides.window };
  window.window = window;
  const marker = feature === "collection-detail" ? "  root.register({" : "  registry.register({";
  const original = fs.readFileSync(filename, "utf8");
  if (!original.includes(marker)) throw new Error("Frontend registration marker changed: " + filename);
  const common = fs.readFileSync(path.join(ROOT, "apps/framework/frontend/src/common/runtime.js"), "utf8");
  const source = common + '\nwindow.localStorage = localStorage;\n' +
    original.replace(marker, "  window.__captureTreeBenchmark(" + hookSource + ");\n" + marker);
  vm.runInNewContext(source, {
    window, document, console, localStorage: { getItem() { return null; } },
    URLSearchParams, setTimeout, clearTimeout,
  }, { filename });
  return { hooks, window, document };
}

function resourceTreeFixture(rootCount = 20, width = 25) {
  let count = 0;
  function branch(name, depth) {
    count++;
    return {
      type: "folder", name, relpath: name, path: "S:/" + name,
      children_loaded: true, files: [],
      children: depth ? Array.from({ length: width }, (_, index) => branch(name + "/" + index, depth - 1)) : [],
    };
  }
  const tree = { type: "folder", relpath: "", children_loaded: true, children: [] };
  const collapsed = {};
  for (let index = 0; index < rootCount; index++) {
    tree.children.push(branch("root:" + index, 2));
    collapsed["root:" + index] = true;
  }
  return { tree, collapsed, count };
}

function measure(render) {
  let output;
  const elapsed = [];
  for (let run = 0; run < 4; run++) {
    const start = performance.now();
    output = render();
    elapsed.push(performance.now() - start);
  }
  return { first_render_ms: Number(elapsed[0].toFixed(2)),
    repeat_median_ms: Number(elapsed.slice(1).sort((left, right) => left - right)[1].toFixed(2)),
    html_bytes: Buffer.byteLength(output) };
}

function benchmark() {
  const resource = loadFrontend("collection-detail",
    "{ render: renderResourceTreeRootChildren, collapse: function(paths) { resourceTreeCollapsedPaths = paths; } }").hooks;
  const fixture = resourceTreeFixture();
  resource.collapse(fixture.collapsed);
  console.log(JSON.stringify({ benchmark: "collapsed_resource_tree", nodes: fixture.count,
    ...measure(() => resource.render(fixture.tree)) }));
}

module.exports = { loadFrontend, resourceTreeFixture, measure };
if (require.main === module) benchmark();
