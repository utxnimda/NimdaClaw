(function () {
  "use strict";

  var active = null;
  var nextId = 0;
  var PAGE_SIZE = 80;
  var jsonBranches = new WeakMap();
  var EMPTY = "—";

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function isObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function own(value, key) { return Object.prototype.hasOwnProperty.call(value, key); }
  function display(value) {
    if (value == null || value === "" || value === "----") return EMPTY;
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }
  function dateDisplay(value) {
    var text = display(value);
    return /^[0-9Xx]{8}$/.test(text) ? text.slice(0, 4) + "-" + text.slice(4, 6) + "-" + text.slice(6, 8) : text;
  }
  function token(value) {
    if (value === null) return "null";
    if (value === undefined) return "undefined";
    return typeof value === "string" ? JSON.stringify(value) : String(value);
  }

  // Descendants are created only on expansion, and wide objects/arrays are paged.
  function jsonNode(key, value, ancestors, initiallyOpen) {
    var branch = value !== null && typeof value === "object";
    var row = el(branch ? "details" : "div", "work-detail-json-node");
    var label = branch ? el("summary", "work-detail-json-summary") : row;
    if (key != null) label.appendChild(el("span", "work-detail-json-key", JSON.stringify(String(key)) + ": "));
    if (!branch) {
      label.appendChild(el("span", "work-detail-json-value work-detail-json-" + (value === null ? "null" : typeof value), token(value)));
      return row;
    }
    row.appendChild(label);
    if (ancestors.indexOf(value) !== -1) {
      label.appendChild(el("span", "work-detail-json-value", "[循环引用]"));
      jsonBranches.set(row, {
        open: function () { row.open = true; },
        rendered: function () { return 0; },
        hasMore: function () { return false; },
      });
      return row;
    }
    var array = Array.isArray(value);
    var keys = array ? null : Object.keys(value);
    var count = array ? value.length : keys.length;
    label.appendChild(el("span", "work-detail-json-type", array ? "Array [" + count + "]" : "Object {" + count + "}"));
    var children = el("div", "work-detail-json-children");
    var rendered = 0;
    var initialized = false;
    var more = el("button", "work-detail-more");
    more.type = "button";
    var lineage = ancestors.concat([value]);
    function appendPage() {
      more.remove();
      var limit = Math.min(rendered + PAGE_SIZE, count);
      while (rendered < limit) {
        var childKey = array ? rendered : keys[rendered];
        children.appendChild(jsonNode(childKey, value[childKey], lineage, false));
        rendered++;
      }
      if (rendered < count) {
        more.textContent = "继续显示 " + Math.min(PAGE_SIZE, count - rendered) + " 项（剩余 " + (count - rendered) + " 项）";
        children.appendChild(more);
      } else if (!count) children.appendChild(el("div", "work-detail-empty", array ? "空数组 []" : "空对象 {}"));
    }
    function populate() {
      if (initialized || !row.open) return;
      initialized = true;
      row.appendChild(children);
      appendPage();
    }
    more.addEventListener("click", appendPage);
    row.addEventListener("toggle", populate);
    jsonBranches.set(row, {
      open: function () { row.open = true; populate(); },
      appendPage: appendPage,
      rendered: function () { return rendered; },
      hasMore: function () { return rendered < count; },
      child: function (index) { return children.children[index]; },
    });
    if (initiallyOpen) { row.open = true; populate(); }
    return row;
  }

  function paged(parent, items, render) {
    var offset = 0;
    var more = el("button", "work-detail-more");
    more.type = "button";
    function appendPage() {
      more.remove();
      var limit = Math.min(offset + PAGE_SIZE, items.length);
      while (offset < limit) { render(items[offset], offset); offset++; }
      if (offset < items.length) {
        more.textContent = "继续显示 " + Math.min(PAGE_SIZE, items.length - offset) + " 项（剩余 " + (items.length - offset) + " 项）";
        parent.appendChild(more);
      }
    }
    more.addEventListener("click", appendPage);
    appendPage();
  }

  function visualRecord(parent, record, enumLabel) {
    function label(key, value) {
      if (value == null || value === "" || value === "----") return EMPTY;
      if (typeof value === "object") return display(value);
      return enumLabel ? enumLabel(key, value) || String(value) : String(value);
    }
    function section(title, container) {
      var area = el("section", "work-detail-section");
      area.appendChild(el("h3", "", title));
      (container || parent).appendChild(area);
      return area;
    }
    function grid(container) {
      var result = el("dl", "work-detail-grid");
      container.appendChild(result);
      return result;
    }
    function field(container, name, value, wide) {
      var pair = el("div", "work-detail-field" + (wide ? " work-detail-field-wide" : ""));
      pair.appendChild(el("dt", "", name));
      pair.appendChild(el("dd", "", value));
      container.appendChild(pair);
    }
    function extras(container, source, excluded, caption) {
      if (!isObject(source)) return;
      var remaining = {};
      Object.keys(source).forEach(function (key) {
        if (excluded.indexOf(key) === -1) Object.defineProperty(remaining, key, { value: source[key], enumerable: true });
      });
      if (!Object.keys(remaining).length) return;
      var block = el("div", "work-detail-extra");
      block.appendChild(el("p", "work-detail-hint", caption || "其他字段"));
      block.appendChild(jsonNode(null, remaining, [], true));
      container.appendChild(block);
    }
    function presses(container, value) {
      if (!Array.isArray(value)) {
        if (value != null) container.appendChild(jsonNode("collectioned", value, [], true));
        else container.appendChild(el("p", "work-detail-empty", "尚无压制版本"));
        return;
      }
      if (!value.length) { container.appendChild(el("p", "work-detail-empty", "尚无压制版本")); return; }
      var list = el("div", "work-detail-press-list");
      container.appendChild(list);
      paged(list, value, function (press, index) {
        var card = el("article", "work-detail-press");
        card.appendChild(el("h4", "", "版本 " + (index + 1)));
        if (isObject(press)) {
          var fields = grid(card);
          field(fields, "压制格式", label("press_format", press.press_format));
          field(fields, "压制 / 字幕组", label("press_group", press.press_group));
          field(fields, "压制目录", display(press.press_path), true);
          extras(card, press, ["press_format", "press_group", "press_path"]);
        } else card.appendChild(jsonNode(null, press, [], true));
        list.appendChild(card);
      });
    }

    var attrs = Array.isArray(record.attributes) ? record.attributes : [];
    var basics = section("基本信息");
    var basicFields = grid(basics);
    var recognizedBasic = false;
    paged(basics, attrs.filter(function (attr) {
      return isObject(attr) && ["name", "country", "date"].indexOf(attr.type) !== -1;
    }), function (attr) {
      recognizedBasic = true;
      if (attr.type === "name") field(basicFields, "作品名", display(attr.data), true);
      if (attr.type === "country") field(basicFields, "国家 / 地区", label("country", attr.data));
      if (attr.type === "date") {
        if (isObject(attr.data)) {
          field(basicFields, "开播日期", dateDisplay(attr.data.start));
          field(basicFields, "完结日期", dateDisplay(attr.data.end));
          extras(basics, attr.data, ["start", "end"], "日期的其他字段");
        } else basics.appendChild(jsonNode("date", attr.data, [], true));
      }
      extras(basics, attr, ["type", "data"], attr.type + " 属性的其他字段");
    });
    if (!recognizedBasic) basics.appendChild(el("p", "work-detail-empty", "此记录没有标准基本信息字段，可在下方或 JSON 视图查看原始内容。"));

    paged(parent, attrs.filter(function (attr) { return isObject(attr) && attr.type === "collection-type"; }), function (attr, index) {
      var area = section(index ? "收集信息 " + (index + 1) : "收集信息");
      if (!isObject(attr.data)) { area.appendChild(jsonNode("data", attr.data, [], true)); }
      else {
        var data = attr.data;
        var fields = grid(area);
        field(fields, "品类", label("domain", data.domain));
        field(fields, "播出类型", label("release_type", data.release_type));
        field(fields, "作品根目录", display(data.path), true);
        if (Array.isArray(data.markers)) field(fields, "标记", data.markers.map(function (mark) { return label("markers", mark); }).join("、") || EMPTY, true);
        else if (own(data, "markers")) area.appendChild(jsonNode("markers", data.markers, [], true));
        area.appendChild(el("h4", "work-detail-subheading", "压制版本"));
        presses(area, data.collectioned);
        if (Array.isArray(data.continuations) && data.continuations.length) {
          area.appendChild(el("h4", "work-detail-subheading", "续行 / 补充收集"));
          var continuationList = el("div", "work-detail-continuations");
          area.appendChild(continuationList);
          paged(continuationList, data.continuations, function (continuation, number) {
            var block = el("article", "work-detail-continuation");
            block.appendChild(el("h4", "", "续行 " + (number + 1) + (isObject(continuation) && continuation.title ? " · " + display(continuation.title) : "")));
            if (isObject(continuation)) {
              presses(block, continuation.collectioned);
              extras(block, continuation, ["title", "collectioned"]);
              if (own(continuation, "title") && !continuation.title) block.appendChild(jsonNode("title", continuation.title, [], false));
            } else block.appendChild(jsonNode(null, continuation, [], true));
            continuationList.appendChild(block);
          });
        } else if (own(data, "continuations") && !Array.isArray(data.continuations)) area.appendChild(jsonNode("continuations", data.continuations, [], true));
        extras(area, data, ["domain", "release_type", "path", "markers", "collectioned", "continuations"]);
      }
      extras(area, attr, ["type", "data"], "收集属性的其他字段");
    });
    var unknown = attrs.filter(function (attr) { return !isObject(attr) || ["name", "country", "date", "collection-type"].indexOf(attr.type) === -1; });
    if (unknown.length) {
      var unknownArea = section("其他作品属性");
      paged(unknownArea, unknown, function (attr) { unknownArea.appendChild(jsonNode(null, attr, [], true)); });
    }
    var topFields = Object.keys(record).filter(function (key) { return key !== "attributes"; });
    if (topFields.length || !Array.isArray(record.attributes)) {
      var other = section("其他记录字段");
      extras(other, record, Array.isArray(record.attributes) ? ["attributes"] : [], "保留数据库中的完整扩展字段");
    }
  }

  function open(options) {
    options = options || {};
    if (typeof options.loadRecord !== "function") throw new Error("作品详情缺少读取处理函数。");
    var trigger = document.activeElement;
    if (active) {
      var previous = active;
      previous.close(false);
      if (trigger && trigger.isConnected === false) trigger = previous.trigger;
    }
    var prefix = "work-detail-" + (++nextId) + "-";
    var dialog = el("dialog", "work-detail-dialog");
    var layout = el("div", "work-detail-layout");
    var header = el("header", "work-detail-header");
    var titleRow = el("div", "work-detail-title-row");
    var title = el("h2", "", options.title || "作品详情");
    title.id = prefix + "title";
    dialog.setAttribute("aria-labelledby", title.id);
    var closeButton = el("button", "work-detail-close", "关闭");
    closeButton.type = "button";
    closeButton.setAttribute("aria-label", "关闭作品详情");
    titleRow.appendChild(title);
    titleRow.appendChild(closeButton);
    header.appendChild(titleRow);
    var source = el("p", "work-detail-source", options.sourceLabel || "正在读取数据来源…");
    header.appendChild(source);
    var notice = el("p", "work-detail-notice", options.notice || "只读查看作品的完整记录；此窗口不会修改数据库、文件或快捷方式。");
    notice.id = prefix + "notice";
    dialog.setAttribute("aria-describedby", notice.id);
    header.appendChild(notice);
    var tabs = el("div", "work-detail-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "作品信息显示方式");
    var visualTab = el("button", "work-detail-tab", "可视化详情");
    var jsonTab = el("button", "work-detail-tab", "JSON 数据");
    var visual = el("div", "work-detail-visual");
    var json = el("div", "work-detail-json");
    [visualTab, jsonTab].forEach(function (button, index) {
      button.type = "button";
      button.id = prefix + "tab-" + index;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", prefix + "panel-" + index);
      tabs.appendChild(button);
    });
    [visual, json].forEach(function (panel, index) {
      panel.id = prefix + "panel-" + index;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", prefix + "tab-" + index);
      panel.tabIndex = 0;
    });
    header.appendChild(tabs);
    layout.appendChild(header);
    var body = el("div", "work-detail-body");
    var status = el("p", "work-detail-status", "正在读取作品完整信息…");
    status.setAttribute("role", "status");
    var errorPanel = el("div", "work-detail-error-panel");
    errorPanel.hidden = true;
    var error = el("p", "work-detail-error");
    error.setAttribute("role", "alert");
    var retry = el("button", "", "重新读取");
    retry.type = "button";
    errorPanel.appendChild(error);
    errorPanel.appendChild(retry);
    body.appendChild(status);
    body.appendChild(errorPanel);
    body.appendChild(visual);
    body.appendChild(json);
    layout.appendChild(body);
    dialog.appendChild(layout);
    var record = null;
    var mode = 0;
    var jsonRendered = false;
    var closed = false;
    var restoreFocus = true;
    var generation = 0;
    var loading = false;
    var jsonRoot = null;
    var jsonTree = null;
    var jsonBulkStatus = null;
    var expandButton = null;
    var bulkGeneration = 0;
    var bulkTimer = null;
    var bulkRunning = false;

    function cancelBulk(message) {
      bulkGeneration++;
      if (bulkTimer !== null) clearTimeout(bulkTimer);
      bulkTimer = null;
      bulkRunning = false;
      if (expandButton) expandButton.disabled = false;
      if (jsonTree) jsonTree.setAttribute("aria-busy", "false");
      if (jsonBulkStatus && message) jsonBulkStatus.textContent = message;
    }
    function expandAll() {
      if (closed || mode !== 1 || !record || !jsonRoot || bulkRunning) return;
      cancelBulk();
      var currentBulk = bulkGeneration;
      var pending = [{ state: jsonBranches.get(jsonRoot), offset: 0 }];
      var head = 0;
      var visited = 1;
      bulkRunning = true;
      expandButton.disabled = true;
      jsonTree.setAttribute("aria-busy", "true");
      jsonBulkStatus.textContent = "正在全部展开…可点击全部折叠停止。";
      function current() { return !closed && mode === 1 && currentBulk === bulkGeneration; }
      function runBatch() {
        bulkTimer = null;
        if (!current()) return;
        try {
          var started = Date.now();
          var steps = 0;
          // A task creates at most one 80-item page; yield after a few tasks or 8 ms.
          while (head < pending.length && steps < 4 && Date.now() - started < 8) {
            var task = pending[head];
            pending[head++] = null;
            var branch = task.state;
            branch.open();
            if (task.offset >= branch.rendered() && branch.hasMore()) branch.appendPage();
            var end = Math.min(task.offset + PAGE_SIZE, branch.rendered());
            while (task.offset < end) {
              var child = branch.child(task.offset++);
              var childBranch = jsonBranches.get(child);
              if (childBranch) pending.push({ state: childBranch, offset: 0 });
              visited++;
            }
            if (task.offset < branch.rendered() || branch.hasMore()) pending.push(task);
            steps++;
          }
          if (!current()) return;
          if (head < pending.length) {
            jsonBulkStatus.textContent = "正在全部展开…已处理 " + visited + " 个节点，可点击全部折叠停止。";
            bulkTimer = setTimeout(runBatch, 0);
          } else {
            bulkRunning = false;
            expandButton.disabled = false;
            jsonTree.setAttribute("aria-busy", "false");
            jsonBulkStatus.textContent = "已全部展开，共 " + visited + " 个节点。";
          }
        } catch (exception) {
          cancelBulk("展开失败：" + (exception && exception.message ? exception.message : "请重试。"));
        }
      }
      bulkTimer = setTimeout(runBatch, 0);
    }
    function collapseAll() {
      if (closed || mode !== 1 || !record || !jsonTree) return;
      cancelBulk("已全部折叠。再次展开时仍可逐层查看。");
      // Replace the materialized tree rather than walking thousands of descendants.
      // This also restores lazy paging and releases the expanded DOM immediately.
      clear(jsonTree);
      jsonRoot = jsonNode(null, record, [], false);
      jsonTree.appendChild(jsonRoot);
    }
    function renderJson() {
      jsonRendered = true;
      var toolbar = el("div", "work-detail-json-toolbar");
      toolbar.setAttribute("role", "group");
      toolbar.setAttribute("aria-label", "JSON 展开控制");
      expandButton = el("button", "", "全部展开");
      var collapseButton = el("button", "", "全部折叠");
      expandButton.type = collapseButton.type = "button";
      expandButton.addEventListener("click", expandAll);
      collapseButton.addEventListener("click", collapseAll);
      toolbar.appendChild(expandButton);
      toolbar.appendChild(collapseButton);
      jsonBulkStatus = el("span", "work-detail-json-bulk-status", "按需展开，也可一键查看全部层级。");
      jsonBulkStatus.setAttribute("role", "status");
      toolbar.appendChild(jsonBulkStatus);
      json.appendChild(toolbar);
      json.appendChild(el("p", "work-detail-hint", "完整原始记录。日期和空值保留数据库原值；全部展开会分批加载所有层级和分页。"));
      jsonTree = el("div", "work-detail-json-tree");
      jsonTree.addEventListener("toggle", function (event) {
        if (bulkRunning && event.target.open === false) cancelBulk("已停止全部展开，保留当前展开状态。");
      }, true);
      jsonRoot = jsonNode(null, record, [], true);
      jsonTree.appendChild(jsonRoot);
      json.appendChild(jsonTree);
    }

    function setMode(index, focus) {
      if (index !== 1 && bulkRunning) cancelBulk("已停止全部展开，保留当前展开状态。");
      mode = index;
      [visualTab, jsonTab].forEach(function (button, number) {
        button.setAttribute("aria-selected", number === mode ? "true" : "false");
        button.tabIndex = number === mode ? 0 : -1;
      });
      visual.hidden = mode !== 0 || !record;
      json.hidden = mode !== 1 || !record;
      if (mode === 1 && record && !jsonRendered) {
        renderJson();
      }
      if (focus) (mode ? jsonTab : visualTab).focus();
    }
    [visualTab, jsonTab].forEach(function (button, index) {
      button.addEventListener("click", function () { setMode(index, false); });
      button.addEventListener("keydown", function (event) {
        if (["ArrowLeft", "ArrowRight", "Home", "End"].indexOf(event.key) === -1) return;
        event.preventDefault();
        setMode(event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - mode, true);
      });
    });
    function finishClose() {
      if (closed) return;
      closed = true;
      generation++;
      cancelBulk();
      dialog.remove();
      if (active && active.dialog === dialog) active = null;
      if (restoreFocus && trigger && trigger.isConnected !== false && typeof trigger.focus === "function") trigger.focus();
    }
    function close(restore) {
      restoreFocus = restore !== false;
      dialog.close();
      finishClose();
    }
    closeButton.addEventListener("click", function () { close(true); });
    dialog.addEventListener("cancel", function (event) { event.preventDefault(); close(true); });
    dialog.addEventListener("close", finishClose);
    async function load() {
      if (closed || loading) return;
      loading = true;
      cancelBulk();
      var currentGeneration = ++generation;
      errorPanel.hidden = true;
      status.hidden = false;
      status.textContent = "正在读取作品完整信息…";
      body.setAttribute("aria-busy", "true");
      retry.disabled = true;
      try {
        var result = await options.loadRecord();
        if (closed || currentGeneration !== generation) return;
        if (!result || !isObject(result.record)) throw new Error("未读取到有效的作品记录，请重新加载收集列表后重试。");
        record = result.record;
        clear(visual);
        clear(json);
        jsonRoot = jsonTree = jsonBulkStatus = expandButton = null;
        jsonRendered = false;
        visualRecord(visual, record, options.enumLabel);
        if (typeof result.source === "string" && result.source) source.textContent = result.source;
        else if (!options.sourceLabel) source.textContent = "当前作品记录";
        status.hidden = true;
        setMode(mode, false);
      } catch (exception) {
        if (closed || currentGeneration !== generation) return;
        record = null;
        status.hidden = true;
        error.textContent = "读取失败：" + (exception && exception.message ? exception.message : "无法读取作品信息，请重试。");
        errorPanel.hidden = false;
        setMode(mode, false);
      } finally {
        if (!closed && currentGeneration === generation) {
          loading = false;
          retry.disabled = false;
          body.setAttribute("aria-busy", "false");
        }
      }
    }
    retry.addEventListener("click", load);
    setMode(0, false);
    document.body.appendChild(dialog);
    active = { dialog: dialog, close: close, trigger: trigger };
    try { dialog.showModal(); closeButton.focus(); }
    catch (exception) { finishClose(); throw exception; }
    load();
    return dialog;
  }

  window.NimdaWorkDetailDialog = { open: open };
})();
