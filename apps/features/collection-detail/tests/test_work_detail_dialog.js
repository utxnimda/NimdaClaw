"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = path.resolve(__dirname, "../frontend/work-detail-dialog.js");

function setup(options = {}) {
  let document;
  const timers = new Map();
  let nextTimer = 0;
  class Element {
    constructor(tag) {
      this.tagName = tag;
      this.children = [];
      this.attributes = {};
      this.listeners = {};
      this.hidden = false;
      this.disabled = false;
      this.className = "";
      this._text = "";
    }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
    set textContent(value) { this._text = String(value); this.children.forEach((child) => { child.parent = null; }); this.children = []; }
    set innerHTML(value) { throw new Error("Untrusted values must never enter innerHTML: " + value); }
    appendChild(child) { child.remove(); this.children.push(child); child.parent = this; return child; }
    removeChild(child) { this.children = this.children.filter((candidate) => candidate !== child); child.parent = null; }
    get firstChild() { return this.children[0] || null; }
    remove() { if (this.parent) this.parent.removeChild(this); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    async emit(name, extra = {}) {
      const event = { target: this, prevented: false, preventDefault() { this.prevented = true; }, ...extra };
      await Promise.all((this.listeners[name] || []).map((callback) => callback(event)));
      return event;
    }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; this.emit("close"); }
    get isConnected() { return this === document.body || Boolean(this.parent && this.parent.isConnected); }
  }
  function flatten(node) { return [node, ...node.children.flatMap(flatten)]; }
  document = { createElement(tag) { return new Element(tag); } };
  document.body = new Element("body");
  const trigger = document.body.appendChild(new Element("button"));
  trigger.focus();
  const window = { document };
  vm.runInNewContext(fs.readFileSync(SOURCE, "utf8"), {
    window, document, console,
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
  }, { filename: SOURCE });
  const dialog = window.NimdaWorkDetailDialog.open({
    title: "作品示例",
    sourceLabel: "[JP][TVInfo][2005].yaml · 第 3 条",
    loadRecord: async () => ({ record: sampleRecord() }),
    enumLabel(key, value) { return ({ country: { japan: "日本" }, domain: { animation: "动画" }, release_type: { tv: "TV" } })[key]?.[value] || value; },
    ...options,
  });
  const nodes = (container = dialog) => flatten(container);
  const byClass = (name, container = dialog) => nodes(container).find((node) => node.className.split(" ").includes(name));
  const button = (text, container = dialog) => nodes(container).find((node) => node.tagName === "button" && node.textContent === text);
  function runNextTimer() {
    const next = timers.entries().next().value;
    if (!next) return false;
    timers.delete(next[0]);
    next[1]();
    return true;
  }
  return { document, trigger, dialog, nodes, byClass, button, runNextTimer,
    timerCount() { return timers.size; },
    runTimers() {
      let count = 0;
      while (runNextTimer()) { if (++count > 10000) throw new Error("Scheduled work did not finish"); }
      return count;
    },
    click(text) { return button(text).emit("click"); },
    open(next) { return window.NimdaWorkDetailDialog.open(next); },
  };
}

function sampleRecord() {
  return {
    attributes: [
      { type: "name", data: "示例作品 <script>" },
      { type: "country", data: "japan" },
      { type: "date", data: { start: "20050101", end: "20050331", precision: "day" }, date_note: "原始日期备注" },
      { type: "collection-type", data: {
        domain: "animation", release_type: "tv", path: "U:\\作品\\特别长的目录名", markers: ["complete"],
        collectioned: [
          { press_format: "BDRip", press_group: "", press_path: "示例_BDRip", custom_count: 0 },
          { press_format: "1080p", press_group: "----", press_path: "", enabled: false },
        ],
        continuations: [{ title: "特别篇", collectioned: [{ press_format: "BDRip", press_group: "VCB", press_path: "SP" }], relation: null }],
        custom_collection: { flag: false },
      }, envelope_note: "collection envelope" },
      { type: "unknown", data: { x: 0, active: false, nullable: null, blank: "", nested: { original: "kept" } }, note: "保留属性" },
    ],
    extra: { score: 0, active: false, nullable: null, blank: "" },
    root_note: "顶层扩展字段",
  };
}

