"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const SOURCE = path.resolve(__dirname, "../frontend/src/operation-center.js");

function response(data, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => data };
}
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup(fetcher = async () => response({ ok: true })) {
  let document;
  let now = 1800480000000;
  let nextTimer = 0;
  let nextId = 0;
  const timers = new Map();
  const calls = [];
  class Element {
    constructor(tag) {
      this.tagName = tag;
      this.children = [];
      this.attributes = {};
      this.dataset = {};
      this.listeners = {};
      this.textContent = "";
      this.hidden = false;
      this.scrollHeight = 0;
      this.scrollTop = 0;
      this.clientHeight = 0;
      this.classList = { add() {}, toggle() {} };
    }
    appendChild(child) { child.remove(); this.children.push(child); child.parent = this; return child; }
    insertBefore(child, reference) { child.remove(); const index = this.children.indexOf(reference); this.children.splice(index < 0 ? this.children.length : index, 0, child); child.parent = this; return child; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); this.parent = null; }
    replaceChildren(...children) { this.children.forEach((child) => { child.parent = null; }); this.children = []; children.forEach((child) => this.appendChild(child)); }
    setAttribute(name, value) { this.attributes[name] = value; }
    getAttribute(name) { return this.attributes[name] || null; }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    emit(name, attrs = {}) {
      const event = { target: this, preventDefault() {}, ...attrs };
      (this.listeners[name] || []).forEach((callback) => callback(event));
    }
    closest(selector) { return selector === "[data-operation-details]" && Object.hasOwn(this.attributes, "data-operation-details") ? this : null; }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; this.emit("close"); }
    get isConnected() { return this === document.body || Boolean(this.parent && this.parent.isConnected); }
  }
  document = new Element("document");
  document.createElement = (tag) => new Element(tag);
  document.body = new Element("body");
  class ClockDate extends Date {
    constructor(value) { super(arguments.length ? value : now); }
    static now() { return now; }
  }
  const window = { document, location: { href: "http://127.0.0.1:57357/", origin: "http://127.0.0.1:57357" }, crypto: { randomUUID() { return "operation-" + ++nextId; } } };
  const windowListeners = {};
  window.addEventListener = (name, callback) => { (windowListeners[name] ||= []).push(callback); };
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../frontend/src/common/operation-result.js"), "utf8"), { window });
  vm.runInNewContext(fs.readFileSync(SOURCE, "utf8"), {
    window, document, console, URL, Headers, AbortController, Date: ClockDate,
    fetch(url, options) { calls.push({ url, options }); return fetcher(url, options); },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  }, { filename: SOURCE });
  async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
  return { center: window.NimdaOperationCenter, document, calls, timers,
    emitWindow(name, event) { (windowListeners[name] || []).forEach((callback) => callback(event)); },
    async advance(ms) {
      now += ms;
      for (let i = 0; i < 100; i++) {
        const due = [...timers].find(([, timer]) => timer.at <= now);
        if (!due) break;
        timers.delete(due[0]); due[1].callback(); await flush();
      }
    },
    flush,
    all(node = document.body) { return [node, ...node.children.flatMap((child) => this.all(child))]; },
    task() { return this.center.snapshot().at(-1); },
  };
}

test("manual lifecycle has bounded, escaped text history and no estimated percentage", () => {
  const app = setup();
  const job = app.center.start("<img src=x onerror=alert(1)>");
  job.progress("扫描文件", { completed: 2, detail: "<script>bad()</script>" });
  app.center.open(job.id);
  assert.equal(app.center.ui.heading.textContent, "<img src=x onerror=alert(1)>");
  assert.equal(app.center.ui.meter.hidden, true);
  assert.match(app.center.ui.metrics.textContent, /总量尚未确定/);
  assert.equal(app.all().filter((node) => node.tagName === "img" || node.tagName === "script").length, 0);
  job.progress("核对文件", { completed: 2, total: 5, unit: "文件" });
  assert.equal(app.center.ui.meter.max, 5);
  assert.equal(app.center.ui.meter.value, 2);
  job.finish("完成核对");
  assert.equal(app.task().status, "succeeded");
  assert.equal(app.task().events.at(-1).message, "完成核对");
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].url, "/api/operations/client-events");
  for (let i = 0; i < 65; i++) app.center.start("任务" + i).finish();
  assert.equal(app.center.snapshot().length, 60);
  const many = app.center.start("大量日志");
  for (let i = 0; i < 200; i++) many.progress("步骤" + i);
  assert.equal(app.task().events.length, 160);
});

