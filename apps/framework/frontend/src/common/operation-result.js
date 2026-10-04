(function () {
  "use strict";

  var LABELS = {
    created: "已创建", created_count: "已创建", updated: "已更新", updated_count: "已更新",
    saved: "已保存", saved_count: "已保存", written: "已写入", written_count: "已写入",
    skipped: "已跳过", skipped_count: "已跳过", skipped_existing: "已存在，跳过",
    skipped_empty_target: "目标未绑定，跳过", skipped_missing_target: "目标不存在，跳过",
    failed: "失败", failed_count: "失败", conflicts: "冲突", conflict_count: "冲突",
    total: "总计", count: "数量", file_count: "文件", directory_count: "目录", work_count: "作品",
    press_count: "压制记录", shortcut_leaves: "实际快捷方式", unmapped_on_disk: "未关联快捷方式",
    ready: "可用", missing_target: "目标不存在", missing_catalog_binding: "缺少 DB 目录绑定",
    target_mismatch: "指向不一致", already_exists: "已存在", planned: "计划", planned_count: "计划",
    matched: "已匹配", matched_count: "已匹配", repaired: "已修复", repair_count: "已修复"
  };
  var CONTAINERS = ["summary", "plan_summary", "disk_summary", "mapping_summary", "file_generation", "execution", "result", "scan", "validation", "index_db"];
  var LISTS = { errors: "error", failed: "error", issues: "warning", warnings: "warning", unmapped_shortcuts: "warning", writes: "info", repairs: "info", details: "info" };
  function text(value, limit) { return typeof value === "string" || typeof value === "number" ? String(value).slice(0, limit || 2000) : ""; }
  var CONTEXT_LABELS = {
    action: "处理操作", stage: "失败 / 当前步骤", object: "处理对象", object_type: "对象类型", name: "名称", reason: "原因", error_type: "异常类型",
    source_path: "来源路径", target_path: "目标路径", actual_target_path: "实际指向", shortcut_path: "快捷方式", link_path: "链接路径", path: "涉及路径",
    press_path: "压制目录", root: "根目录", root_path: "根路径", relpath: "相对路径", library_id: "资源库",
    work_key: "作品记录", press_key: "压制记录", yaml_source_rel: "数据库文件", index_in_file: "记录位置（从 0 开始）", record_index: "记录序号", press_index: "压制序号",
    expected: "预期", actual: "实际", target_exists: "目标存在", target_error: "目标错误", status: "状态", operation_count: "操作数",
    history_file: "历史文件", history_path: "历史路径", location: "代码位置",
    receipt_path: "整理收据", moved: "已移动文件", db_committed: "DB 已提交", db_record_count: "返回作品记录数", db_write_count: "写入 DB 文件数"
  };
  function context(value) {
    var source = value && typeof value === "object" ? value : {};
    var out = {};
    Object.keys(CONTEXT_LABELS).forEach(function (key) {
      var item = source[key];
      if (key === "location" && item && typeof item === "object") item = text(item.file, 2000) + (item.line != null ? ":" + text(item.line, 30) : "") + (item.function ? " · " + text(item.function, 200) : "");
      if (typeof item === "string" || typeof item === "number" || typeof item === "boolean") out[key] = String(item).slice(0, 4000);
    });
    return out;
  }
  function describeContext(value) {
    var safe = context(value);
    return Object.keys(safe).filter(function (key) { return safe[key] !== ""; }).map(function (key) { return { label: CONTEXT_LABELS[key], value: safe[key] }; });
  }
  function summarize(payload) {
    var result = { summary: "", counters: [], details: [], truncated: 0 };
    var seen = Object.create(null);
    function detail(value, level) {
      if (result.details.length >= 80) { result.truncated++; return; }
      var row = value && typeof value === "object" ? value : {};
      var message = text(row.message || row.error || row.reason || row.action || row.status || (typeof value === "string" ? value : ""));
      var path = text(row.shortcut_path || row.path || row.target_path || row.yaml_source_rel || context(row.context).path, 4000);
      var target = text(row.target_path, 4000);
      if (target && target !== path) message += (message ? "\n" : "") + "目标：" + target;
      if (!message && !path) return;
      level = /^(error|warning|info)$/.test(row.level) ? row.level : level;
      var fields = Object.assign({}, context(row.context), context(row));
      var key = level + "|" + message + "|" + path + "|" + JSON.stringify(fields);
      if (seen[key]) return;
      seen[key] = true;
      result.details.push(Object.assign(fields, { level: level, message: message || "处理对象", path: path, code: text(row.code, 100) }));
    }
    function organizer(value) {
      function object(row) { return row && typeof row === "object" && !Array.isArray(row); }
      function number(value) { return typeof value === "number" && isFinite(value) && value >= 0; }
      function emit(row, scope, level) {
        if (typeof row === "string") row = { message: row };
        if (!object(row)) return;
        var safe = Object.assign({}, scope, context(row.context), context(row));
        safe.message = text(row.message || row.error || row.reason || row.status || row.path || row.yaml_source_rel);
        safe.code = text(row.code, 100);
        safe.level = /^(error|warning|info)$/.test(row.level) ? row.level : level || "info";
        detail(safe, safe.level);
      }
      function rows(entries, scope, level, title) {
        if (!Array.isArray(entries)) return;
        entries.slice(0, 200).forEach(function (row) { emit(row, scope, level); });
        if (entries.length > 200) {
          result.truncated += entries.length - 200;
          emit({ message: (title || "处理明细") + "过多，摘要未展开其余 " + (entries.length - 200) + " 项" }, scope, "warning");
        }
      }
      function shortcut(preview, parent) {
        if (!object(preview)) return;
        var scope = Object.assign({}, parent, { stage: Object.prototype.hasOwnProperty.call(preview, "created") ? "快捷方式结果" : "快捷方式预检" });
        var labels = { total: "总计", creatable: "可创建", already_exists_count: "已存在", conflict_count: "冲突", created: "已创建", failed_count: "失败",
          skipped_empty_target: "空目标跳过", skipped_missing_target: "目标不存在跳过", skipped_unbound_count: "未绑定跳过", skipped_unsafe_count: "不安全目标跳过" };
        var counts = Object.keys(labels).filter(function (key) { return number(preview[key]); }).map(function (key) { return labels[key] + " " + preview[key]; });
        if (counts.length) emit({ message: "快捷方式：" + counts.join("；") }, scope);
        ["issues", "conflicts", "failed"].forEach(function (key) { rows(preview[key], scope, key === "failed" ? "error" : "warning", "快捷方式问题"); });
        if (Array.isArray(preview.items)) {
          preview.items.slice(0, 200).forEach(function (row) {
            if (!object(row)) return;
            var safe = context(row);
            if (row.actual_target) safe.actual_target_path = text(row.actual_target);
            // These are preflight items, not individual creation receipts.
            safe.message = "快捷方式预检条目：" + text(row.status || "待核对");
            emit(safe, scope);
          });
          if (preview.items.length > 200) {
            result.truncated += preview.items.length - 200;
            emit({ message: "快捷方式条目过多，摘要未展开其余 " + (preview.items.length - 200) + " 项" }, scope, "warning");
          }
        }
      }
      var plan = value.plan;
      if (object(plan) && typeof plan.child === "string" && object(plan.counts)) {
        var planScope = Object.assign({}, context(plan), { object: text(plan.child), stage: "目录整理预览" });
        var labels = { files: "文件", moves: "待移动", db_records: "关联作品", db_changes: "DB 变更", blocking_issues: "阻断问题" };
        var counts = Object.keys(labels).filter(function (key) { return number(plan.counts[key]); }).map(function (key) { return labels[key] + " " + plan.counts[key]; });
        emit({ message: "目录预览：" + (plan.can_execute === true ? "可执行" : "待核对") + (counts.length ? "；" + counts.join("；") : "") }, planScope);
        rows(plan.issues, planScope, "warning");
      }
      if (Array.isArray(value.results)) {
        value.results.slice(0, 64).forEach(function (row) {
          if (!object(row) || typeof row.child !== "string" || typeof row.plan_id !== "string" || !/^(succeeded|warning|failed|skipped)$/.test(row.status)) return;
          var scope = Object.assign({}, context(row), { object: text(row.child), stage: "目录整理结果" });
          // The outer exception flag can mean uncertain DB state / no rollback.
          // Only the writer's database receipt proves that it was committed.
          delete scope.db_committed;
          var parts = [text(row.message || row.status)], database = row.database;
          if (number(row.moved)) parts.push("已移动 " + row.moved + " 个文件");
          if (object(database)) {
            if (typeof database.db_committed === "boolean") { scope.db_committed = database.db_committed; parts.push(database.db_committed ? "DB 已提交" : "DB 未提交"); }
            [["records", "db_record_count", "返回作品记录"], ["writes", "db_write_count", "写入数据库文件"]].forEach(function (field) {
              if (Array.isArray(database[field[0]])) { scope[field[1]] = database[field[0]].length; parts.push(field[2] + " " + scope[field[1]] + " 项"); }
            });
          }
          emit({ message: parts.join("；") }, scope, row.status === "failed" ? "error" : /^(warning|skipped)$/.test(row.status) ? "warning" : "info");
          rows(row.issues, scope, "warning");
          if (object(database)) {
            var dbScope = Object.assign({}, scope, { stage: "作品 DB 保存" });
            rows(database.issues, dbScope, "warning"); rows(database.writes, dbScope, "info", "数据库写入明细");
          }
          shortcut(row.shortcut_preview, scope); shortcut(row.shortcut_result, scope);
        });
        if (value.results.length > 64) { result.truncated += value.results.length - 64; emit({ message: "批次结果过多，摘要未展开其余 " + (value.results.length - 64) + " 个目录", stage: "目录整理结果" }, {}, "warning"); }
      }
      shortcut(value.shortcut_preview, {});
      if (value.incremental === true && typeof value.plan_id === "string" && Array.isArray(value.items)) shortcut(value, {});
    }
    function inspect(value, depth) {
      if (!value || typeof value !== "object" || Array.isArray(value) || depth > 3) return;
      if (!result.summary) result.summary = text(value.message || value.error || (typeof value.summary === "string" ? value.summary : ""));
      if (typeof value.error === "string" && value.error) detail(value, "error");
      Object.keys(LABELS).forEach(function (key) {
        var count = value[key];
        if ((typeof count === "number" && isFinite(count)) || typeof count === "boolean") {
          var identity = key + ":" + count;
          if (!seen[identity] && result.counters.length < 40) {
            seen[identity] = true;
            result.counters.push({ label: LABELS[key], value: count });
          }
        }
      });
      Object.keys(LISTS).forEach(function (key) {
        if (!Array.isArray(value[key])) return;
        value[key].slice(0, 80).forEach(function (entry) { detail(entry, LISTS[key]); });
        result.truncated += Math.max(0, value[key].length - 80);
      });
      CONTAINERS.forEach(function (key) { inspect(value[key], depth + 1); });
      organizer(value);
    }
    inspect(payload, 0);
    return result;
  }
  window.NimdaOperationResults = { summarize: summarize, context: context, describeContext: describeContext };
})();
