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
  vm.runInNewContext(fs.readFileSync(SOURCE, "utf8"), {
    window, document, console, URL, Headers, AbortController, Date: ClockDate,
    fetch(url, options) { calls.push({ url, options }); return fetcher(url, options); },
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, at: now + delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  }, { filename: SOURCE });
  async function flush() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
  return { center: window.NimdaOperationCenter, document, calls, timers,
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
  assert.equal(app.calls.length, 0);
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
  await assert.rejects(app.center.fetchJson("/api/media-directory-organizer/apply", { method: "POST" }), (error) => error === networkError);
  assert.equal(app.task().status, "unknown");
  assert.match(app.task().message, /不要直接重复提交/);
  await app.advance(1000);
  assert.equal(app.task().status, "queued");
  await app.advance(1000);
  assert.equal(app.task().status, "running");
  assert.equal(app.task().finished_at, null);
  await app.advance(1000);
  assert.equal(app.task().status, "succeeded");
  assert.equal(app.calls.filter((call) => call.url === "/api/media-directory-organizer/apply").length, 1);
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
  const pending = app.center.fetchJson("/api/media-directory-organizer/preview");
  const selected = app.task().id;
  app.center.start("其它模块", { path: "/api/browse/default" });
  const trigger = app.document.body.appendChild(app.document.createElement("button"));
  trigger.setAttribute("data-operation-details", "");
  trigger.setAttribute("data-operation-prefix", "/api/media-directory-organizer/");
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