test("request keeps body and headers, adds id, returns original shape, never stores query or payload", async () => {
  const app = setup();
  const body = new FormData();
  body.append("private", "secret-value");
  const headers = new Headers({ "X-Custom": "retained" });
  const options = { method: "POST", body, headers };
  const result = await app.center.fetchJson("/api/browse?private=secret-query", options);
  assert.equal(result.res.ok, true);
  assert.deepEqual(result.data, { ok: true });
  assert.equal(app.calls[0].options.body, body);
  assert.equal(app.calls[0].options.headers.get("X-Custom"), "retained");
  assert.equal(app.calls[0].options.headers.get("X-Nimda-Operation-Id"), app.task().id);
  assert.equal(headers.has("X-Nimda-Operation-Id"), false);
  assert.equal(app.calls[0].options.headers.has("Content-Type"), false);
  assert.doesNotMatch(JSON.stringify(app.center.snapshot()), /secret-/);
  assert.equal(app.task().title, "读取上传的数据库文件");
  assert.equal(app.center.ui.dialog.open, undefined, "starting work must not interrupt with a modal");
});

test("only same-origin application endpoints are traced, excluding health and polling", async () => {
  const app = setup();
  for (const url of ["/api/health", "/api/operations/test", "/static/test.json", "https://other.example/api/lookup"]) {
    await app.center.fetchJson(url);
  }
  assert.equal(app.center.snapshot().length, 0);
  assert.equal(app.calls.some((call) => call.options.headers), false);
});

test("HTTP failure, invalid JSON, and null JSON preserve existing fetchJson behavior", async () => {
  const failed = setup(async () => response({ ok: false, error: "配置校验失败" }, 400));
  const result = await failed.center.fetchJson("/api/config/enum-edits");
  assert.equal(result.res.status, 400);
  assert.equal(failed.task().status, "failed");
  assert.match(failed.task().message, /配置校验失败/);
  const malformed = setup(async () => ({ ok: true, status: 200, json: async () => { throw new Error("bad JSON"); } }));
  assert.equal(JSON.stringify((await malformed.center.fetchJson("/api/browse/default")).data), "{}");
  assert.equal(malformed.task().status, "unknown");
  assert.match(malformed.task().message, /无法解析/);
  const empty = setup(async () => response(null));
  assert.equal((await empty.center.fetchJson("/api/browse/default")).data, null);
});

test("nested partial failures and skipped targets are not falsely presented as total success", async () => {
  const app = setup(async () => response({ ok: true, execution: { state: "partial" }, file_generation: { skipped_empty_target: 2, skipped_missing_target: 3 } }));
  await app.center.fetchJson("/api/collection-detail/link-index/generate-files");
  assert.equal(app.task().status, "warning");
  assert.match(app.task().message, /空目标 2 项/);
  assert.match(app.task().message, /目标不存在 3 项/);
  app.center.merge(app.center.get(app.task().id), { status: "succeeded", message: "请求完成", events: [] });
  assert.equal(app.task().status, "warning");
  assert.match(app.task().message, /目标不存在 3 项/);
});

test("network failure keeps outcome unknown, rethrows original error, and never repeats mutation", async () => {
  const networkError = new Error("connection lost");
  let polls = 0;
  const app = setup(async (url) => {
    if (url.startsWith("/api/operations/")) {
      polls++;
      return response({ ok: true, operation: { status: polls === 1 ? "queued" : polls === 2 ? "running" : "succeeded", message: "仍在处理", events: [] } });
    }
    throw networkError;
  });
  await assert.rejects(app.center.fetchJson("/api/collection-detail/link-index/save", { method: "POST" }), (error) => error === networkError);
  assert.equal(app.task().status, "unknown");
  assert.match(app.task().message, /不要直接重复提交/);
  await app.advance(1000);
  assert.equal(app.task().status, "queued");
  await app.advance(1000);
  assert.equal(app.task().status, "running");
  assert.equal(app.task().finished_at, null);
  await app.advance(1000);
  assert.equal(app.task().status, "succeeded");
  assert.equal(app.calls.filter((call) => call.url === "/api/collection-detail/link-index/save").length, 1);
});