async function flush() { await Promise.resolve(); await Promise.resolve(); }
function freezeDeep(value) {
  if (value && typeof value === "object") { Object.values(value).forEach(freezeDeep); Object.freeze(value); }
  return value;
}
async function expandTree(app, container) {
  for (let count = 0; count < 1000; count++) {
    const details = app.nodes(container).find((node) => node.tagName === "details" && !node.open);
    if (details) { details.open = true; await details.emit("toggle"); continue; }
    const more = app.nodes(container).find((node) => node.tagName === "button" && node.className === "work-detail-more");
    if (more) { await more.emit("click"); continue; }
    return;
  }
  throw new Error("Tree did not finish expanding");
}

test("opens an accessible readonly dialog with visual details by default and compact dates formatted only for display", async () => {
  const record = freezeDeep(sampleRecord());
  const before = JSON.stringify(record);
  const app = setup({ loadRecord: async () => ({ record }) });
  assert.equal(app.dialog.open, true);
  assert.equal(app.document.activeElement, app.button("关闭"));
  assert.equal(app.byClass("work-detail-status").hidden, false);
  await flush();
  const visual = app.byClass("work-detail-visual");
  assert.equal(visual.hidden, false);
  assert.equal(app.byClass("work-detail-json").hidden, true);
  assert.equal(app.button("可视化详情").attributes["aria-selected"], "true");
  assert.match(visual.textContent, /2005-01-01/);
  assert.match(visual.textContent, /2005-03-31/);
  assert.match(visual.textContent, /日本/);
  assert.match(visual.textContent, /动画/);
  assert.match(visual.textContent, /续行 1 · 特别篇/);
  assert.match(visual.textContent, /U:\\作品\\特别长的目录名/);
  assert.equal(JSON.stringify(record), before);
  assert.equal(app.nodes().filter((node) => ["input", "select", "textarea"].includes(node.tagName)).length, 0);
  const groups = app.nodes(visual).filter((node) => node.tagName === "dt" && node.textContent === "压制 / 字幕组").map((node) => node.parent.children[1].textContent);
  assert.deepEqual(groups, ["—", "—", "VCB"]);
});

test("JSON view exposes every original field and literal, including unknown attributes and dates unchanged", async () => {
  const app = setup();
  await flush();
  const json = app.byClass("work-detail-json");
  assert.equal(json.children.length, 0, "JSON nodes are not created before the mode is requested");
  await app.click("JSON 数据");
  assert.equal(json.hidden, false);
  assert.equal(app.byClass("work-detail-visual").hidden, true);
  await expandTree(app, json);
  for (const value of ["20050101", "20050331", "root_note", "date_note", "precision", "envelope_note", "custom_collection", "relation", "original", "kept", "\"blank\": \"\"", "\"active\": false", "\"score\": 0", "\"nullable\": null", "----"]) {
    assert.ok(json.textContent.includes(value), `Missing JSON value: ${value}`);
  }
  const count = app.nodes(json).length;
  await app.click("可视化详情");
  await app.click("JSON 数据");
  assert.equal(app.nodes(json).length, count, "Switching modes must retain the expanded tree without duplicate nodes");
});

test("visual mode retains unknown record and attribute fields safely instead of rendering markup", async () => {
  const app = setup({ title: '<img src=x onerror="alert(1)">', notice: "<script>notice</script>" });
  await flush();
  const visual = app.byClass("work-detail-visual");
  await expandTree(app, visual);
  for (const text of ["date_note", "envelope_note", "custom_count", "enabled", "custom_collection", "root_note", "unknown", "保留属性", "nested", "kept"]) {
    assert.ok(visual.textContent.includes(text), `Missing extension: ${text}`);
  }
  assert.match(app.dialog.textContent, /<img src=x onerror="alert\(1\)">/);
  assert.match(app.dialog.textContent, /示例作品 <script>/);
  assert.equal(app.nodes().filter((node) => ["script", "img", "iframe", "a"].includes(node.tagName)).length, 0);
});

