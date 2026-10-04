"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const scope = { window: {} };
vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, "../frontend/src/common/operation-result.js"), "utf8"), scope);
const api = scope.window.NimdaOperationResults;
const summarize = (payload) => JSON.parse(JSON.stringify(api.summarize(payload)));

test("organizer preview includes counts and scoped issues without draft or records", () => {
  const output = summarize({ ok: true, plan: { child: "A", source_path: "S:/A", target_path: "S:/NewA", can_execute: false,
    counts: { files: 12, moves: 10, db_records: 2, db_changes: 1, blocking_issues: 1 },
    issues: [{ code: "name-required", message: "作品名不能为空", level: "error", blocking: true }],
    draft: { records: [{ error: "PRIVATE_DRAFT" }] }, files: [{ reason: "PRIVATE_FILES" }] } });
  assert.match(output.details[0].message, /待移动 10/);
  const issue = output.details.find((row) => row.code === "name-required");
  assert.equal(issue.object, "A");
  assert.equal(issue.source_path, "S:/A");
  assert.equal(issue.level, "error");
  assert.doesNotMatch(JSON.stringify(output), /PRIVATE_/);
});

test("batch directory results retain DB receipt and exact child warnings but never inspect DB records", () => {
  const records = [{ error: "PRIVATE_RECORD", issues: [{ message: "PRIVATE_NESTED" }] }];
  Object.defineProperty(records, "0", { get() { throw new Error("DB records must not be inspected"); } });
  const output = summarize({ ok: true, results: [
    { plan_id: "a", child: "A", status: "warning", message: "媒体与DB完成", moved: 7, receipt_path: "logs/a.json",
      database: { db_committed: true, records, writes: [{ path: "catalog/a.yaml", history_name: "backup", record: "PRIVATE_WRITE" }],
        issues: [{ code: "refresh", message: "刷新失败", reason: "Access denied", level: "warning" }] },
      issues: [{ code: "empty-cleanup", message: "空目录清理失败", path: "S:/A/old", reason: "仍有文件" }] },
    { plan_id: "b", child: "B", status: "failed", message: "媒体移动失败", db_committed: false, receipt_path: "logs/b.json", issues: [{ message: "回滚目标被占用", level: "error" }] },
    { plan_id: "c", child: "C", status: "skipped", message: "前一目录失败，未执行" }
  ] });
  const header = output.details.find((row) => row.object === "A" && row.stage === "目录整理结果");
  assert.match(header.message, /已移动 7 个文件.*DB 已提交.*返回作品记录 1 项.*写入数据库文件 1 项/);
  assert.equal(header.receipt_path, "logs/a.json");
  assert.equal(header.db_committed, "true");
  assert.ok(output.details.some((row) => row.object === "A" && row.path === "catalog/a.yaml"));
  assert.equal(output.details.find((row) => row.code === "refresh").object, "A");
  assert.equal(output.details.find((row) => row.code === "empty-cleanup").reason, "仍有文件");
  assert.ok(output.details.some((row) => row.object === "B" && row.level === "error" && row.receipt_path === "logs/b.json"));
  assert.ok(output.details.some((row) => row.object === "C" && row.level === "warning"));
  assert.doesNotMatch(JSON.stringify(output), /PRIVATE_/);
});

test("shortcut preview and direct apply distinguish preflight rows from creation results", () => {
  const preview = { incremental: true, plan_id: "shortcut", total: 2, creatable: 1, already_exists_count: 0, conflict_count: 1,
    conflicts: [{ code: "shortcut-existing-conflict", error: "目标不一致", shortcut_path: "links/a.lnk", target_path: "S:/A" }],
    items: [{ status: "conflict", shortcut_path: "links/a.lnk", target_path: "S:/A", actual_target: "S:/Other", file_sha256: "PRIVATE_HASH", record: "PRIVATE_RECORD" }],
    catalog_bindings: { records: [{ message: "PRIVATE_BINDING" }] } };
  for (const payload of [{ shortcut_preview: preview }, { results: [{ plan_id: "a", child: "A", status: "succeeded", message: "完成", shortcut_preview: preview }] }, { ...preview, created: 1, failed_count: 0 }]) {
    const output = summarize(payload);
    assert.ok(output.details.some((row) => /冲突 1/.test(row.message)));
    assert.ok(output.details.some((row) => row.code === "shortcut-existing-conflict" && row.message.includes("目标不一致")));
    const item = output.details.find((row) => row.message.startsWith("快捷方式预检条目"));
    assert.equal(item.actual_target_path, "S:/Other");
    assert.equal(item.status, "conflict");
    assert.doesNotMatch(JSON.stringify(output), /PRIVATE_/);
    if (payload.created) assert.ok(output.details.some((row) => row.stage === "快捷方式结果" && /已创建 1/.test(row.message)));
  }
});

test("new envelopes are explicitly bounded and malformed or unrelated objects are ignored", () => {
  const output = summarize({ results: Array.from({ length: 80 }, (_, index) => ({ plan_id: String(index), child: `child-${index}`, status: "succeeded", message: `child-${index}` })) });
  assert.ok(output.details.length <= 80);
  assert.ok(output.truncated >= 16);
  assert.ok(output.details.some((row) => /未展开其余 16 个目录/.test(row.message)));
  assert.ok(!output.details.some((row) => row.object === "child-64"));
  const huge = summarize({ plan: { child: "A", counts: {}, issues: Array.from({ length: 300 }, (_, index) => ({ message: `issue-${index}` })) } });
  assert.equal(huge.details.length, 80);
  assert.ok(huge.truncated >= 221);
  assert.equal(summarize({ results: [{ message: "PRIVATE_GENERIC" }], plan: { issues: [{ message: "PRIVATE_PLAN" }] }, database: { records: [{ error: "PRIVATE_DB" }] } }).details.length, 0);
  assert.doesNotThrow(() => summarize({ results: [null, [], { child: "A", plan_id: "x", status: {} }], plan: [], shortcut_preview: [] }));
});

test("existing feature summaries and diagnostic contexts remain compatible", () => {
  const output = summarize({ file_generation: { created: 2, failed: [{ error: "创建失败", shortcut_path: "links/b.lnk" }] }, writes: [{ path: "db.yaml" }] });
  assert.ok(output.counters.some((row) => row.label === "已创建" && row.value === 2));
  assert.ok(output.details.some((row) => row.path === "links/b.lnk" && row.level === "error"));
  assert.ok(output.details.some((row) => row.path === "db.yaml"));
  const contexts = api.describeContext({ receipt_path: "journal/a.json", moved: 0, db_committed: false, database: { secret: "PRIVATE" } });
  assert.deepEqual(Array.from(contexts, (row) => row.label), ["整理收据", "已移动文件", "DB 已提交"]);
  assert.doesNotMatch(JSON.stringify(contexts), /PRIVATE/);
});

test("uncertain commit protection flag is not displayed as proven DB save", () => {
  const result = summarize({ results: [{ plan_id: "a", child: "A", status: "warning", db_committed: true,
    message: "DB 写入状态无法确认，未自动回滚媒体", receipt_path: "logs/a.json" }] });
  const row = result.details[0];
  assert.match(row.message, /无法确认/);
  assert.equal(row.db_committed, undefined);
  assert.equal(row.receipt_path, "logs/a.json");
});