test("polling is globally serial, cursored, deduplicated, and never tracked recursively", async () => {
  const a = deferred();
  const b = deferred();
  const poll = deferred();
  let polls = 0;
  const app = setup((url) => {
    if (url.startsWith("/api/operations/")) {
      polls++;
      if (polls === 1) return poll.promise;
      return Promise.resolve(response({ ok: true, operation: { status: "running", events: [{ seq: 1, message: "处理文件", completed: 1, total: 4 }], last_seq: 1 } }));
    }
    return url === "/api/browse/default" ? a.promise : b.promise;
  });
  const one = app.center.fetchJson("/api/browse/default");
  const two = app.center.fetchJson("/api/browse/catalog");
  await app.advance(500);
  assert.equal(polls, 1);
  await app.advance(1000);
  assert.equal(polls, 1, "second poll cannot overlap a pending poll");
  poll.resolve(response({ ok: true, operation: { status: "running", events: [{ seq: 1, message: "处理文件", completed: 1, total: 4 }], last_seq: 1 } }));
  await app.flush();
  await app.advance(1);
  assert.equal(polls, 2);
  await app.advance(1000);
  assert.ok(app.calls.some((call) => call.url.includes("after_seq=1")));
  assert.equal(app.center.snapshot().length, 2);
  assert.equal(app.center.snapshot()[0].events.filter((event) => event.message === "处理文件").length, 1);
  a.resolve(response({ ok: true })); b.resolve(response({ ok: true })); await Promise.all([one, two]);
});

test("404 progress falls back after a bounded number while leaving the original request alive", async () => {
  const original = deferred();
  const app = setup((url) => url.startsWith("/api/operations/") ? Promise.resolve(response({}, 404)) : original.promise);
  const pending = app.center.fetchJson("/api/browse/default");
  for (let i = 0; i < 8; i++) await app.advance(1000);
  assert.equal(app.calls.filter((call) => call.url.startsWith("/api/operations/")).length, 3);
  assert.equal(app.task().status, "running");
  assert.match(app.task().message, /等待请求返回/);
  original.resolve(response({ ok: true })); await pending;
  assert.equal(app.task().status, "succeeded");
});

test("closing details never cancels work and delegated prefix actions select matching tasks", async () => {
  const original = deferred();
  const app = setup(() => original.promise);
  const pending = app.center.fetchJson("/api/collection-detail/link-index/preview");
  const selected = app.task().id;
  app.center.start("其它模块", { path: "/api/browse/default" });
  const trigger = app.document.body.appendChild(app.document.createElement("button"));
  trigger.setAttribute("data-operation-details", "");
  trigger.setAttribute("data-operation-prefix", "/api/collection-detail/link-index/");
  trigger.focus();
  app.document.emit("click", { target: trigger });
  assert.equal(app.center.selectedId, selected);
  assert.equal(app.center.ui.dialog.open, true);
  app.center.ui.close.emit("click");
  assert.equal(app.center.ui.dialog.open, false);
  assert.equal(app.document.activeElement, trigger);
  assert.equal(app.center.get(selected).requestPending, true);
  assert.equal(app.calls.length, 1);
  original.resolve(response({ ok: true })); await pending;
});

test("status binding is idempotent, keyboard accessible, and opens the shared dialog", () => {
  const app = setup();
  const status = app.document.body.appendChild(app.document.createElement("div"));
  app.center.attachStatus(status);
  app.center.attachStatus(status);
  assert.equal(status.attributes.role, "button");
  assert.equal(status.attributes.tabindex, "0");
  assert.equal(status.listeners.click.length, 1);
  status.emit("keydown", { key: "Enter" });
  assert.equal(app.center.ui.dialog.open, true);
});

test("server event timestamps and queued state are accepted; unknown totals clear previous bars", () => {
  const app = setup();
  const handle = app.center.start("扫描");
  const task = app.center.get(handle.id);
  task.requestPending = true;
  app.center.merge(task, { status: "queued", started_at: null, total: 4, completed: 2, events: [{ seq: 1, at: "2026-09-21T01:02:03.456+00:00", message: "开始扫描" }], last_seq: 1 });
  app.center.open(handle.id);
  assert.equal(app.task().status, "queued");
  assert.equal(app.task().events.at(-1).at, Date.parse("2026-09-21T01:02:03.456+00:00"));
  assert.equal(app.center.ui.meter.hidden, false);
  app.center.merge(task, { status: "running", total: null, completed: null, events: [] });
  app.center.render();
  assert.equal(app.center.ui.meter.hidden, true);
  assert.match(app.center.ui.metrics.textContent, /不估算百分比/);
});