test("failed load can be retried and invalid records show an actionable failure", async () => {
  let calls = 0;
  const app = setup({ loadRecord: async () => {
    calls++;
    if (calls === 1) throw new Error("记录已移动，请重新加载列表");
    if (calls === 2) return { record: null };
    return { record: sampleRecord(), source: "confirmed.yaml · 第 2 条" };
  } });
  await flush();
  assert.equal(app.byClass("work-detail-error-panel").hidden, false);
  assert.match(app.byClass("work-detail-error").textContent, /记录已移动/);
  assert.equal(app.button("重新读取").disabled, false);
  await app.click("重新读取");
  assert.match(app.byClass("work-detail-error").textContent, /有效的作品记录/);
  await app.click("重新读取");
  assert.equal(calls, 3);
  assert.equal(app.byClass("work-detail-error-panel").hidden, true);
  assert.equal(app.byClass("work-detail-visual").hidden, false);
  assert.equal(app.byClass("work-detail-source").textContent, "confirmed.yaml · 第 2 条");
});

test("Escape closes while loading and a late result cannot mutate a replacement dialog", async () => {
  let resolveFirst;
  const app = setup({ loadRecord: () => new Promise((resolve) => { resolveFirst = resolve; }) });
  const event = await app.dialog.emit("cancel");
  assert.equal(event.prevented, true);
  assert.equal(app.dialog.isConnected, false);
  assert.equal(app.document.activeElement, app.trigger);
  const next = app.open({ title: "新作品", loadRecord: async () => ({ record: { attributes: [{ type: "name", data: "新记录" }] } }) });
  await flush();
  resolveFirst({ record: { attributes: [{ type: "name", data: "过期结果" }] } });
  await flush();
  assert.match(next.textContent, /新记录/);
  assert.doesNotMatch(next.textContent, /过期结果/);
  assert.equal(app.byClass("work-detail-visual").children.length, 0, "Closed dialog must not render stale content either");
  assert.equal(app.document.body.children.filter((node) => node.tagName === "dialog").length, 1);
});

test("programmatic reopening replaces the old modal and restores the original external trigger", async () => {
  const app = setup();
  await flush();
  const next = app.open({ title: "第二个作品", loadRecord: async () => ({ record: {} }) });
  await flush();
  assert.equal(app.dialog.isConnected, false);
  assert.equal(app.document.body.children.filter((node) => node.tagName === "dialog").length, 1);
  await app.button("关闭", next).emit("click");
  assert.equal(app.document.activeElement, app.trigger);
});

test("loading is single-flight and tabs remain usable with keyboard navigation", async () => {
  let release;
  let calls = 0;
  const app = setup({ loadRecord: () => { calls++; return new Promise((resolve) => { release = resolve; }); } });
  await app.click("重新读取");
  assert.equal(calls, 1);
  const event = await app.button("可视化详情").emit("keydown", { key: "ArrowRight" });
  assert.equal(event.prevented, true);
  assert.equal(app.button("JSON 数据").attributes["aria-selected"], "true");
  assert.equal(app.document.activeElement, app.button("JSON 数据"));
  release({ record: sampleRecord() });
  await flush();
  assert.equal(app.byClass("work-detail-json").hidden, false);
  await app.button("JSON 数据").emit("keydown", { key: "Home" });
  assert.equal(app.button("可视化详情").attributes["aria-selected"], "true");
  assert.equal(app.byClass("work-detail-body").attributes["aria-busy"], "false");
});

