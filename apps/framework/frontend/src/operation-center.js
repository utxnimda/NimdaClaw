(function () {
  "use strict";

  var MAX_TASKS = 60;
  var MAX_EVENTS = 160;
  var POLL_INTERVAL = 900;
  var TERMINAL = { succeeded: true, failed: true, cancelled: true, warning: true, unknown: true };
  var LABELS = { queued: "等待处理", running: "处理中", succeeded: "已完成", failed: "失败", cancelled: "已取消", warning: "部分完成 / 需留意", unknown: "结果待确认" };
  var ROUTES = {
    "/api/config": "加载应用配置", "/api/config/enum-edits": "保存枚举配置",
    "/api/browse": "读取上传的数据库文件", "/api/browse/catalog": "加载选定数据库", "/api/browse/default": "加载全部数据库", "/api/browse/save": "保存收集记录",
    "/api/collection-info": "查询收集信息",
    "/api/collection-detail/press/open": "打开压制目录",
    "/api/collection-detail/work/detail": "查看完整作品信息",
    "/api/collection-detail/link-index": "加载快捷方式索引",
    "/api/collection-detail/link-index/resolve": "解析快捷方式",
    "/api/collection-detail/link-index/validate": "检查快捷方式",
    "/api/collection-detail/link-index/fixes/apply": "修复快捷方式记录",
    "/api/collection-detail/link-index/generate": "重新生成索引数据库",
    "/api/collection-detail/link-index/generate-files": "生成目录文件（含预检）",
    "/api/collection-detail/link-index/open": "打开快捷方式目录",
    "/api/collection-detail/resource-libraries/scan": "扫描资源库",
    "/api/collection-detail/resource-libraries/cache": "读取资源库缓存",
    "/api/collection-detail/resource-libraries/config": "保存资源库配置",
    "/api/collection-detail/resource-libraries/node": "读取资源库目录",
    "/api/collection-detail/resource-libraries/search": "搜索资源库",
    "/api/media-directory-organizer/config": "加载归类配置",
    "/api/media-directory-organizer/preview": "预览媒体归类",
    "/api/media-directory-organizer/apply": "执行媒体归类",
    "/api/media-directory-organizer/landing/suggest": "识别作品信息",
    "/api/media-directory-organizer/landing/preview": "预览作品落地",
    "/api/media-directory-organizer/landing/apply": "保存作品并归类",
    "/api/media-directory-organizer/landing/repair/preview": "预览数据库与目录修复",
    "/api/media-directory-organizer/landing/repair/apply": "修复数据库并归类",
    "/api/media-directory-organizer/landing/shortcuts/preview": "检查并预览快捷方式",
    "/api/media-directory-organizer/landing/shortcuts/apply": "补建快捷方式"
  };

  function text(value, limit) { return String(value == null ? "" : value).slice(0, limit || 2000); }
  function node(tag, className, value) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (value != null) element.textContent = value;
    return element;
  }
  function count(value) { return typeof value === "number" && isFinite(value) && value >= 0 ? value : null; }
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
          (Array.isArray(value.errors) && value.errors.length) || (Array.isArray(value.failed) && value.failed.length)) warning = true;
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
  }
  OperationCenter.prototype.get = function (id) {
    return this.tasks.find(function (task) { return task.id === id; }) || null;
  };
  OperationCenter.prototype.snapshot = function () {
    return this.tasks.map(function (task) {
      return { id: task.id, title: task.title, path: task.path, status: task.status, message: task.message,
        started_at: task.startedAt, finished_at: task.finishedAt, completed: task.completed, total: task.total, unit: task.unit,
        events: task.events.map(function (event) { return Object.assign({}, event); }) };
    });
  };
  OperationCenter.prototype.event = function (task, message, details) {
    details = details || {};
    var event = { at: time(details.at, Date.now()), message: text(message), detail: text(details.detail, 4000),
      completed: count(details.completed), total: count(details.total), unit: text(details.unit, 30) };
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
      lastSeq: 0, watch: !!options.server, requestPending: !!options.server, nextPollAt: Date.now() + 400, pollFailures: 0, missingPolls: 0 };
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
  };
  OperationCenter.prototype.finish = function (id, message) { this.complete(id, "succeeded", message || "处理完成。"); };
  OperationCenter.prototype.fail = function (id, error) { this.complete(id, "failed", errorText(error)); };
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
        var failed = !response.ok || (data && data.ok === false);
        var result = responseWarning(data);
        var partial = !failed && result.warning;
        task.localOutcome = parseFailed ? "unknown" : failed ? "failed" : partial ? "warning" : "succeeded";
        this.complete(task.id, task.localOutcome,
          parseFailed ? "服务端返回的结果无法解析，暂不能确认事务结果；请先核对结果，不要直接重复提交。" : failed ? errorText((data && (data.error || data.message)) || ("请求未完成（HTTP " + response.status + "）。")) : partial ? "请求已返回。" + result.message + "请查看原页面结果。" : "请求已完成，结果已返回页面。");
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
      if (task.pollFailures >= 8) task.watch = false;
    } finally {
      if (timeout) clearTimeout(timeout);
      if (finalPass) task.watch = false;
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
    if (LABELS[operation.status] && (task.requestPending || !task.localOutcome || task.localOutcome === "unknown" || TERMINAL[operation.status])) {
      // A backend success must not hide a partial failure already reported by its HTTP body.
      if (!((task.localOutcome === "warning" || task.localOutcome === "failed") && operation.status === "succeeded")) task.status = operation.status;
      if (TERMINAL[operation.status]) {
        task.finishedAt = time(operation.finished_at, Date.now());
        task.watch = false;
      } else task.finishedAt = null;
    }
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
    dialog.appendChild(node("p", "operation-explainer", "关闭此窗口不会取消处理，也不会重复执行。进行中的任务全部保留；最多保留 60 项已完成历史，每项最多 160 条记录。"));
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
    detail.appendChild(heading);
    detail.appendChild(summary);
    detail.appendChild(current);
    detail.appendChild(meter);
    detail.appendChild(metrics);
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
      heading: heading, summary: summary, current: current, meter: meter, metrics: metrics, events: events, buttons: {}, eventKey: "" };
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
      button.textContent = task.title + "\n" + LABELS[task.status];
      button.setAttribute("aria-pressed", self.selectedId === task.id ? "true" : "false");
      button.dataset.state = task.status;
      if (ui.history.children[index] !== button) ui.history.insertBefore(button, ui.history.children[index] || null);
    });
    var selected = this.get(this.selectedId);
    ui.heading.textContent = selected ? selected.title : "暂无处理任务";
    ui.summary.textContent = selected ? LABELS[selected.status] + " · 用时 " + elapsed((selected.finishedAt || Date.now()) - selected.startedAt) : "执行加载、扫描、预览或保存后，可在这里查看处理进度。";
    ui.summary.dataset.state = selected ? selected.status : "";
    ui.current.textContent = selected ? selected.message : "";
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
  if (document.body) window.NimdaOperationCenter.mount();
  else document.addEventListener("DOMContentLoaded", function () { window.NimdaOperationCenter.mount(); }, { once: true });
})();