test("a phase with explicit unknown counters clears old counts but lifecycle-only messages retain them", () => {
  const app = setup();
  const job = app.center.start("分阶段事务");
  app.center.open(job.id);
  job.progress("扫描结束", { completed: 12, total: 12, unit: "文件" });
  assert.equal(app.center.ui.meter.value, 12);
  job.progress("整理计划", { completed: null, total: null, unit: "" });
  assert.equal(app.task().completed, null);
  assert.equal(app.task().total, null);
  assert.equal(app.task().unit, "");
  assert.equal(app.center.ui.meter.hidden, true);
  job.progress("整理结束", { completed: 4, total: 4, unit: "目录" });
  job.finish();
  assert.equal(app.task().completed, 4);
  assert.equal(app.task().total, 4);
  assert.equal(app.task().unit, "目录");
});

test("history limits never discard running requests or jobs awaiting final server verification", () => {
  const app = setup();
  const active = Array.from({ length: 65 }, (_, i) => app.center.start("活动事务 " + i));
  assert.equal(app.center.snapshot().length, 65);
  active.forEach((job) => assert.ok(app.center.get(job.id)));
  const watched = app.center.get(active[0].id);
  watched.watch = true;
  active[0].finish();
  for (let i = 1; i < active.length; i++) active[i].finish();
  assert.ok(app.center.get(active[0].id), "awaiting-final-poll records cannot be evicted");
  assert.equal(app.center.snapshot().length, 60);
});

test("rendered timeline stays chronological and periodic updates do not reparent focused history buttons", () => {
  const app = setup();
  const job = app.center.start("时间线");
  app.center.open(job.id);
  job.finish();
  const task = app.center.get(job.id);
  app.center.merge(task, { status: "succeeded", events: [{ seq: 1, at: task.startedAt - 5000, message: "较早的服务端事件" }] });
  app.center.render();
  assert.equal(app.center.ui.events.children[0].children[1].children[0].textContent, "较早的服务端事件");
  let moves = 0;
  const insert = app.center.ui.history.insertBefore.bind(app.center.ui.history);
  app.center.ui.history.insertBefore = (...args) => { moves++; return insert(...args); };
  app.center.render();
  assert.equal(moves, 0);
});

test("results show exact problems and counts without retaining arbitrary DB payloads", async () => {
  const app = setup(async () => response({ ok: true, summary: { created: 2, failed: 1 },
    errors: [{ code: "target-missing", message: "目标目录不存在", path: "S:/作品/BDRip" }],
    works: [{ secret: "whole-database-must-not-be-retained" }] }));
  await app.center.fetchJson("/api/collection-detail/link-index/generate-files", { method: "POST" });
  app.center.open(app.task().id);
  assert.equal(app.task().status, "warning");
  assert.equal(app.task().result.counters.find((row) => row.label === "已创建").value, 2);
  assert.equal(app.task().result.details[0].path, "S:/作品/BDRip");
  assert.doesNotMatch(JSON.stringify(app.center.snapshot()), /whole-database/);
  assert.ok(app.all().some((node) => node.textContent.includes("target-missing")));
});

test("historical logs restore results and show actual file content with bounded read-only paging", async () => {
  const app = setup(async (url) => {
    if (url.startsWith("/api/operations?")) return response({ ok: true, operations: [{ id: "old-job", title: "/api/browse/save",
      status: "failed", message: "写入失败", started_at: "2026-10-02T01:00:00Z", finished_at: "2026-10-02T01:00:03Z",
      events: [], log: { available: true, path: "E:/Project/nimda/data/framework/logs/operations/2026-10-02/example.jsonl" },
      result: { summary: "写入失败", counters: [], details: [{ level: "error", message: "权限不足" }], truncated: 0 } }], next_cursor: "older" });
    if (url.includes("/old-job/log?")) return response({ ok: true, log: { path: "example.jsonl", content: '<script>not executable</script>\n{"event":"failed"}', next_offset: 100, eof: false } });
    return response({ ok: true });
  });
  await app.center.loadHistory(false);
  app.center.open("old-job");
  assert.equal(app.center.ui.readLog.disabled, false);
  assert.match(app.center.ui.logPath.textContent, /2026-10-02/);
  await app.center.loadLog("old-job", false);
  assert.match(app.center.ui.logContent.textContent, /not executable/);
  assert.equal(app.all().filter((node) => node.tagName === "script").length, 0);
  await app.center.loadLog("old-job", true);
  assert.ok(app.calls.some((call) => call.url.includes("offset=100&limit=65536")));
  await app.center.loadHistory(true);
  assert.ok(app.calls.some((call) => call.url.includes("before=older")));
  assert.equal(app.calls.some((call) => call.options.method === "POST"), false);
});