test("large JSON arrays are lazy and paged, including the final item", async () => {
  const app = setup({ loadRecord: async () => ({ record: { attributes: [], many: Array.from({ length: 173 }, (_, index) => ({ index, nested: { name: "item-" + index } })) } }) });
  await flush();
  await app.click("JSON 数据");
  const json = app.byClass("work-detail-json");
  const array = app.nodes(json).find((node) => node.tagName === "details" && node.children[0].textContent.includes('"many"'));
  assert.equal(array.children.length, 1, "Collapsed branch must not create descendant DOM");
  array.open = true;
  await array.emit("toggle");
  const children = array.children[1];
  assert.equal(children.children.filter((node) => node.tagName === "details").length, 80);
  assert.match(children.children.at(-1).textContent, /剩余 93 项/);
  await children.children.at(-1).emit("click");
  assert.equal(children.children.filter((node) => node.tagName === "details").length, 160);
  await children.children.at(-1).emit("click");
  assert.equal(children.children.length, 173);
  const last = children.children.at(-1);
  last.open = true;
  await last.emit("toggle");
  assert.match(last.textContent, /"index": 172/);
  assert.equal(app.nodes(json).filter((node) => node.tagName === "button" && node.className === "work-detail-more").length, 0);
});

test("nonstandard attributes, empty containers, and prototype-like keys remain inspectable", async () => {
  const record = JSON.parse('{"attributes":[false,0,null,{"type":"collection-type","data":{"markers":false,"collectioned":null,"continuations":0}}],"__proto__":{"retained":true},"empty":{},"array":[]}');
  const app = setup({ loadRecord: async () => ({ record }) });
  await flush();
  await app.click("JSON 数据");
  const json = app.byClass("work-detail-json");
  await expandTree(app, json);
  assert.match(json.textContent, /"__proto__"/);
  assert.match(json.textContent, /"retained": true/);
  assert.match(json.textContent, /空对象 \{\}/);
  assert.match(json.textContent, /空数组 \[\]/);
  assert.match(json.textContent, /"0": false/);
  assert.match(json.textContent, /"1": 0/);
  assert.match(json.textContent, /"2": null/);
  assert.equal({}.retained, undefined);
});

test("one-click expand visits every level and every page in asynchronous batches without changing the record", async () => {
  const record = freezeDeep({
    attributes: [],
    many: Array.from({ length: 173 }, (_, index) => ({ index, nested: { name: "item-" + index, values: [false, 0, null, ""] } })),
  });
  const original = JSON.stringify(record);
  const app = setup({ loadRecord: async () => ({ record }) });
  await flush();
  await app.click("JSON 数据");
  const json = app.byClass("work-detail-json");
  const before = app.nodes(json).length;
  await app.click("全部展开");
  assert.equal(app.nodes(json).length, before, "The click must yield before rendering a potentially large tree");
  assert.equal(app.button("全部展开").disabled, true);
  assert.equal(app.byClass("work-detail-json-tree").attributes["aria-busy"], "true");
  assert.equal(app.timerCount(), 1);
  await app.click("全部展开");
  assert.equal(app.timerCount(), 1, "Duplicate clicks must not create competing expansion jobs");
  assert.ok(app.runTimers() > 1, "A large record must be rendered over multiple event-loop turns");
  const branches = app.nodes(json).filter((node) => node.tagName === "details");
  assert.ok(branches.length > 500);
  assert.ok(branches.every((node) => node.open), "Every nested branch must be open");
  assert.equal(app.nodes(json).filter((node) => node.className === "work-detail-more").length, 0);
  assert.match(json.textContent, /"name": "item-172"/);
  assert.match(json.textContent, /"0": false/);
  assert.match(json.textContent, /"1": 0/);
  assert.match(json.textContent, /"2": null/);
  assert.match(json.textContent, /"3": ""/);
  assert.match(app.byClass("work-detail-json-bulk-status").textContent, /已全部展开/);
  assert.equal(app.button("全部展开").disabled, false);
  assert.equal(app.byClass("work-detail-json-tree").attributes["aria-busy"], "false");
  assert.equal(JSON.stringify(record), original);
});

