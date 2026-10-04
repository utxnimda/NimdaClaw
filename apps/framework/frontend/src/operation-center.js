(function () {
  "use strict";

  var MAX_TASKS = 60;
  var MAX_EVENTS = 160;
  var LOG_PAGE_BYTES = 65536;
  var POLL_INTERVAL = 900;
  var TERMINAL = { succeeded: true, failed: true, cancelled: true, warning: true, unknown: true };
  var LABELS = { queued: "等待处理", running: "处理中", succeeded: "已完成", failed: "失败", cancelled: "已取消", warning: "部分完成 / 需留意", unknown: "结果待确认" };
  var ROUTES = {
    "/api/directory-organizer/scan": "读取待整理目录",
    "/api/directory-organizer/preview": "预览子目录整理",
    "/api/directory-organizer/execute": "执行已确认的目录整理",
    "/api/directory-organizer/catalog": "查找整理关联作品",
    "/api/directory-organizer/shortcuts": "处理本目录快捷方式",
    "/api/directory-organizer/choose-directory": "选择待整理目录",
    "/api/config": "加载应用配置", "/api/config/enum-edits": "保存枚举配置",
    "/api/browse": "读取上传的数据库文件", "/api/browse/catalog": "加载选定数据库", "/api/browse/default": "加载全部数据库", "/api/browse/save": "保存收集记录",
    "/api/collection-info": "查询收集信息",
    "/api/collection-detail/press/open": "打开压制目录",
    "/api/collection-detail/work/detail": "查看完整作品信息",
    "/api/collection-detail/link-index": "加载快捷方式索引",
    "/api/collection-detail/link-index/resolve": "解析快捷方式",
    "/api/collection-detail/link-index/validate": "检查快捷方式",
    "/api/collection-detail/link-index/fixes/apply": "修复快捷方式记录",
    "/api/collection-detail/link-index/generate": "重建索引缓存",
    "/api/collection-detail/link-index/generate-files": "补建快捷方式",
    "/api/collection-detail/link-index/open": "打开快捷方式目录",
    "/api/collection-detail/resource-libraries/scan": "扫描资源库",
    "/api/collection-detail/resource-libraries/cache": "读取资源库缓存",
    "/api/collection-detail/resource-libraries/config": "保存资源库配置",
    "/api/collection-detail/resource-libraries/node": "读取资源库目录",
    "/api/collection-detail/resource-libraries/search": "搜索资源库"
  };

  function text(value, limit) { return String(value == null ? "" : value).slice(0, limit || 2000); }
  function node(tag, className, value) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = value;
    return element;
  }
  function count(value) { return typeof value === "number" && isFinite(value) && value >= 0 ? value : null; }
  function context(value) { return window.NimdaOperationResults ? window.NimdaOperationResults.context(value) : {}; }
  function appendContext(parent, value) {
    var fields = window.NimdaOperationResults ? window.NimdaOperationResults.describeContext(value) : [];
    if (!fields.length) return;
    var list = node("dl", "operation-context");
    fields.forEach(function (field) { list.appendChild(node("dt", "", field.label)); list.appendChild(node("dd", "", field.value)); });
    parent.appendChild(list);
  }
  function time(value, fallback) {
    if (typeof value === "number") return value < 100000000000 ? value * 1000 : value;
    var parsed = Date.parse(value);
    return isNaN(parsed) ? fallback : parsed;
  }
  function elapsed(ms) {
    var seconds = Math.max(0, Math.floor(ms / 1000));
    return seconds < 60 ? seconds + " 秒" : Math.floor(seconds / 60) + " 分 " + seconds % 60 + " 秒";
  }
  function errorText(error) {
    if (typeof error === "string") return text(error);
    if (error && typeof error.message === "string") return text(error.message);
    if (error && typeof error.detail === "string") return text(error.detail);
    return "处理未完成，请查看原页面的具体提示。";
  }
  function responseWarning(data) {
    var warning = false;
    var skippedEmpty = 0;
    var skippedMissing = 0;
    function inspect(value, depth) {
      if (!value || typeof value !== "object" || depth > 3 || Array.isArray(value)) return;
      if (value.state === "partial" || value.partial_failure === true || value.partial === true || Number(value.failed_count) > 0 || Number(value.failed) > 0 ||
          ["errors", "failed", "issues", "warnings", "unmapped_shortcuts"].some(function (key) { return Array.isArray(value[key]) && value[key].length; })) warning = true;
      skippedEmpty += Number(value.skipped_empty_target) || 0;
      skippedMissing += Number(value.skipped_missing_target) || 0;
      Object.keys(value).forEach(function (key) { inspect(value[key], depth + 1); });
    }
    inspect(data, 0);
    var skipped = [];
    if (skippedEmpty) skipped.push("空目标 " + skippedEmpty + " 项");
    if (skippedMissing) skipped.push("目标不存在 " + skippedMissing + " 项");
    return { warning: warning || skipped.length > 0, message: (warning ? "部分项目未完成。" : "") + (skipped.length ? "已跳过：" + skipped.join("、") + "。" : "") };
  }
  function uuid() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (char) {
      var number = Math.floor(Math.random() * 16);
      return (char === "x" ? number : (number & 3) | 8).toString(16);
    });
  }
  function OperationCenter() {
    this.tasks = [];
    this.selectedId = null;
    this.pollTimer = null;
    this.polling = false;
    this.clockTimer = null;
    this.ui = null;
    this.bound = false;
    this.historyLoading = false;
    this.historyCursor = null;
    this.historyMessage = "";
  }
  OperationCenter.prototype.get = function (id) {
    return this.tasks.find(function (task) { return task.id === id; }) || null;
  };
  OperationCenter.prototype.snapshot = function () {
    return this.tasks.map(function (task) {
      return { id: task.id, title: task.title, path: task.path, status: task.status, message: task.message,
        started_at: task.startedAt, finished_at: task.finishedAt, completed: task.completed, total: task.total, unit: task.unit,
        result: task.result || null, log: task.log || null,
        events: task.events.map(function (event) { return Object.assign({}, event); }) };
    });
  };
  OperationCenter.prototype.event = function (task, message, details) {
    details = details || {};
    var event = { at: time(details.at, Date.now()), message: text(message), detail: text(details.detail, 4000),
      completed: count(details.completed), total: count(details.total), unit: text(details.unit, 30), context: context(details.context) };
    task.events.push(event);
    if (task.events.length > MAX_EVENTS) task.events.splice(0, task.events.length - MAX_EVENTS);
    task.version++;
    task.message = event.message;
    if (Object.prototype.hasOwnProperty.call(details, "completed")) task.completed = event.completed;
    if (Object.prototype.hasOwnProperty.call(details, "total")) task.total = event.total;
    if (Object.prototype.hasOwnProperty.call(details, "unit")) task.unit = event.unit;
  };
  OperationCenter.prototype.prune = function (limit) {
    while (this.tasks.length > limit) {
      var removable = this.tasks.findIndex(function (task) { return TERMINAL[task.status] && !task.requestPending && !task.watch; });
      if (removable < 0) break;
      this.tasks.splice(removable, 1);
    }
  };
  OperationCenter.prototype.start = function (title, options) {
    options = options || {};
    this.prune(MAX_TASKS - 1);
    var task = { id: uuid(), title: text(title || "处理事务", 150), path: text(options.path, 300), status: "running",
      startedAt: Date.now(), finishedAt: null, message: "", completed: null, total: null, unit: "", events: [], version: 0,
      lastSeq: 0, watch: !!options.server, requestPending: !!options.server, server: !!options.server,
      nextPollAt: Date.now() + 400, pollFailures: 0, missingPolls: 0, persist: options.persist !== false,
      result: null, log: null, logContent: "", logOffset: 0, logEof: false, logLoading: false, logError: "" };
    this.tasks.push(task);
    this.event(task, options.server ? "请求已发送，等待服务端开始处理。" : "开始处理。");
    this.render();
    this.schedulePoll();
    var self = this;
    return { id: task.id, progress: function (message, details) { self.progress(task.id, message, details); },
      finish: function (message) { self.finish(task.id, message); }, fail: function (error) { self.fail(task.id, error); } };
  };
  OperationCenter.prototype.progress = function (id, message, details) {
    var task = this.get(id);
    if (!task || TERMINAL[task.status]) return;
    this.event(task, message, typeof details === "string" ? { detail: details } : details);
    this.render();
  };
  OperationCenter.prototype.complete = function (id, status, message) {
    var task = this.get(id);
    if (!task) return;
    task.status = status;
    task.finishedAt = Date.now();
    this.event(task, message || LABELS[status]);
    this.prune(MAX_TASKS);
    this.render();
    if (!task.server && task.persist) this.persistClient(task);
  };
  OperationCenter.prototype.finish = function (id, message) { this.complete(id, "succeeded", message || "处理完成。"); };
  OperationCenter.prototype.fail = function (id, error) {
    var task = this.get(id);
    if (task) this.event(task, "异常详情", { detail: error && error.stack, context: { error_type: error && error.name, reason: errorText(error), action: task.title, path: task.path } });
    this.complete(id, "failed", errorText(error));
  };
  OperationCenter.prototype.notice = function (message, isError, options) {
    message = text(message, 4000).trim();
    if (!message) return;
    options = options || {};
    // In-flight captions already have tracked API lifecycle events.
    if (!isError && /(?:^正在|中(?:\.{3}|…|。)?$|请稍等|请稍候)/.test(message)) return;
    var noticeKey = message + "|" + !!isError + "|" + (options.path || "");
    if (this.lastNotice && this.lastNotice.key === noticeKey && Date.now() - this.lastNotice.at < 5000) return;
    this.lastNotice = { key: noticeKey, at: Date.now() };
    // Without an explicit request ID, never guess which concurrent job owns a notice.
    var job = this.start(isError ? "页面校验 / 处理错误" : "页面处理结果", { path: options.path || "" });
    if (options.detail || options.context) this.event(this.get(job.id), "具体处理信息", { detail: options.detail, context: options.context });
    this.complete(job.id, isError ? "failed" : "succeeded", message);
  };
  OperationCenter.prototype.persistClient = async function (task) {
    if (!task || task.server || task.persisting || task.persisted) return;
    task.persisting = true;
    try {
      var response = await fetch("/api/operations/client-events", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: task.id, title: text(task.title, 128), path: text(task.path, 512),
          status: task.status, message: text(task.message, 2048),
          details: task.events.map(function (event) {
            var fields = window.NimdaOperationResults ? window.NimdaOperationResults.describeContext(event.context).map(function (field) { return field.label + "：" + field.value; }).join("\n") : "";
            return new Date(event.at).toISOString() + " " + event.message + (event.detail ? "\n" + event.detail : "") + (fields ? "\n" + fields : "");
          }).join("\n").slice(-16000) })
      });
      var data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || "客户端日志保存失败");
      task.persisted = true;
      if (data.operation) this.merge(task, data.operation);
    } catch (error) { task.logError = "页面结果已保留，但日志写入失败：" + errorText(error); }
    finally { task.persisting = false; this.render(); }
  };
  OperationCenter.prototype.route = function (url) {
    try {
      var parsed = new URL(String(url), window.location.href);
      return parsed.origin === window.location.origin ? parsed.pathname : "";
    } catch (_) { return ""; }
  };
  OperationCenter.prototype.fetchJson = async function (url, options) {
    var opts = Object.assign({}, options || {});
    var path = this.route(url);
    var tracked = /^\/api\//.test(path) && !/^\/api\/(?:operations(?:\/|$)|health(?:\/|$))/.test(path);
    var task = null;
    if (tracked) {
      var handle = this.start(opts.operationTitle || ROUTES[path] || "处理应用请求", { path: path, server: true });
      task = this.get(handle.id);
      var headers = new Headers(opts.headers || {});
      headers.set("X-Nimda-Operation-Id", task.id);
      opts.headers = headers;
    }
    delete opts.operationTitle;
    try {
      var response = await fetch(url, opts);
      var parseFailed = false;
      var data = await response.json().catch(function () { parseFailed = true; return {}; });
      if (task) {
        task.requestPending = false;
        task.result = window.NimdaOperationResults ? window.NimdaOperationResults.summarize(data) : null;
        var failed = !response.ok || (data && data.ok === false);
        var result = responseWarning(data);
        var partial = !failed && result.warning;
        task.localOutcome = parseFailed ? "unknown" : failed ? "failed" : partial ? "warning" : "succeeded";
        this.complete(task.id, task.localOutcome,
          parseFailed ? "服务端返回的结果无法解析，暂不能确认事务结果；请先核对结果，不要直接重复提交。" : failed ? errorText((data && (data.error || data.message)) || ("请求未完成（HTTP " + response.status + "）。")) : partial ? "请求已返回。" + result.message + "具体项目见下方处理结果。" : "请求已完成，具体结果见下方。");
        task.resultMessage = task.message;
        task.finalPoll = !parseFailed;
        task.watch = true;
        task.nextPollAt = Date.now();
        this.schedulePoll();
      }
      return { res: response, data: data };
    } catch (error) {
      if (task) {
        task.requestPending = false;
        this.event(task, "请求连接异常", { detail: error && error.stack, context: { action: task.title, path: task.path, stage: "请求 / 读取响应", error_type: error && error.name, reason: errorText(error) } });
        this.complete(task.id, "unknown", "连接中断，暂不能确认处理结果；服务端可能仍在执行。请先核对结果，不要直接重复提交。");
        task.watch = true;
        task.nextPollAt = Date.now();
        this.schedulePoll();
      }
      throw error;
    }
  };
  OperationCenter.prototype.schedulePoll = function () {
    if (this.polling || this.pollTimer) return;
    var active = this.tasks.filter(function (task) { return task.watch; });
    if (!active.length) return;
    var next = Math.min.apply(null, active.map(function (task) { return task.nextPollAt; }));
    var self = this;
    this.pollTimer = setTimeout(function () { self.pollTimer = null; self.poll(); }, Math.max(0, next - Date.now()));
  };
  OperationCenter.prototype.poll = async function () {
    if (this.polling) return;
    var candidates = this.tasks.filter(function (task) { return task.watch && task.nextPollAt <= Date.now(); });
    candidates.sort(function (a, b) { return a.nextPollAt - b.nextPollAt; });
    var task = candidates[0];
    if (!task) { this.schedulePoll(); return; }
    this.polling = true;
    var finalPass = !!task.finalPoll;
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timeout = controller ? setTimeout(function () { controller.abort(); }, 6000) : null;
    try {
      var response = await fetch("/api/operations/" + encodeURIComponent(task.id) + "?after_seq=" + task.lastSeq,
        { method: "GET", cache: "no-store", signal: controller ? controller.signal : undefined });
      if (response.status === 404) {
        task.missingPolls++;
        if (task.missingPolls >= 3 || !task.requestPending) {
          task.watch = false;
          if (task.requestPending) this.event(task, "服务端暂未提供细分进度，仍在等待请求返回。");
        }
      } else {
        if (!response.ok) throw new Error("progress unavailable");
        var payload = await response.json();
        if (!payload || !payload.operation) throw new Error("progress unavailable");
        task.pollFailures = 0;
        task.missingPolls = 0;
        this.merge(task, payload.operation);
      }
    } catch (_) {
      task.pollFailures++;
      if (task.pollFailures === 3) this.event(task, "暂时无法获取详细进度；原请求不会被取消，也不会自动重试。");
      if (task.pollFailures >= 8) {
        task.watch = false;
        task.logError = "详细状态暂时不可用；可通过「历史日志 / 刷新」或「查看 / 从头刷新」重新读取，不要重复提交业务操作。";
      }
    } finally {
      if (timeout) clearTimeout(timeout);
      if (finalPass && task.pollFailures === 0) task.finalPoll = false;
      task.nextPollAt = task.finalPoll && !finalPass ? Date.now() : Date.now() + Math.min(10000, POLL_INTERVAL * Math.max(1, task.pollFailures));
      this.polling = false;
      this.prune(MAX_TASKS);
      this.render();
      this.schedulePoll();
    }
  };
  OperationCenter.prototype.merge = function (task, operation) {
    var self = this;
    (Array.isArray(operation.events) ? operation.events : []).forEach(function (event) {
      if (typeof event.seq !== "number" || event.seq <= task.lastSeq) return;
      self.event(task, event.message, event);
      task.lastSeq = event.seq;
    });
    if (typeof operation.last_seq === "number") task.lastSeq = Math.max(task.lastSeq, operation.last_seq);
    if (Object.prototype.hasOwnProperty.call(operation, "completed")) task.completed = count(operation.completed);
    if (Object.prototype.hasOwnProperty.call(operation, "total")) task.total = count(operation.total);
    if (Object.prototype.hasOwnProperty.call(operation, "unit")) task.unit = text(operation.unit, 30);
    if (operation.message) task.message = text(operation.message);
    if (task.resultMessage && (task.localOutcome === "warning" || task.localOutcome === "failed")) task.message = task.resultMessage;
    if (operation.started_at) task.startedAt = time(operation.started_at, task.startedAt);
    if (operation.result) task.result = operation.result;
    if (operation.log) task.log = operation.log;
    task.version++;
    if (LABELS[operation.status] && (task.requestPending || !task.localOutcome || task.localOutcome === "unknown" || TERMINAL[operation.status])) {
      // A backend success must not hide a partial failure already reported by its HTTP body.
      if (!((task.localOutcome === "warning" || task.localOutcome === "failed") && operation.status === "succeeded")) task.status = operation.status;
      if (TERMINAL[operation.status]) {
        task.finishedAt = time(operation.finished_at, Date.now());
        task.watch = false;
      } else task.finishedAt = null;
    }
  };
  OperationCenter.prototype.loadHistory = async function (more) {
    if (this.historyLoading) return;
    this.historyLoading = true;
    this.historyMessage = "正在读取历史日志…";
    this.render();
    try {
      var cursor = more ? this.historyCursor : null;
      var response = await fetch("/api/operations?limit=30" + (cursor ? "&before=" + encodeURIComponent(cursor) : ""), { cache: "no-store" });
      var data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || "历史日志读取失败");
      var self = this;
      this.tasks = this.tasks.filter(function (task) { return !task.historyOnly || task.watch || !TERMINAL[task.status]; });
      (Array.isArray(data.operations) ? data.operations : []).forEach(function (row) {
        var task = self.get(row.id);
        if (!task) {
          task = { id: row.id, title: ROUTES[row.title] || text(row.title, 150), path: row.path || row.title || "",
            status: "unknown", startedAt: time(row.started_at || row.updated_at, Date.now()), finishedAt: null,
            completed: null, total: null, unit: "", events: [], version: 0, lastSeq: 0,
            requestPending: false, watch: false, server: true, historyOnly: true, logContent: "", logOffset: 0, logEof: false, logError: "" };
          self.tasks.push(task);
        }
        self.merge(task, row);
        if (!TERMINAL[task.status]) {
          task.watch = true;
          task.nextPollAt = Date.now();
          task.pollFailures = 0;
          task.missingPolls = 0;
        }
      });
      this.tasks.sort(function (a, b) { return a.startedAt - b.startedAt; });
      this.historyCursor = data.next_cursor || null;
      this.historyMessage = "已读取 " + (data.operations || []).length + " 项历史记录" + (this.historyCursor ? "，可继续读取更早记录。" : "。");
    } catch (error) { this.historyMessage = errorText(error); }
    finally { this.historyLoading = false; this.render(); this.schedulePoll(); }
  };
  OperationCenter.prototype.loadLog = async function (id, next, startOffset) {
    var task = this.get(id);
    if (!task || task.logLoading) return;
    task.logLoading = true;
    task.logError = "";
    this.render();
    try {
      var offset = typeof startOffset === "number" ? Math.max(0, startOffset) : next ? task.logOffset || 0 : 0;
      var response = await fetch("/api/operations/" + encodeURIComponent(task.id) + "/log?offset=" + offset + "&limit=" + LOG_PAGE_BYTES, { cache: "no-store" });
      var data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || "日志读取失败");
      var log = data.log || {};
      task.log = Object.assign({}, task.log || {}, { path: log.path, available: true });
      task.logContent = text(log.content, LOG_PAGE_BYTES + 4096);
      task.logOffset = Number(log.next_offset) || 0;
      task.logStart = offset;
      task.logEof = !!log.eof;
      task.logLoaded = true;
    } catch (error) { task.logError = errorText(error); }
    finally {
      task.logLoading = false; this.render();
      if (typeof startOffset === "number" && this.selectedId === task.id && this.ui && this.ui.logContent.scrollIntoView) this.ui.logContent.scrollIntoView({ block: "nearest" });
    }
  };
  OperationCenter.prototype.openLogFile = async function (id) {
    var task = this.get(id);
    if (!task || task.logOpening) return;
    task.logOpening = true; task.logError = ""; task.logOpenMessage = ""; this.render();
    try {
      var response = await fetch("/api/operations/" + encodeURIComponent(task.id) + "/log/open", { method: "POST", cache: "no-store" });
      var data = await response.json();
      if (!response.ok || data.ok === false) throw new Error(data.error || "打开日志文件失败");
      task.logOpenMessage = data.message || "已请求系统打开日志文件，可使用 Ctrl+F 搜索。";
    } catch (error) { task.logError = errorText(error) + "；仍可下载日志，或在此搜索整个文件。"; }
    finally { task.logOpening = false; this.render(); }
  };
  OperationCenter.prototype.searchLog = async function (id, more, query) {
    var task = this.get(id);
    if (!task) return;
    var term = text(query == null ? task.searchDraft : query, 256).trim();
    if (!term) { task.searchError = "请输入要搜索的文字。"; task.searchRevision = (task.searchRevision || 0) + 1; this.render(); return; }
    var offset = more && term === task.searchQuery ? task.searchOffset || 0 : 0;
    var sequence = task.searchSequence = (task.searchSequence || 0) + 1;
    task.searchDraft = term; task.searchQuery = term; task.searchLoading = true; task.searchError = "";
    task.searchMatches = []; task.searchRevision = (task.searchRevision || 0) + 1; this.render();
    try {
      var response = await fetch("/api/operations/" + encodeURIComponent(task.id) + "/log/search?q=" + encodeURIComponent(term) + "&offset=" + offset + "&limit=50", { cache: "no-store" });
      var data = await response.json();
      if (sequence !== task.searchSequence) return;
      if (!response.ok || data.ok === false) throw new Error(data.error || "日志搜索失败");
      var found = data.search || {};
      task.searchMatches = (Array.isArray(found.matches) ? found.matches : []).slice(0, 50);
      task.searchOffset = Number(found.next_offset) || 0; task.searchEof = !!found.eof;
      task.searchTotalBytes = Number(found.total_bytes) || 0; task.searchTruncated = !!found.truncated;
      task.searchLoaded = true; task.searchStart = offset;
    } catch (error) { if (sequence === task.searchSequence) task.searchError = errorText(error); }
    finally { if (sequence === task.searchSequence) { task.searchLoading = false; task.searchRevision++; this.render(); } }
  };
  OperationCenter.prototype.latest = function (prefix) {
    var matching = this.tasks.filter(function (task) { return !prefix || task.path.indexOf(prefix) === 0; });
    return matching.slice().reverse().find(function (task) { return !TERMINAL[task.status]; }) || matching[matching.length - 1] || null;
  };
  OperationCenter.prototype.attachStatus = function (element) {
    if (!element || element.dataset.operationStatusBound) return;
    element.dataset.operationStatusBound = "1";
    element.setAttribute("role", "button");
    element.setAttribute("tabindex", "0");
    element.setAttribute("aria-haspopup", "dialog");
    element.setAttribute("title", "点击查看处理步骤和进度");
    element.classList.add("operation-status-link");
    var self = this;
    element.addEventListener("click", function () { self.open(); });
    element.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); self.open(); }
    });
  };
  OperationCenter.prototype.mount = function () {
    if (this.ui || !document.body) return;
    var self = this;
    var launcher = node("button", "operation-launcher", "处理详情");
    launcher.type = "button";
    launcher.setAttribute("aria-haspopup", "dialog");
    launcher.addEventListener("click", function () { self.open(); });
    document.body.appendChild(launcher);
    var dialog = node("dialog", "operation-dialog");
    dialog.setAttribute("aria-labelledby", "operation-center-title");
    var header = node("header", "operation-header");
    var title = node("h2", "", "处理详情");
    title.id = "operation-center-title";
    var close = node("button", "btn secondary", "关闭");
    close.type = "button";
    close.addEventListener("click", function () { dialog.close(); });
    header.appendChild(title);
    header.appendChild(close);
    dialog.appendChild(header);
    dialog.appendChild(node("p", "operation-explainer", "关闭窗口不会取消或重复执行。这里统一查看进度、结果与错误；服务端日志按日期保存，重启后可通过「历史日志」继续追踪。时间线仅保留近期事件，完整记录请查看日志文件。"));
    var tools = node("div", "operation-tools");
    var loadHistory = node("button", "btn secondary", "历史日志 / 刷新");
    loadHistory.type = "button";
    loadHistory.addEventListener("click", function () { self.loadHistory(false); });
    var olderHistory = node("button", "btn secondary", "更早日志");
    olderHistory.type = "button";
    olderHistory.addEventListener("click", function () { self.loadHistory(true); });
    var historyMessage = node("span", "operation-muted");
    historyMessage.setAttribute("role", "status");
    tools.appendChild(loadHistory);
    tools.appendChild(olderHistory);
    tools.appendChild(historyMessage);
    dialog.appendChild(tools);
    var body = node("div", "operation-body");
    var history = node("nav", "operation-history");
    history.setAttribute("aria-label", "最近处理任务");
    var detail = node("section", "operation-detail");
    var heading = node("h3");
    var summary = node("p", "operation-summary");
    summary.setAttribute("role", "status");
    var current = node("p", "operation-current");
    var meter = node("progress", "operation-meter");
    meter.setAttribute("aria-label", "已完成工作量");
    var metrics = node("p", "operation-metrics");
    var events = node("ol", "operation-events");
    var identity = node("p", "operation-identity");
    var result = node("section", "operation-result");
    var logs = node("details", "operation-logs");
    logs.appendChild(node("summary", "", "实际处理日志文件"));
    var logPath = node("p", "operation-log-path");
    var logControls = node("div", "operation-tools");
    var openLog = node("button", "btn secondary", "打开日志文件");
    openLog.type = "button";
    openLog.addEventListener("click", function () { self.openLogFile(self.selectedId); });
    var downloadLog = node("a", "btn secondary", "下载完整日志");
    downloadLog.setAttribute("download", "");
    var readLog = node("button", "btn secondary", "查看 / 从头刷新");
    readLog.type = "button";
    readLog.addEventListener("click", function () { self.loadLog(self.selectedId, false); });
    var nextLog = node("button", "btn secondary", "读取下一段");
    nextLog.type = "button";
    nextLog.addEventListener("click", function () { self.loadLog(self.selectedId, true); });
    logControls.appendChild(openLog);
    logControls.appendChild(downloadLog);
    logControls.appendChild(readLog);
    logControls.appendChild(nextLog);
    var logStatus = node("p", "operation-muted");
    logStatus.setAttribute("role", "status");
    var logContent = node("pre", "operation-log-content");
    logContent.setAttribute("tabindex", "0");
    var searchControls = node("form", "operation-log-search");
    var searchInput = node("input");
    searchInput.type = "search"; searchInput.maxLength = 256; searchInput.placeholder = "搜索整个日志：作品名 / 路径 / WARN / ERROR";
    searchInput.setAttribute("aria-label", "搜索整个日志文件");
    searchInput.addEventListener("input", function () { var task = self.get(self.selectedId); if (task) task.searchDraft = searchInput.value; });
    var searchButton = node("button", "btn secondary", "搜索"); searchButton.type = "submit";
    searchControls.addEventListener("submit", function (event) { event.preventDefault(); self.searchLog(self.selectedId, false, searchInput.value); });
    var moreSearch = node("button", "btn secondary", "继续搜索"); moreSearch.type = "button";
    moreSearch.addEventListener("click", function () { self.searchLog(self.selectedId, true, searchInput.value); });
    searchControls.appendChild(searchInput); searchControls.appendChild(searchButton); searchControls.appendChild(moreSearch);
    var searchStatus = node("p", "operation-muted"); searchStatus.setAttribute("role", "status");
    var searchMatches = node("ul", "operation-log-matches");
    logs.appendChild(logPath);
    logs.appendChild(logControls);
    logs.appendChild(logStatus);
    logs.appendChild(searchControls);
    logs.appendChild(searchStatus);
    logs.appendChild(searchMatches);
    logs.appendChild(logContent);
    detail.appendChild(heading);
    detail.appendChild(summary);
    detail.appendChild(current);
    detail.appendChild(meter);
    detail.appendChild(metrics);
    detail.appendChild(identity);
    detail.appendChild(result);
    detail.appendChild(logs);
    detail.appendChild(node("h4", "operation-timeline-title", "处理时间线"));
    detail.appendChild(events);
    body.appendChild(history);
    body.appendChild(detail);
    dialog.appendChild(body);
    document.body.appendChild(dialog);
    dialog.addEventListener("close", function () {
      if (self.clockTimer) { clearTimeout(self.clockTimer); self.clockTimer = null; }
      if (self.returnFocus && self.returnFocus.isConnected) self.returnFocus.focus();
    });
    this.ui = { launcher: launcher, dialog: dialog, close: close, history: history, detail: detail,
      heading: heading, summary: summary, current: current, meter: meter, metrics: metrics, events: events,
      identity: identity, result: result, resultKey: "", logs: logs, logPath: logPath, logContent: logContent,
      logStatus: logStatus, readLog: readLog, nextLog: nextLog, openLog: openLog, downloadLog: downloadLog,
      searchInput: searchInput, searchButton: searchButton, moreSearch: moreSearch, searchStatus: searchStatus, searchMatches: searchMatches,
      loadHistory: loadHistory, olderHistory: olderHistory, historyMessage: historyMessage,
      buttons: {}, eventKey: "" };
    if (!this.bound) {
      document.addEventListener("click", function (event) {
        var trigger = event.target.closest && event.target.closest("[data-operation-details]");
        if (!trigger) return;
        event.preventDefault();
        var task = self.latest(trigger.getAttribute("data-operation-prefix") || "");
        self.open(task ? task.id : undefined);
      });
      this.bound = true;
    }
    this.render();
  };
  OperationCenter.prototype.open = function (id) {
    this.mount();
    if (!this.ui) return;
    var task = this.get(id) || this.latest();
    this.selectedId = task ? task.id : null;
    if (!this.ui.dialog.open) {
      this.returnFocus = document.activeElement;
      this.ui.dialog.showModal();
    }
    this.render();
    this.ui.close.focus();
  };
  OperationCenter.prototype.render = function () {
    if (!this.ui) return;
    var self = this;
    var ui = this.ui;
    var active = this.tasks.filter(function (task) { return !TERMINAL[task.status]; }).length;
    ui.launcher.textContent = active ? "处理中 " + active + " · 查看详情" : this.tasks.length ? "处理详情 · 最近任务" : "处理详情";
    ui.launcher.classList.toggle("is-running", active > 0);
    if (!ui.dialog.open) return;
    ui.loadHistory.disabled = this.historyLoading;
    ui.olderHistory.disabled = this.historyLoading || !this.historyCursor;
    ui.historyMessage.textContent = this.historyMessage;
    Object.keys(ui.buttons).forEach(function (id) {
      if (!self.get(id)) { ui.buttons[id].remove(); delete ui.buttons[id]; }
    });
    if (!this.get(this.selectedId)) {
      var latest = this.latest();
      this.selectedId = latest ? latest.id : null;
    }
    this.tasks.slice().reverse().forEach(function (task, index) {
      var button = ui.buttons[task.id];
      if (!button) {
        button = node("button", "operation-history-item");
        button.type = "button";
        button.addEventListener("click", function () { self.selectedId = task.id; self.render(); });
        ui.buttons[task.id] = button;
      }
      button.textContent = task.title + "\n" + LABELS[task.status] + " · " + new Date(task.startedAt).toLocaleString("zh-CN", { hour12: false });
      button.setAttribute("aria-pressed", self.selectedId === task.id ? "true" : "false");
      button.dataset.state = task.status;
      if (ui.history.children[index] !== button) ui.history.insertBefore(button, ui.history.children[index] || null);
    });
    var selected = this.get(this.selectedId);
    ui.heading.textContent = selected ? selected.title : "暂无处理任务";
    ui.summary.textContent = selected ? LABELS[selected.status] + " · 用时 " + elapsed((selected.finishedAt || Date.now()) - selected.startedAt) : "执行加载、扫描、预览或保存后，可在这里查看处理进度。";
    ui.summary.dataset.state = selected ? selected.status : "";
    ui.current.textContent = selected ? selected.message : "";
    ui.identity.textContent = selected ? "操作 ID：" + selected.id + (selected.path ? "\n接口：" + selected.path : "") : "";
    var resultKey = selected ? selected.id + ":" + selected.version : "";
    if (ui.resultKey !== resultKey) {
      ui.result.replaceChildren();
      ui.result.appendChild(node("h4", "", "处理结果 / 错误明细"));
      var outcome = selected && selected.result;
      if (outcome && (outcome.summary || (outcome.counters || []).length || (outcome.details || []).length)) {
        if (outcome.summary) ui.result.appendChild(node("p", "", text(outcome.summary, 4000)));
        var counters = node("div", "operation-counters");
        (outcome.counters || []).slice(0, 40).forEach(function (counter) {
          counters.appendChild(node("span", "", text(counter.label, 100) + "：" + text(counter.value, 100)));
        });
        ui.result.appendChild(counters);
        var problems = node("ul", "operation-result-items");
        (outcome.details || []).slice(0, 80).forEach(function (row) {
          var item = node("li");
          item.dataset.level = row.level || "info";
          item.appendChild(node("p", "", (row.code ? "[" + text(row.code, 100) + "] " : "") + text(row.message, 4000)));
          appendContext(item, Object.assign({}, context(row.context), context(row)));
          problems.appendChild(item);
        });
        ui.result.appendChild(problems);
        if (outcome.truncated) ui.result.appendChild(node("p", "operation-muted", "面板摘要还有 " + outcome.truncated + " 项未展开；请查看实际日志文件中的完整记录。"));
      } else ui.result.appendChild(node("p", "operation-muted", selected && TERMINAL[selected.status] ? selected.message : "处理结束后在这里显示结果与问题；当前步骤见时间线。"));
      ui.resultKey = resultKey;
    }
    var log = selected && selected.log;
    ui.logs.hidden = !selected;
    ui.logPath.textContent = log && log.path ? log.path : selected && !selected.server ? "仅页面记录；持久化后可查看日志文件。" : "日志文件将在服务端接收操作后生成。";
    ui.readLog.disabled = !selected || selected.logLoading || (!selected.server && !selected.persisted);
    ui.openLog.disabled = !selected || selected.logOpening || (!selected.server && !selected.persisted);
    ui.openLog.textContent = selected && selected.logOpening ? "正在打开…" : "打开日志文件";
    ui.downloadLog.hidden = !selected || (!selected.server && !selected.persisted);
    ui.downloadLog.href = selected ? "/api/operations/" + encodeURIComponent(selected.id) + "/log/file?download=1" : "#";
    ui.nextLog.disabled = !selected || selected.logLoading || !selected.logLoaded;
    ui.nextLog.textContent = selected && selected.logEof ? "读取新增日志" : "读取下一段";
    ui.logStatus.textContent = !selected ? "" : selected.logLoading ? "正在读取日志…" : selected.logError || (log && log.error) || selected.logOpenMessage || (selected.logLoaded ? "当前显示字节 " + (selected.logStart || 0) + "–" + selected.logOffset + (selected.logEof ? "，已到当前文件末尾。" : "；还有后续日志。") : "实际 UTF-8 .log 文件；可直接用系统日志查看器打开。内嵌查看每段 64 KiB，搜索针对整个文件。");
    ui.logContent.textContent = selected ? selected.logContent || "" : "";
    if (ui.searchTask !== (selected && selected.id)) { ui.searchInput.value = selected ? selected.searchDraft || "" : ""; ui.searchTask = selected && selected.id; }
    ui.searchInput.disabled = !selected || (!selected.server && !selected.persisted);
    ui.searchButton.disabled = ui.searchInput.disabled;
    ui.moreSearch.disabled = !selected || selected.searchLoading || !selected.searchLoaded || selected.searchEof;
    ui.searchStatus.textContent = !selected ? "" : selected.searchLoading ? "正在搜索日志文件…" : selected.searchError || (selected.searchLoaded ? "搜索「" + selected.searchQuery + "」：本段找到 " + (selected.searchMatches || []).length + " 处；已检查至字节 " + selected.searchOffset + " / " + selected.searchTotalBytes + (selected.searchEof ? "，已到当前文件末尾。" : "，尚未搜完整个文件，请继续搜索。") + (selected.searchTruncated ? " 超长行仅显示片段，请打开完整文件查看。" : "") : "支持普通文字搜索（不区分大小写），点击结果定位；每批最多 50 个匹配。");
    var searchKey = selected ? selected.id + ":" + (selected.searchRevision || 0) : "";
    if (ui.searchKey !== searchKey) {
      ui.searchMatches.replaceChildren();
      (selected && selected.searchMatches || []).forEach(function (match) {
        var item = node("li");
        var button = node("button", "operation-log-match", "第 " + text(match.line, 20) + " 行 · " + text(match.text, 4000));
        button.type = "button";
        var selectedId = selected.id;
        button.addEventListener("click", function () { self.loadLog(selectedId, false, Number(match.offset) || 0); });
        item.appendChild(button); ui.searchMatches.appendChild(item);
      });
      ui.searchKey = searchKey;
    }
    var known = selected && selected.total != null && selected.total > 0 && selected.completed != null;
    ui.meter.hidden = !selected || !known;
    if (known) { ui.meter.max = selected.total; ui.meter.value = Math.min(selected.completed, selected.total); }
    ui.metrics.textContent = !selected ? "" : known ? "已处理 " + selected.completed + " / " + selected.total + (selected.unit ? " " + selected.unit : "") : selected.completed != null ? "已处理 " + selected.completed + " " + selected.unit + " · 总量尚未确定" : "当前步骤未提供工作总量，不估算百分比。";
    var eventKey = selected ? selected.id + ":" + selected.version : "";
    if (ui.eventKey !== eventKey) {
      var nearBottom = ui.events.scrollHeight - ui.events.scrollTop - ui.events.clientHeight < 45;
      ui.events.replaceChildren();
      (selected ? selected.events.slice().sort(function (a, b) { return a.at - b.at; }) : []).forEach(function (event) {
        var item = node("li", "operation-event");
        item.appendChild(node("time", "", new Date(event.at).toLocaleTimeString("zh-CN", { hour12: false })));
        var description = node("div");
        description.appendChild(node("span", "", event.message));
        if (event.detail) description.appendChild(node("pre", "operation-event-detail", event.detail));
        appendContext(description, event.context);
        if (event.completed != null) description.appendChild(node("small", "", "已处理 " + event.completed + (event.total != null ? " / " + event.total : "") + " " + event.unit));
        item.appendChild(description);
        ui.events.appendChild(item);
      });
      if (nearBottom || ui.eventKey.split(":")[0] !== (selected && selected.id)) ui.events.scrollTop = ui.events.scrollHeight;
      ui.eventKey = eventKey;
    }
    if (active && !this.clockTimer) {
      this.clockTimer = setTimeout(function () { self.clockTimer = null; self.render(); }, 1000);
    }
  };

  window.NimdaOperationCenter = new OperationCenter();
  window.NimdaOperationCenter.OperationCenter = OperationCenter;
  if (typeof window.addEventListener === "function") {
    window.addEventListener("error", function (event) {
      if (event && (event.error || event.message)) {
        window.NimdaOperationCenter.notice("页面运行错误：" + errorText(event.error || event.message), true, {
          detail: event.error && event.error.stack, context: { stage: "页面运行", reason: errorText(event.error || event.message), error_type: event.error && event.error.name,
            location: { file: text(event.filename, 2000).split(/[?#]/)[0], line: event.lineno } }
        });
      }
    });
    window.addEventListener("unhandledrejection", function (event) {
      window.NimdaOperationCenter.notice("页面异步处理错误：" + errorText(event && event.reason), true, { detail: event && event.reason && event.reason.stack,
        context: { stage: "页面异步处理", reason: errorText(event && event.reason), error_type: event && event.reason && event.reason.name } });
    });
  }
  if (document.body) window.NimdaOperationCenter.mount();
  else document.addEventListener("DOMContentLoaded", function () { window.NimdaOperationCenter.mount(); }, { once: true });
})();