test("client preflight errors have their own persistent record and cannot overwrite running server jobs", async () => {
  const original = deferred();
  const app = setup((url) => url === "/api/browse/save" ? original.promise : Promise.resolve(response({ ok: true })));
  const saving = app.center.fetchJson("/api/browse/save", { method: "POST" });
  const serverId = app.task().id;
  app.center.notice("作品名不能为空", true);
  const errorTask = app.task();
  assert.notEqual(errorTask.id, serverId);
  assert.equal(errorTask.status, "failed");
  assert.equal(app.center.get(serverId).status, "running");
  const saved = app.calls.find((call) => call.url === "/api/operations/client-events");
  assert.equal(JSON.parse(saved.options.body).message, "作品名不能为空");
  assert.match(JSON.parse(saved.options.body).details, /作品名不能为空/);
  original.resolve(response({ ok: true }));
  await saving;
});

test("log errors are visible and history pagination does not accumulate unbounded remote tasks", async () => {
  let page = 0;
  const app = setup(async (url) => {
    if (url.startsWith("/api/operations?")) return response({ ok: true, operations: [{ id: "history-" + ++page, title: "旧记录", status: "succeeded", events: [] }], next_cursor: "next" });
    return response({ ok: false, error: "日志文件不可读取" }, 404);
  });
  for (let i = 0; i < 8; i++) await app.center.loadHistory(i > 0);
  assert.equal(app.center.tasks.length, 1);
  app.center.open("history-8");
  await app.center.loadLog("history-8", false);
  assert.match(app.center.ui.logStatus.textContent, /日志文件不可读取/);
});

test("transient final status failures retry reads and recover the durable log metadata", async () => {
  let polls = 0;
  const app = setup(async (url) => {
    if (url.startsWith("/api/operations/")) {
      if (++polls === 1) return response({ error: "temporarily unavailable" }, 503);
      return response({ ok: true, operation: { status: "succeeded", events: [], log: { available: true, path: "completed.jsonl" } } });
    }
    return response({ ok: true });
  });
  await app.center.fetchJson("/api/browse/default");
  await app.advance(500);
  assert.equal(polls, 1);
  assert.equal(app.center.get(app.task().id).watch, true);
  await app.advance(1000);
  assert.equal(polls, 2);
  assert.equal(app.task().log.path, "completed.jsonl");
  assert.equal(app.calls.filter((call) => call.url === "/api/browse/default").length, 1);
});

test("restoring running history resumes status polling rather than leaving it stuck", async () => {
  const app = setup(async (url) => url.startsWith("/api/operations?")
    ? response({ ok: true, operations: [{ id: "restored-job", title: "扫描", status: "running", events: [] }], next_cursor: null })
    : response({ ok: true, operation: { status: "succeeded", message: "扫描完成", events: [] } }));
  await app.center.loadHistory(false);
  assert.equal(app.center.get("restored-job").watch, true);
  await app.advance(1);
  assert.equal(app.center.get("restored-job").status, "succeeded");
});

test("uncaught page errors and asynchronous failures enter persistent processing details", async () => {
  const app = setup();
  app.emitWindow("error", { error: new Error("render failed") });
  app.emitWindow("unhandledrejection", { reason: new Error("asynchronous failure") });
  await app.flush();
  assert.equal(app.center.snapshot().length, 2);
  assert.ok(app.center.snapshot().every((task) => task.status === "failed"));
  assert.match(app.center.snapshot()[0].message, /render failed/);
  assert.equal(app.calls.filter((call) => call.url === "/api/operations/client-events").length, 2);
});

