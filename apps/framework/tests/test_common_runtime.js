"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const vm = require("node:vm");
const { withCommonRuntime } = require("./frontend_runtime_fixture");

function setup(window = {}) {
  vm.runInNewContext(withCommonRuntime(""), { window });
  return window.NimdaCommon;
}

test("registry is reused, ignores malformed features and disposes a replaced feature once", () => {
  const window = {};
  const common = setup(window);
  const registry = common.ensureFeatureRegistry(window);
  let disposals = 0;
  const first = { id: "sample", dispose() { disposals += 1; } };
  registry.register(null);
  registry.register({});
  registry.register(first);
  registry.register(first);
  assert.equal(disposals, 0);
  const replacement = { id: "sample" };
  registry.register(replacement);
  assert.equal(disposals, 1);
  assert.equal(registry.features.length, 1);
  assert.equal(registry.features[0], replacement);
  assert.equal(common.ensureFeatureRegistry(window), registry);
});

test("HTML escapes all attribute delimiters without interpreting text or null values", () => {
  const common = setup();
  assert.equal(common.escapeHtml('<a title="x\'y">&</a>'), "&lt;a title=&quot;x&#39;y&quot;&gt;&amp;&lt;/a&gt;");
  assert.equal(common.escapeHtml(null), "");
  assert.equal(common.escapeHtml(undefined), "");
  assert.equal(common.escapeHtml(0), "0");
  assert.equal(common.escapeHtml("&lt;"), "&amp;lt;");
});

test("preferences share fallbacks and support removal without leaking storage errors", () => {
  const values = new Map();
  const storage = {
    getItem(key) { return values.get(key) || null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  const common = setup({ localStorage: storage });
  assert.equal(common.readPreference("width"), null);
  assert.equal(common.writePreference("width", "400"), true);
  assert.equal(common.readPreference("width"), "400");
  assert.equal(common.writePreference("width", null), true);
  assert.equal(common.readPreference("width"), null);
  const blocked = new Proxy({}, { get() { throw new Error("storage blocked"); } });
  assert.equal(common.readPreference("width", blocked), null);
  assert.equal(common.writePreference("width", "500", blocked), false);
  assert.equal(common.writePreference("width", null, blocked), false);
  assert.equal(common.readPreference("width"), null);
});

test("preference helpers handle a denied localStorage property", () => {
  const window = {};
  Object.defineProperty(window, "localStorage", { get() { throw new Error("storage denied"); } });
  const common = setup(window);
  assert.equal(common.readPreference("key"), null);
  assert.equal(common.writePreference("key", "value"), false);
});

test("HTTP requests always use the live operation center when available", async () => {
  const calls = [];
  const expected = { res: { status: 200 }, data: { ok: true } };
  const window = { NimdaOperationCenter: { fetchJson(...args) { calls.push(args); return expected; } } };
  const common = setup(window);
  const options = { method: "POST", body: "{}" };
  assert.equal(await common.fetchJson("/api/sample", options), expected);
  assert.deepEqual(calls, [["/api/sample", options]]);
  // Lookup is dynamic: loading the center after the common module is supported.
  const replacement = { res: { status: 201 }, data: { ok: true } };
  window.NimdaOperationCenter = { fetchJson() { return replacement; } };
  assert.equal(await common.fetchJson("/api/sample"), replacement);
});

test("HTTP fallback preserves status and treats non-JSON bodies as an empty payload", async () => {
  const res = { status: 503, ok: false, json() { return Promise.reject(new SyntaxError("not JSON")); } };
  const calls = [];
  const common = setup({ fetch(url, options) { calls.push([url, options]); return Promise.resolve(res); } });
  const result = await common.fetchJson("/api/sample");
  assert.equal(result.res, res);
  assert.equal(Object.keys(result.data).length, 0);
  assert.equal(calls[0][0], "/api/sample");
  assert.equal(Object.keys(calls[0][1]).length, 0);
});

test("HTTP fallback propagates transport failures and errors remain feature-neutral", async () => {
  const failure = new Error("offline");
  const common = setup({ fetch() { return Promise.reject(failure); } });
  await assert.rejects(common.fetchJson("/api/sample"), (error) => error === failure);
  assert.equal(common.httpErrorHint(500, "保存"), "保存失败（HTTP 500）");
  assert.match(common.httpErrorHint(404), /服务接口不可用/);
  assert.doesNotMatch(common.httpErrorHint(405), /catalog|pip install/);
});