test("collapse all cancels expansion immediately, discards expanded DOM, and can restart with complete data", async () => {
  const app = setup({ loadRecord: async () => ({ record: { attributes: [], many: Array.from({ length: 173 }, (_, index) => ({ nested: { name: "item-" + index } })) } }) });
  await flush();
  await app.click("JSON 数据");
  await app.click("全部展开");
  assert.equal(app.runNextTimer(), true);
  assert.equal(app.timerCount(), 1);
  await app.click("全部折叠");
  assert.equal(app.timerCount(), 0);
  assert.equal(app.runTimers(), 0);
  const tree = app.byClass("work-detail-json-tree");
  let branches = app.nodes(tree).filter((node) => node.tagName === "details");
  assert.equal(branches.length, 1);
  assert.equal(branches[0].open, undefined);
  assert.equal(branches[0].children.length, 1, "Collapsing restores a fully lazy root without retained descendant DOM");
  assert.match(app.byClass("work-detail-json-bulk-status").textContent, /已全部折叠/);
  assert.equal(app.button("全部展开").disabled, false);
  await app.click("全部展开");
  app.runTimers();
  branches = app.nodes(tree).filter((node) => node.tagName === "details");
  assert.ok(branches.every((node) => node.open));
  assert.match(tree.textContent, /item-172/);
  await app.click("全部折叠");
  assert.equal(app.nodes(tree).filter((node) => node.tagName === "details").length, 1);
});

test("switching display modes cancels queued expansion but keeps already rendered content inspectable", async () => {
  const app = setup({ loadRecord: async () => ({ record: { attributes: [], many: Array.from({ length: 173 }, (_, index) => ({ value: index })) } }) });
  await flush();
  await app.click("JSON 数据");
  await app.click("全部展开");
  app.runNextTimer();
  const tree = app.byClass("work-detail-json-tree");
  const count = app.nodes(tree).length;
  await app.click("可视化详情");
  assert.equal(app.timerCount(), 0);
  app.runTimers();
  assert.equal(app.nodes(tree).length, count);
  assert.equal(app.button("全部展开").disabled, false);
  await app.click("JSON 数据");
  assert.match(app.byClass("work-detail-json-bulk-status").textContent, /已停止全部展开/);
  await app.click("全部展开");
  app.runTimers();
  assert.match(tree.textContent, /"value": 172/);
  assert.ok(app.nodes(tree).filter((node) => node.tagName === "details").every((node) => node.open));
});

test("closing or replacing a detail modal cancels old expansion jobs without modifying either stale or new trees", async () => {
  for (const replace of [false, true]) {
    const app = setup({ loadRecord: async () => ({ record: { attributes: [], many: Array.from({ length: 173 }, (_, index) => ({ value: "old-" + index })) } }) });
    await flush();
    await app.click("JSON 数据");
    await app.click("全部展开");
    app.runNextTimer();
    const oldTree = app.byClass("work-detail-json-tree");
    const oldCount = app.nodes(oldTree).length;
    let next;
    if (replace) next = app.open({ title: "新详情", loadRecord: async () => ({ record: { attributes: [], name: "replacement" } }) });
    else await app.click("关闭");
    await flush();
    assert.equal(app.timerCount(), 0);
    app.runTimers();
    assert.equal(app.nodes(oldTree).length, oldCount);
    assert.equal(app.dialog.isConnected, false);
    if (next) {
      assert.match(next.textContent, /replacement/);
      assert.doesNotMatch(next.textContent, /old-/);
      await app.button("JSON 数据", next).emit("click");
      await app.button("全部展开", next).emit("click");
      app.runTimers();
      assert.match(app.byClass("work-detail-json-bulk-status", next).textContent, /已全部展开/);
    }
  }
});

test("manually collapsing a branch during bulk expansion cancels the remaining background work", async () => {
  const app = setup({ loadRecord: async () => ({ record: { attributes: [], many: Array.from({ length: 173 }, (_, index) => ({ value: index })) } }) });
  await flush();
  await app.click("JSON 数据");
  await app.click("全部展开");
  app.runNextTimer();
  const tree = app.byClass("work-detail-json-tree");
  const root = tree.children[0];
  root.open = false;
  await tree.emit("toggle", { target: root });
  assert.equal(app.timerCount(), 0);
  app.runTimers();
  assert.equal(root.open, false);
  assert.match(app.byClass("work-detail-json-bulk-status").textContent, /已停止全部展开/);
});