test("whole-file log search continues beyond displayed bytes and jumps to matching line", async () => {
  let searches = 0;
  const app = setup(async (url) => {
    if (url.includes("/log/search")) return response({ ok: true, search: ++searches === 1
      ? { matches: [], next_offset: 8388608, eof: false, total_bytes: 9000000 }
      : { matches: [{ line: 102, offset: 8500000, text: "[2026-10-03][ERROR] <script>path denied</script>" }], next_offset: 9000000, eof: true, total_bytes: 9000000 } });
    return response({ ok: true, log: { path: "test.log", content: "[2026-10-03][ERROR] path denied", next_offset: 8500044, eof: true } });
  });
  const job = app.center.start("测试", { server: true });
  app.center.open(job.id);
  await app.center.searchLog(job.id, false, "ERROR");
  assert.match(app.center.ui.searchStatus.textContent, /尚未搜完整个文件/);
  assert.equal(app.center.ui.moreSearch.disabled, false);
  await app.center.searchLog(job.id, true, "ERROR");
  assert.match(app.calls[1].url, /offset=8388608/);
  assert.match(app.center.ui.searchStatus.textContent, /已到当前文件末尾/);
  assert.equal(app.center.ui.moreSearch.disabled, true);
  const hit = app.center.ui.searchMatches.children[0].children[0];
  assert.match(hit.textContent, /第 102 行/);
  assert.equal(app.all().filter((el) => el.tagName === "script").length, 0);
  hit.emit("click"); await app.flush();
  assert.match(app.calls[2].url, /offset=8500000/);
  assert.match(app.center.ui.logContent.textContent, /path denied/);
});

test("newer search wins over stale response and task switching preserves input", async () => {
  const old = deferred();
  const app = setup(async (url) => url.includes("q=old") ? old.promise : response({ ok: true, search: { matches: [{ line: 3, text: "new", offset: 30 }], next_offset: 33, eof: true, total_bytes: 33 } }));
  const job = app.center.start("搜索", { server: true }); app.center.open(job.id);
  const pending = app.center.searchLog(job.id, false, "old");
  await app.center.searchLog(job.id, false, "new");
  old.resolve(response({ ok: true, search: { matches: [{ line: 1, text: "old", offset: 0 }], next_offset: 3, eof: true } }));
  await pending;
  assert.match(app.center.ui.searchMatches.children[0].children[0].textContent, /new/);
  const other = app.center.start("另一个", { server: true }); app.center.open(other.id); app.center.open(job.id);
  assert.equal(app.center.ui.searchInput.value, "new");
});

test("open actual log uses POST with only operation id and exposes download fallback", async () => {
  const app = setup(async () => response({ ok: false, error: "未关联 .log 编辑器" }, 503));
  const job = app.center.start("日志", { server: true }); app.center.open(job.id);
  await app.center.openLogFile(job.id);
  assert.equal(app.calls[0].url, "/api/operations/" + job.id + "/log/open");
  assert.equal(app.calls[0].options.method, "POST");
  assert.equal(app.calls[0].options.body, undefined);
  assert.match(app.center.ui.logStatus.textContent, /未关联 .log 编辑器/);
  assert.match(app.center.ui.logStatus.textContent, /仍可下载/);
  assert.match(app.center.ui.downloadLog.href, /\/log\/file\?download=1$/);
});

test("warning rows and event context retain exact object, cause, paths and code location safely", async () => {
  const app = setup(async () => response({ ok: true, issues: [{ level: "warning", message: "无法创建快捷方式", action: "创建快捷方式", stage: "写入 .lnk", source_path: "U:/作品", target_path: "E:/链接.lnk", work_key: "jp-2005-1", press_key: "BDRip", reason: "权限不足", expected: false, actual: 0, location: { file: "shortcuts.py", line: 88, function: "create" }, secret: "do-not-log" }] }));
  await app.center.fetchJson("/api/collection-detail/link-index/generate-files");
  assert.equal(app.task().status, "warning");
  assert.equal(app.task().result.details[0].press_key, "BDRip");
  assert.equal(app.task().result.details[0].actual, "0");
  app.center.open(app.task().id);
  const rendered = app.all().map((el) => el.textContent).join("\n");
  for (const expected of ["权限不足", "写入 .lnk", "U:/作品", "E:/链接.lnk", "shortcuts.py:88", "jp-2005-1"]) assert.ok(rendered.includes(expected));
  assert.doesNotMatch(JSON.stringify(app.center.snapshot()), /do-not-log/);
  const task = app.center.get(app.task().id);
  app.center.merge(task, { status: "warning", events: [{ seq: 1, message: "处理失败", context: { stage: "读数据库", reason: "bad yaml", location: { file: "read.py", line: 12 } } }] });
  app.center.render();
  assert.equal(app.task().events.at(-1).context.stage, "读数据库");
  assert.match(app.all().map((el) => el.textContent).join("\n"), /read.py:12/);
});
