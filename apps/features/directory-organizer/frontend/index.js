(function () {
  "use strict";

  var API = "/api/directory-organizer/";
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function text(value) { return String(value == null ? "" : value); }
  function list(value) { return Array.isArray(value) ? value : []; }
  function message(error) { return error && error.message ? error.message : text(error); }
  function attribute(record, type, fallback) {
    if (!Array.isArray(record.attributes)) record.attributes = [];
    var row = record.attributes.find(function (item) { return item && item.type === type; });
    if (!row) { row = { type: type, data: clone(fallback) }; record.attributes.push(row); }
    return row;
  }
  function recordName(record) {
    var found = list(record && record.attributes).find(function (row) { return row.type === "name"; });
    return found ? text(found.data) : "未命名作品";
  }
  function template(name) {
    return { attributes: [
      { type: "name", data: name || "" }, { type: "country", data: "japan" },
      { type: "date", data: { start: "", end: "" } },
      { type: "collection-type", data: { domain: "animation", release_type: "tv", path: "",
        markers: [], collectioned: [{ press_format: "", press_group: "", press_path: "" }], continuations: [] } }
    ] };
  }
  function isoDate(value) { return text(value).replace(/^(\d{4})(\d{2})(\d{2})$/, "$1-$2-$3"); }
  function identity(ref) { return ref ? text(ref.yaml_source_rel) + ":" + text(ref.index_in_file) : ""; }
  function pressRows(record) {
    var found = list(record.attributes).find(function (row) { return row.type === "collection-type"; });
    var collection = found && found.data || {}, rows = list(collection.collectioned).slice();
    list(collection.continuations).forEach(function (part) { rows = rows.concat(list(part.collectioned)); });
    return rows;
  }
  function selectedPositions(entry) {
    var rows = pressRows(entry.record), selected = list(entry.press_keys).map(function (key) { return Number(text(key).split(":")[0]); });
    return selected.length ? selected : !entry.ref && rows.length === 1 ? [0] : [];
  }
  function validateRecordStructure(record) {
    function object(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
    function objectRows(value, label) {
      if (!Array.isArray(value) || value.some(function (row) { return !object(row); })) throw new Error(label + " 必须是对象数组，不能包含 null");
    }
    objectRows(record.attributes, "attributes");
    record.attributes.forEach(function (attr) {
      if (attr.type === "date" || attr.type === "collection-type") {
        if (!object(attr.data)) throw new Error(attr.type + ".data 必须是对象，不能为 null 或数组");
      }
      if (attr.type !== "collection-type") return;
      var collection = attr.data;
      if (Object.prototype.hasOwnProperty.call(collection, "collectioned")) objectRows(collection.collectioned, "collectioned");
      if (Object.prototype.hasOwnProperty.call(collection, "continuations")) {
        objectRows(collection.continuations, "continuations");
        collection.continuations.forEach(function (part) {
          if (Object.prototype.hasOwnProperty.call(part, "collectioned")) objectRows(part.collectioned, "continuations.collectioned");
        });
      }
    });
  }
  function synchronizeTarget(draft) {
    list(draft.records).forEach(function (entry) {
      var selected = selectedPositions(entry); if (!selected.length) return;
      attribute(entry.record, "collection-type", {}).data.path = draft.work_root;
      pressRows(entry.record).forEach(function (press, index) { if (selected.indexOf(index) >= 0) press.press_path = draft.release_name; });
    });
  }
  function resetValidJsonTexts(item) {
    Object.keys(item.jsonTexts).forEach(function (key) { if (!item.jsonErrors[key]) delete item.jsonTexts[key]; });
  }
  function shortcutCreatable(preview) {
    return preview.creatable == null ? list(preview.items).filter(function (row) { return row.status === "planned"; }).length : Number(preview.creatable) || 0;
  }
  function shortcutBlocked(preview) {
    return Number(preview.conflict_count) > 0 || list(preview.conflicts).length > 0;
  }
  function fileDestination(item, row) {
    if (item.draft && item.draft.completed) return row.source_rel;
    var overrides = item.draft && item.draft.file_targets || {};
    return Object.prototype.hasOwnProperty.call(overrides, row.source_rel) ? overrides[row.source_rel] : row.target_rel;
  }
  function normalizedPath(value) { return text(value).replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase(); }
  function fileMoved(item, row) {
    var plan = item.plan || item.lastPlan || {}, destination = fileDestination(item, row);
    if (item.plan && typeof row.changed === "boolean") return row.changed;
    if (item.draft && item.draft.completed) return false;
    var target = item.draft ? text(item.draft.work_root) + "/" + text(item.draft.release_name) : plan.target_path;
    var source = plan.source_path || item.path;
    return !!(source && target && normalizedPath(source) !== normalizedPath(target)) || normalizedPath(row.source_rel) !== normalizedPath(destination);
  }
  function planOverview(item) {
    var plan = item.plan || item.lastPlan, draft = item.draft || {}, records = list(draft.records);
    if (!plan) return { media: "待预览", database: "DB 待核对", moves: 0, files: 0, unchanged: 0, records: records.length, changes: 0, blocking: 0 };
    var counts = plan.counts || {}, files = list(plan.files);
    var moves = item.plan && counts.moves != null ? Number(counts.moves) : files.filter(function (row) { return fileMoved(item, row); }).length;
    if (!item.plan) return { media: item.result ? "媒体结果见处理详情" : "目录待重新预览", database: item.result ? "DB 结果见处理详情" : "DB 待重新预览",
      outdated: true, historical: !!item.result, moves: moves, files: files.length, unchanged: Math.max(0, files.length - moves), records: records.length, changes: null, blocking: null };
    var fileCount = counts.files == null ? files.length : Number(counts.files);
    var changes = counts.db_changes == null ? list(plan.db_changes).length : Number(counts.db_changes);
    var missing = records.filter(function (entry) { return !entry.ref; }).length;
    var blocking = counts.blocking_issues == null ? list(plan.issues).filter(function (issue) { return issue.blocking; }).length : Number(counts.blocking_issues);
    var databaseIssues = list(plan.issues).some(function (issue) { return issue.blocking && /catalog|record|binding|press-selection|name-required|format-required|date/.test(text(issue.code)); });
    return { media: plan.media_status === "unresolved" ? "目录待核对" : moves ? "待移动 " + moves + " 个文件" : "目录已整理",
      database: plan.database_status === "matched" ? "DB 已关联" : plan.database_status === "needs_sync" ? "DB 待同步" :
        missing || !records.length ? "DB 待补齐" : plan.database_status === "unresolved" || databaseIssues ? "DB 待核对" : changes ? "DB 待同步" : "DB 已关联",
      moves: moves, files: fileCount, unchanged: Math.max(0, fileCount - moves), records: counts.db_records == null ? records.length : Number(counts.db_records), changes: changes, blocking: blocking };
  }

  function createController(options) {
    options = options || {};
    var state = { root: "", scannedRoot: "", children: [], activeId: "", strategies: [],
      rootFiles: [], issues: [], scanning: false, choosing: false, executing: false,
      generateShortcuts: false, error: "", generation: 0, disposed: false,
      autoPreviewSerial: 0, autoPreview: { running: false, completed: 0, total: 0, limit: 64, cancelled: false } };
    var autoPreviewLimit = options.autoPreviewLimit == null ? 64 : Math.max(0, Math.min(64, Number(options.autoPreviewLimit) || 0));
    function changed(reason, itemId) { if (!state.disposed && options.onChange) options.onChange(state, reason, itemId); }
    function notify(value, error) { if (!state.disposed && options.setStatus) options.setStatus(value, !!error); }
    async function request(route, payload, onOperation) {
      var result = await options.request(API + route, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      var headers = result && result.res && result.res.headers;
      if (onOperation && headers && typeof headers.get === "function") onOperation(headers.get("X-Nimda-Operation-Id") || "");
      var data = result && result.data !== undefined ? result.data : result;
      if (!data || data.ok === false || (result.res && !result.res.ok)) throw new Error(data && (data.error || data.message) || "目录整理请求失败");
      return data;
    }
    function child(id) { return state.children.find(function (item) { return item.id === id; }); }
    function editable(item) { return item && !state.executing && !item.executed && !item.locked; }
    function invalidate(item) {
      item.version += 1; item.locked = false; item.plan = null; item.error = ""; item.result = null;
    }
    function setRoot(value) {
      if (state.executing) return;
      if (state.root === value) return;
      state.root = text(value); state.generation += 1; state.scanning = false; state.choosing = false;
      state.autoPreviewSerial += 1; state.autoPreview = { running: false, completed: 0, total: 0, limit: 64, cancelled: false };
      state.children = []; state.scannedRoot = ""; state.activeId = ""; state.rootFiles = []; state.issues = [];
      changed("root");
    }
    async function choose() {
      if (state.executing || state.choosing) return;
      var generation = state.generation; state.choosing = true; changed("busy");
      try {
        var result = await request("choose-directory", { root: state.root });
        if (generation !== state.generation || state.disposed) return;
        if (!result.cancelled && result.path) setRoot(result.path);
      } catch (error) { notify(message(error), true); }
      finally { if (generation === state.generation) { state.choosing = false; changed("busy"); } }
    }
    async function scan() {
      if (state.executing || state.scanning || !state.root.trim()) return;
      var generation = ++state.generation, requestedRoot = state.root, autoSerial = state.autoPreviewSerial;
      state.scanning = true; state.error = ""; changed("busy");
      try {
        var data = await request("scan", { root: requestedRoot });
        if (generation !== state.generation || state.disposed) return;
        state.root = data.root || requestedRoot; state.scannedRoot = state.root;
        state.strategies = list(data.strategies); state.rootFiles = list(data.root_files); state.issues = list(data.issues);
        state.children = list(data.children).map(function (row) {
          return Object.assign({}, row, { id: text(row.id || row.name), selected: false, strategy: "auto", draft: undefined,
            plan: null, lastPlan: null, previewing: false, locked: false, version: 0, error: "", jsonErrors: {}, jsonTexts: {},
            candidates: [], query: "", searching: false, searchVersion: 0, result: null, executed: false, expanded: Object.create(null) });
        });
        state.activeId = state.children.length ? state.children[0].id : "";
        notify("已扫描 " + state.children.length + " 个子目录。");
        state.scanning = false; changed("scan");
        if (autoPreviewLimit && autoSerial === state.autoPreviewSerial) await previewAutomatically(generation);
      } catch (error) { if (generation === state.generation) { state.error = message(error); notify(state.error, true); } }
      finally { if (generation === state.generation) { state.scanning = false; changed("scan"); } }
    }
    async function previewAutomatically(generation) {
      var serial = ++state.autoPreviewSerial, rows = state.children.slice(0, autoPreviewLimit);
      state.autoPreview = { running: true, completed: 0, total: rows.length, limit: autoPreviewLimit, cancelled: false }; changed("auto-preview");
      for (var index = 0; index < rows.length; index++) {
        if (state.disposed || generation !== state.generation || serial !== state.autoPreviewSerial) break;
        await preview(rows[index].id);
        if (generation === state.generation && serial === state.autoPreviewSerial) { state.autoPreview.completed = index + 1; changed("auto-preview"); }
      }
      if (generation === state.generation && serial === state.autoPreviewSerial) { state.autoPreview.running = false; changed("auto-preview"); }
    }
    function stopAutoPreview() {
      state.autoPreviewSerial += 1;
      if (state.autoPreview.running) { state.autoPreview.running = false; state.autoPreview.cancelled = true; changed("auto-preview"); }
    }
    function edit(id, mutate, reason) {
      var item = child(id); if (!editable(item)) return false;
      if (!item.draft) item.draft = {};
      mutate(item.draft, item); invalidate(item); changed(reason || "edit"); return true;
    }
    async function preview(id) {
      var item = child(id); if (!editable(item) || item.previewing || Object.keys(item.jsonErrors).length) return;
      if (!item.plan && state.children.filter(function (row) { return !!row.plan; }).length >= 64) {
        state.children.forEach(function (row) { if (row.plan) { row.plan = null; row.locked = false; } });
        notify("预览缓存最多保留 64 项；继续预览前已解除上一批确认，执行前请逐项重新预览。", true);
      }
      var generation = state.generation, version = item.version;
      item.previewing = true; item.error = ""; changed("busy", item.id);
      try {
        var payload = { root: state.scannedRoot, child: item.name, strategy: item.strategy };
        if (item.draft) payload.draft = clone(item.draft);
        var data = await request("preview", payload, function (id) { if (generation === state.generation && version === item.version) item.previewOperationId = id; });
        if (generation !== state.generation || version !== item.version || state.disposed) return;
        item.plan = data.plan; item.lastPlan = data.plan; item.draft = clone(data.plan.draft || {}); item.jsonTexts = {};
        item.status = data.plan.status; item.candidates = list(data.plan.catalog_candidates); item.locked = false;
        notify(item.name + "：" + (data.plan.summary || "预览已更新，请检查并确认。"), !data.plan.can_execute);
      } catch (error) {
        if (generation === state.generation && version === item.version) { item.error = message(error); notify(item.name + "：" + item.error, true); }
      } finally { item.previewing = false; if (generation === state.generation) changed("preview", item.id); }
    }
    function lock(id) {
      var item = child(id);
      if (!editable(item) || item.previewing || !item.plan || !item.plan.can_execute || Object.keys(item.jsonErrors).length) return false;
      item.locked = true; changed("lock"); return true;
    }
    function unlock(id) {
      var item = child(id); if (!item || state.executing || item.executed) return;
      invalidate(item); changed("unlock");
    }
    async function search(id, query) {
      var item = child(id); if (!editable(item)) return;
      var generation = state.generation, serial = ++item.searchVersion;
      item.query = text(query); item.searching = true; changed("busy", item.id);
      try {
        var data = await request("catalog", { query: item.query, limit: 50 });
        if (generation !== state.generation || serial !== item.searchVersion || state.disposed) return;
        item.candidates = list(data.records); item.searchSummary = "共 " + text(data.total == null ? item.candidates.length : data.total) + " 条" + (data.truncated ? "，请缩小搜索范围" : "");
      } catch (error) { if (generation === state.generation) notify(message(error), true); }
      finally { if (serial === item.searchVersion) { item.searching = false; changed("search", item.id); } }
    }
    function addCandidate(id, candidate) {
      return edit(id, function (draft) {
        if (!Array.isArray(draft.records)) draft.records = [];
        if (draft.records.some(function (row) { return identity(row.ref) === identity(candidate.ref); })) return;
        draft.records.push({ ref: clone(candidate.ref), record: clone(candidate.record), press_keys: [], confirmed: true });
      }, "record");
    }
    function addRecord(id) {
      return edit(id, function (draft, item) {
        if (!Array.isArray(draft.records)) draft.records = [];
        var record = template(item.name), collection = attribute(record, "collection-type", {}).data;
        collection.path = draft.work_root || ""; collection.collectioned[0].press_path = draft.release_name || "";
        draft.records.push({ ref: null, record: record, press_keys: [], confirmed: false });
      }, "record");
    }
    function applyWorkForm(id, index, patch) {
      return edit(id, function (draft, item) {
        if (index != null && item.jsonErrors[index]) throw new Error("请先修正作品 JSON 格式");
        if (!Array.isArray(draft.records)) draft.records = [];
        var entry = index == null ? { ref: null, record: template(""), press_keys: [], confirmed: false } : draft.records[index];
        if (!entry) throw new Error("作品草稿已变化，请重新打开编辑窗口");
        var previous = pressRows(entry.record), previousPath = attribute(entry.record, "collection-type", {}).data.path;
        var record = window.NimdaWorkRecordForm.applyPatch(entry.record, patch);
        var pressesChanged = JSON.stringify(previous) !== JSON.stringify(pressRows(record));
        entry.record = record;
        if (pressesChanged) { entry.press_keys = []; entry.manual_press_binding = true; }
        if (!entry.ref) entry.confirmed = true;
        if (index == null) draft.records.push(entry);
        var collection = attribute(record, "collection-type", {}).data, selected = selectedPositions(entry);
        if (collection.path !== previousPath) draft.work_root = collection.path;
        if (!entry.ref && selected.length === 1) {
          draft.work_root = collection.path;
          draft.release_name = pressRows(record)[selected[0]].press_path;
        }
        synchronizeTarget(draft); resetValidJsonTexts(item);
      }, "record");
    }
    function addPress(id, index, continuationIndex) {
      return edit(id, function (draft, item) {
        if (item.jsonErrors[index]) throw new Error("请先修正该记录的 JSON 格式");
        var entry = draft.records[index], collection = attribute(entry.record, "collection-type", {}).data;
        var parent = continuationIndex == null ? collection : collection.continuations[continuationIndex];
        if (!Array.isArray(parent.collectioned)) parent.collectioned = [];
        var press = { press_format: "", press_group: "", press_path: draft.release_name || "" };
        parent.collectioned.push(press);
        entry.press_keys = [pressRows(entry.record).indexOf(press) + ":manual"];
        entry.manual_press_binding = true; collection.path = draft.work_root || collection.path || "";
        delete item.jsonTexts[index];
      }, "record");
    }
    function removePress(id, index, pressIndex, continuationIndex) {
      return edit(id, function (draft, item) {
        var entry = draft.records[index], collection = attribute(entry.record, "collection-type", {}).data;
        var parent = continuationIndex == null ? collection : collection.continuations[continuationIndex];
        parent.collectioned.splice(pressIndex, 1); entry.press_keys = []; entry.manual_press_binding = true;
        delete item.jsonTexts[index];
      }, "record");
    }
    function addContinuation(id, index) {
      return edit(id, function (draft, item) {
        var collection = attribute(draft.records[index].record, "collection-type", {}).data;
        if (!Array.isArray(collection.continuations)) collection.continuations = [];
        collection.continuations.push({ title: "", collectioned: [] }); delete item.jsonTexts[index];
      }, "record");
    }
    function updateRecordJson(id, index, value) {
      return edit(id, function (draft, item) {
        item.jsonTexts[index] = value;
        try {
          var record = JSON.parse(value);
          if (!record || typeof record !== "object" || Array.isArray(record) || !Array.isArray(record.attributes)) throw new Error("必须是含 attributes 数组的作品对象");
          validateRecordStructure(record);
          if (JSON.stringify(pressRows(draft.records[index].record)) !== JSON.stringify(pressRows(record))) {
            draft.records[index].press_keys = []; draft.records[index].manual_press_binding = true;
          }
          draft.records[index].record = record; delete item.jsonErrors[index];
        } catch (error) { item.jsonErrors[index] = message(error); }
      }, "json");
    }
    async function execute(ids) {
      if (state.executing || state.scanning) return;
      var chosen = ids.map(child).filter(Boolean);
      if (chosen.length > 64) { notify("单批最多处理 64 个子目录，请缩小勾选范围。", true); return; }
      if (!chosen.length || chosen.some(function (item) { return !item.locked || !item.plan || !item.plan.can_execute || item.executed; })) {
        notify("所选目录必须全部完成重新预览并确认锁定，才可以执行。", true); return;
      }
      stopAutoPreview(); state.executing = true; changed("busy");
      try {
        var data = await request("execute", { plan_ids: chosen.map(function (item) { return item.plan.id; }), confirm: true, generate_shortcuts: state.generateShortcuts }, function (id) { chosen.forEach(function (item) { item.executeOperationId = id; }); });
        list(data.results).forEach(function (result) {
          var item = chosen.find(function (row) { return row.plan && row.plan.id === result.plan_id || row.name === result.child; });
          if (!item) return; item.result = result; item.locked = false;
          // A failed execution may already have changed media or DB. Never reuse its plan.
          item.executed = result.status === "succeeded" || result.status === "warning";
          item.plan = null; item.version += 1;
        });
        chosen.forEach(function (item) { if (item.plan) { item.plan = null; item.locked = false; item.version += 1; } });
        notify("整理处理结束。请查看各目录结果；快捷方式需核对具体预览后单独确认。", list(data.results).some(function (row) { return row.status === "failed"; }));
      } catch (error) {
        chosen.forEach(function (item) { item.plan = null; item.locked = false; item.version += 1; item.error = message(error); });
        notify("执行结果未能确认，请检查处理详情并重新扫描，不能重复使用旧计划：" + message(error), true);
      } finally { state.executing = false; changed("execute"); }
    }
    async function shortcuts(id, confirm) {
      var item = child(id), result = item && item.result; if (!result || state.executing || item.shortcutBusy) return;
      item.shortcutBusy = true; changed("busy");
      try {
        var payload = { refs: result.shortcut_refs || [], preview: !confirm };
        if (confirm) {
          var preview = result.shortcut_preview || {};
          payload.confirm = true; payload.plan_id = preview.plan_id;
          if (!payload.plan_id) throw new Error("请先重新预检快捷方式");
          if (shortcutBlocked(preview)) throw new Error("快捷方式存在冲突，请修正后重新预检；不会覆盖现有文件");
          if (!shortcutCreatable(preview)) throw new Error("当前没有需要新增的快捷方式，请查看已存在或跳过原因");
        }
        var data = await request("shortcuts", payload, function (operationId) { item.shortcutOperationId = operationId; });
        if (confirm) { result.shortcut_result = data; result.shortcut_preview = null; notify(item.name + "：快捷方式处理完成。"); }
        else result.shortcut_preview = data.preview || data.shortcut_preview || data;
      } catch (error) { notify(item.name + "：" + message(error), true); item.error = message(error); }
      finally { item.shortcutBusy = false; changed("shortcuts"); }
    }
    return { state: state, child: child, setRoot: setRoot, choose: choose, scan: scan, edit: edit, preview: preview,
      lock: lock, unlock: unlock, search: search, addCandidate: addCandidate, addRecord: addRecord, applyWorkForm: applyWorkForm,
      addPress: addPress, removePress: removePress, addContinuation: addContinuation, stopAutoPreview: stopAutoPreview,
      updateRecordJson: updateRecordJson, execute: execute, shortcuts: shortcuts,
      setTarget: function (id, key, value) { return edit(id, function (draft, item) { draft[key] = value; synchronizeTarget(draft); resetValidJsonTexts(item); }, "target"); },
      setCompleted: function (id, completed) { return edit(id, function (draft, item) {
        draft.completed = completed;
        if (completed) { draft.work_root = state.scannedRoot; draft.release_name = item.name; synchronizeTarget(draft); resetValidJsonTexts(item); }
      }, "completed"); },
      syncFromRecord: function (id, index, key, value, position) { return edit(id, function (draft, item) {
        var entry = draft.records[index], collection = attribute(entry.record, "collection-type", {}).data;
        if (key === "path") { collection.path = value; draft.work_root = value; synchronizeTarget(draft); }
        else { pressRows(entry.record)[position].press_path = value; if (selectedPositions(entry).indexOf(position) >= 0) { draft.release_name = value; draft.work_root = collection.path; synchronizeTarget(draft); } }
        resetValidJsonTexts(item);
      }, "target"); },
      select: function (id) { state.activeId = id; changed("selection"); },
      setSelected: function (id, value) { var item = child(id); if (item && !state.executing) { item.selected = !!value; changed("selection-check"); } },
      dispose: function () { stopAutoPreview(); state.disposed = true; state.generation += 1; } };
  }

  window.NimdaDirectoryOrganizer = { createController: createController, attribute: attribute, template: template, planOverview: planOverview };
  var controller, featureContext = {}, view, navButton, ui = {}, ownedView = false, ownedTab = false, inputEditing = false, fileDetails = null, enumFieldSerial = 0, workEditorEpoch = 0;
  function element(tag, className, value) {
    var node = document.createElement(tag); if (className) node.className = className;
    if (value != null) node.textContent = text(value); return node;
  }
  function append(parent) { for (var i = 1; i < arguments.length; i++) parent.appendChild(arguments[i]); return parent; }
  function button(label, action, disabled) { var node = element("button", "organizer-button", label); node.type = "button"; node.disabled = !!disabled; node.addEventListener("click", action); return node; }
  function inputField(parent, label, value, onChange, disabled, type) {
    var box = element("label", "organizer-field"), control = element("input"); control.type = type || "text";
    control.value = text(value); control.disabled = !!disabled; control.addEventListener("change", function () { onChange(control.value); });
    control.addEventListener("input", function () { inputEditing = true; try { onChange(control.value); } finally { inputEditing = false; } });
    append(box, element("span", "", label), control); parent.appendChild(box); return control;
  }
  function enumField(parent, key, label, value, onChange, disabled) {
    var enums = window.NimdaEnumFields, mode = enums.fieldMode(key);
    if (mode === "text") return inputField(parent, label, value, onChange, disabled);
    var current = text(value), control, options;
    function changed(value) { if (value !== current) { current = value; onChange(value); } }
    if (mode === "select") {
      var box = element("label", "organizer-field"); control = element("select"); control.disabled = !!disabled;
      control.addEventListener("change", function () { changed(control.value); });
      append(box, element("span", "", label), control); parent.appendChild(box); options = control;
    } else {
      control = inputField(parent, label, value, changed, disabled);
      options = element("datalist"); options.id = "organizer-enum-options-" + (++enumFieldSerial);
      control.setAttribute("list", options.id); parent.appendChild(options);
    }
    control.setAttribute("data-enum-field", key);
    function refresh(initialValue) {
      // A native select discards .value until its matching option exists.
      var saved = arguments.length ? text(initialValue) : control.value, config = featureContext.config || {};
      var choices = enums.choices(config.enum_options, config.enum_labels, key, mode === "select" ? saved : undefined);
      clear(options);
      if (mode === "select") {
        var placeholder = element("option", "", choices.length ? "请选择" : "暂无选项，请先配置枚举");
        placeholder.value = ""; options.appendChild(placeholder);
      }
      choices.forEach(function (choice) {
        var option = element("option", "", choice.label); option.value = choice.value;
        if (mode === "datalist") option.label = choice.label;
        options.appendChild(option);
      });
      control.value = saved;
    }
    refresh(current);
    if (!ui.enumControls) ui.enumControls = [];
    ui.enumControls.push({ control: control, refresh: refresh }); return control;
  }
  function refreshEnumFields(context) {
    featureContext = context || featureContext;
    list(ui.enumControls).forEach(function (entry) { if (entry.control.isConnected !== false) entry.refresh(); });
  }
  function checkbox(parent, label, checked, onChange, disabled) {
    var box = element("label", "organizer-check"), control = element("input"); control.type = "checkbox";
    control.checked = !!checked; control.disabled = !!disabled; control.addEventListener("change", function () { onChange(control.checked); });
    append(box, control, element("span", "", label)); parent.appendChild(box); return control;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function statusLabel(item) {
    if (item.result) return { succeeded: "本次已执行", warning: "已执行 · 有警告", failed: "执行失败", skipped: "尚未执行 · 批次暂停" }[item.result.status] || "已返回处理结果";
    if (item.locked) return "已确认锁定";
    if (!item.plan && item.lastPlan) return "草稿已修改 · 待重新预览";
    var summary = planOverview(item);
    return summary.media === "待预览" ? summary.media : summary.media + " · " + summary.database;
  }
  function section(parent, title, className) {
    var box = element("section", "organizer-panel" + (className ? " " + className : ""));
    box.appendChild(element("h3", "organizer-panel-title", title)); parent.appendChild(box); return box;
  }
  function overviewText(overview) {
    if (overview.historical) return "上次扫描 " + overview.files + " 个文件";
    if (overview.outdated) return overview.files + " 个文件 · " + overview.records + " 条作品记录";
    return overview.files + " 个文件 · 移动 " + overview.moves + " · 原位 " + overview.unchanged + " · 关联 " + overview.records + " 条 DB · 修改 " + overview.changes;
  }
  function details(parent, title, open) {
    var node = element("details", "organizer-section"), ancestor = parent;
    while (ancestor && !ancestor.organizerSectionPath) ancestor = ancestor.parentNode;
    node.organizerSectionPath = (ancestor ? ancestor.organizerSectionPath + " / " : "") + title;
    var item = controller && controller.child(controller.state.activeId), saved = item && item.expanded;
    node.open = saved && Object.prototype.hasOwnProperty.call(saved, node.organizerSectionPath) ? saved[node.organizerSectionPath] : !!open;
    append(node, element("summary", "", title)); parent.appendChild(node); return node;
  }
  function rememberSections() {
    var item = controller && controller.child(ui.renderedChild); if (!item) return;
    function visit(node) {
      if (node.organizerSectionPath) item.expanded[node.organizerSectionPath] = !!node.open;
      Array.prototype.forEach.call(node.children || [], visit);
    }
    visit(ui.detail);
  }
  function jsonView(parent, value) { parent.appendChild(element("pre", "organizer-json-read", JSON.stringify(value, null, 2))); }
  function renderShortcutPreview(parent, preview) {
    var box = element("section", "organizer-shortcut-preview"); parent.appendChild(box);
    box.appendChild(element("h3", "", "快捷方式实际预览"));
    box.appendChild(element("p", "organizer-note", "待创建 " + shortcutCreatable(preview) + " · 已存在 " + (Number(preview.already_exists_count) || 0) + " · 冲突 " + (Number(preview.conflict_count) || list(preview.conflicts).length)));
    list(preview.items).forEach(function (row) {
      var card = element("article", "organizer-shortcut-item"); box.appendChild(card);
      card.appendChild(element("strong", "", text(row.name) + " · " + ({ planned: "将新建", already_exists: "已存在且一致", conflict: "冲突，禁止覆盖", skipped: "跳过", duplicate: "同目标重复" }[row.status] || text(row.status))));
      [["快捷方式文件", row.shortcut_path], ["DB 期望目标", row.target_path], ["已有快捷方式实际目标", row.actual_target]].forEach(function (pair) {
        if (pair[1]) append(card, element("span", "organizer-note", pair[0]), element("code", "organizer-shortcut-path", pair[1]));
      });
      if (row.error) card.appendChild(element("p", "organizer-error", row.error));
    });
    list(preview.issues).concat(list(preview.conflicts)).forEach(function (issue, index, all) {
      var value = text(issue.message || issue.error || issue.reason || issue.code);
      if (all.slice(0, index).some(function (previous) { return text(previous.message || previous.error || previous.reason || previous.code) === value; })) return;
      box.appendChild(element("p", issue.blocking === false ? "organizer-note" : "organizer-error", value + (issue.path || issue.shortcut_path ? " · " + (issue.path || issue.shortcut_path) : "")));
    });
    if (!shortcutCreatable(preview) && !shortcutBlocked(preview)) box.appendChild(element("p", "organizer-note", "本次没有可新增快捷方式；请核对已存在和跳过项。"));
    jsonView(details(box, "完整快捷方式预检 JSON", false), preview);
  }
  function mutateRecord(item, index, apply) {
    if (item.jsonErrors[index]) { if (featureContext.setStatus) featureContext.setStatus("请先修正该作品的完整 JSON 格式，避免覆盖未完成输入。", true); return; }
    var edited = controller.edit(item.id, function (draft, target) { apply(draft.records[index].record); delete target.jsonTexts[index]; }, "record-field");
    if (edited && ui.recordJsonEditors && ui.recordJsonEditors[index]) ui.recordJsonEditors[index].value = JSON.stringify(item.draft.records[index].record, null, 2);
  }

  function openWorkEditor(item, index) {
    if (controller.state.executing || item.locked || item.executed || item.previewing || (index != null && item.jsonErrors[index])) return;
    var draft = item.draft, version = item.version, generation = controller.state.generation, epoch = workEditorEpoch;
    var entry = index == null ? null : draft.records[index];
    var record = entry ? entry.record : template(item.name);
    if (!entry) {
      var collection = attribute(record, "collection-type", {}).data;
      collection.path = draft.work_root || ""; collection.collectioned[0].press_path = draft.release_name || "";
    }
    var config = featureContext.config || {};
    window.NimdaNewWorkDialog.open({
      title: entry ? "编辑作品信息" : "新增 DB 记录", submitLabel: entry ? "应用到待保存记录" : "添加到待保存记录", hideHints: true,
      enumOptions: config.enum_options || {}, enumLabels: config.enum_labels || {},
      initialData: window.NimdaWorkRecordForm.fromRecord(record),
      onSubmit: function (patch) {
        if (!controller || workEditorEpoch !== epoch || controller.state.disposed || controller.state.generation !== generation || controller.child(item.id) !== item ||
            controller.state.activeId !== item.id || item.draft !== draft || item.version !== version || item.previewing ||
            controller.state.executing || item.locked || item.executed || (entry && draft.records[index] !== entry)) {
          throw new Error("目录或预览已变化，请关闭窗口后重新编辑。");
        }
        if (!controller.applyWorkForm(item.id, index, patch)) throw new Error("当前草稿不可编辑，请重新预览。");
      }
    });
  }

  function renderRecord(parent, item, entry, index, disabled) {
    var record = entry.record || {}, section = element("section", "organizer-record-editor");
    parent.appendChild(section);
    var originallyDisabled = disabled; disabled = disabled || !!item.jsonErrors[index];
    if (entry.ref) section.appendChild(element("p", "organizer-note", "来源：" + entry.ref.yaml_source_rel + " · 记录 " + entry.ref.index_in_file));
    var collection = list(record.attributes).find(function (row) { return row.type === "collection-type"; }); collection = collection && collection.data || {};
    var globalPosition = list(collection.collectioned).length;
    function presses(rows, continuationIndex) {
      list(rows).forEach(function (press, pressIndex) {
        var position = globalPosition++;
        var block = element("div", "organizer-press"); section.appendChild(block);
        block.appendChild(element("strong", "", continuationIndex == null ? "压制记录 " + (pressIndex + 1) : "补充收集 " + (continuationIndex + 1) + " · 压制 " + (pressIndex + 1)));
        [["press_format", "压制格式"], ["press_group", "压制 / 字幕组（留空表示无）"], ["press_path", "相对压制目录 press_path"]].forEach(function (pair) {
          enumField(block, pair[0], pair[1], press[pair[0]], function (value) {
            if (pair[0] === "press_path") { controller.syncFromRecord(item.id, index, "press_path", value, position); return; }
            mutateRecord(item, index, function (next) {
            var coll = attribute(next, "collection-type", {}).data;
            var target = continuationIndex == null ? coll.collectioned : coll.continuations[continuationIndex].collectioned;
            target[pressIndex][pair[0]] = value;
          }); }, disabled);
        });
        block.appendChild(button("移除此压制草稿（未保存）", function () { controller.removePress(item.id, index, pressIndex, continuationIndex); }, disabled));
      });
    }
    list(collection.continuations).forEach(function (continuation, ci) {
      inputField(section, "补充收集标题 " + (ci + 1), continuation.title, function (value) { mutateRecord(item, index, function (next) { attribute(next, "collection-type", {}).data.continuations[ci].title = value; }); }, disabled);
      presses(continuation.collectioned, ci);
      section.appendChild(button("为补充收集 " + (ci + 1) + " 新增压制记录", function () { controller.addPress(item.id, index, ci); }, disabled));
    });
    section.appendChild(button("新增补充收集", function () { controller.addContinuation(item.id, index); }, disabled));
    var candidate = item.candidates.find(function (row) { return identity(row.ref) === identity(entry.ref); });
    var available = candidate ? list(candidate.press || candidate.presses) : [];
    var pressOptions = pressRows(record).map(function (press, position) {
      var original = !entry.manual_press_binding && available.find(function (row) { return Number(text(row.press_key || row.key).split(":")[0]) === position; });
      var selectedKey = list(entry.press_keys).find(function (key) { return Number(text(key).split(":")[0]) === position; });
      return Object.assign({}, press, { press_key: selectedKey || original && (original.press_key || original.key) || position + ":manual" });
    });
    if (pressOptions.length) {
      var bindings = element("div", "organizer-bindings"); append(bindings, element("strong", "", "本目录对应的压制记录（可以多选）")); section.appendChild(bindings);
      pressOptions.forEach(function (press) {
        var key = text(press.press_key || press.key); if (!key) return;
        checkbox(bindings, [press.press_format || "待填写格式", press.press_group || "无组", press.press_path].filter(Boolean).join(" · "), selectedPositions(entry).indexOf(Number(key.split(":")[0])) >= 0, function (checked) {
          controller.edit(item.id, function (draft) { var target = draft.records[index]; var keys = list(target.press_keys).filter(function (value) { return value !== key; }); if (checked) keys.push(key); target.press_keys = keys; synchronizeTarget(draft); }, "record");
        }, disabled || (!entry.ref && pressOptions.length === 1));
      });
    }
    if (!entry.ref) checkbox(section, "确认新增作品", entry.confirmed, function (checked) { controller.edit(item.id, function (draft) { draft.records[index].confirmed = checked; }, "record"); }, disabled);
    var raw = details(section, "完整 JSON", false), textarea = element("textarea", "organizer-json-editor");
    textarea.value = Object.prototype.hasOwnProperty.call(item.jsonTexts, index) ? item.jsonTexts[index] : JSON.stringify(record, null, 2); textarea.disabled = originallyDisabled; textarea.spellcheck = false; textarea.rows = 14;
    textarea.setAttribute("aria-label", "完整作品 JSON " + (index + 1));
    ui.recordJsonEditors[index] = textarea;
    var jsonError = element("p", "organizer-error");
    textarea.addEventListener("input", function () { controller.updateRecordJson(item.id, index, textarea.value); jsonError.textContent = item.jsonErrors[index] || ""; });
    textarea.addEventListener("change", function () { if (!item.jsonErrors[index]) render(); });
    raw.appendChild(textarea);
    jsonError.textContent = item.jsonErrors[index] || ""; raw.appendChild(jsonError);
    section.appendChild(button("移除此关联（不会删除 DB）", function () { controller.edit(item.id, function (draft, target) { draft.records.splice(index, 1); target.recordSettingsIndex = null; target.jsonErrors = {}; target.jsonTexts = {}; }, "record"); }, disabled));
  }

  function closeFileDetails(restoreFocus) {
    var current = fileDetails; if (!current) return;
    fileDetails = null;
    if (current.dialog.open && typeof current.dialog.close === "function") current.dialog.close();
    if (current.dialog.parentNode) current.dialog.parentNode.removeChild(current.dialog);
    var focus = current.opener && current.opener.isConnected !== false ? current.opener : ui.fileDetailsButton;
    if (restoreFocus !== false && focus && typeof focus.focus === "function") focus.focus();
  }
  function markFileDetailsStale() {
    if (!fileDetails) return;
    fileDetails.stale = true;
    fileDetails.notice.textContent = "草稿已修改 · 当前详情未重新预览，以下仅供继续编辑，不能作为有效执行计划。请关闭窗口后重新预览。";
    fileDetails.notice.className = "organizer-error";
    fileDetails.dialog.setAttribute("data-preview-stale", "true");
  }
  function renderFileDetails() {
    var current = fileDetails; if (!current) return;
    var item = current.item, disabled = controller.state.executing || item.locked || item.executed;
    clear(current.body);
    var controls = element("div", "organizer-tools"), all = list((item.plan || item.lastPlan || {}).files);
    function mode(showAll) { current.showAll = showAll; current.page = 0; renderFileDetails(); }
    var moving = button("只看移动", function () { mode(false); }); moving.setAttribute("aria-pressed", String(!current.showAll));
    var every = button("全部文件", function () { mode(true); }); every.setAttribute("aria-pressed", String(current.showAll));
    append(controls, moving, every, button(current.editing ? "结束编辑去向" : "编辑文件去向", function () { current.editing = !current.editing; renderFileDetails(); }, disabled));
    current.body.appendChild(controls);
    var rows = all.filter(function (row) { return current.showAll || fileMoved(item, row); });
    var pageSize = 200, pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
    current.page = Math.max(0, Math.min(current.page, pageCount - 1)); var first = current.page * pageSize;
    current.body.appendChild(element("p", "organizer-note", "共 " + rows.length + " 个文件"));
    var roots = element("div", "organizer-detail-roots");
    append(roots, element("code", "organizer-path", "来源：" + current.sourceRoot), element("code", "organizer-path", "目标：" + current.targetRoot)); current.body.appendChild(roots);
    if (!rows.length) current.body.appendChild(element("p", "organizer-empty", current.showAll ? "空目录" : "无需移动文件"));
    rows.slice(first, first + pageSize).forEach(function (row) {
      var changed = fileMoved(item, row), line = element("div", "organizer-file" + (changed ? " is-moving" : " is-unchanged"));
      line.setAttribute("data-file-source", row.source_rel);
      append(line, element("code", "organizer-file-source", row.source_rel), element("span", "organizer-file-arrow", changed ? "→" : "保持原位"));
      if (current.editing) inputField(line, "目标相对路径", fileDestination(item, row), function (value) {
        if (text(fileDestination(item, row)) === value) return;
        controller.edit(item.id, function (draft) {
          draft.completed = false; if (!draft.file_targets) draft.file_targets = {};
          draft.file_targets[row.source_rel] = value;
        }, "file");
      }, disabled);
      else line.appendChild(element("code", "organizer-file-target", fileDestination(item, row)));
      if (row.reason) line.title = text(row.reason); current.body.appendChild(line);
    });
    if (pageCount > 1) {
      var pages = element("div", "organizer-tools organizer-file-pagination");
      append(pages, button("上一页文件", function () { current.page -= 1; renderFileDetails(); }, current.page === 0),
        element("span", "organizer-note", (first + 1) + "–" + Math.min(first + pageSize, rows.length) + " / " + rows.length + "；第 " + (current.page + 1) + " / " + pageCount + " 页"),
        button("下一页文件", function () { current.page += 1; renderFileDetails(); }, current.page >= pageCount - 1)); current.body.appendChild(pages);
    }
  }
  function openFileDetails(item, opener) {
    closeFileDetails(false);
    var dialog = element("dialog", "organizer-details-dialog"), header = element("div", "organizer-dialog-header");
    var heading = element("h2", "", "文件移动详情"); heading.id = "organizer-file-details-title";
    dialog.setAttribute("aria-labelledby", heading.id); dialog.setAttribute("aria-modal", "true");
    var close = button("关闭详情", function () { closeFileDetails(true); });
    var notice = element("p", "organizer-note", item.locked ? "已锁定 · 只读" : "编辑后需重新预览");
    var body = element("div", "organizer-dialog-body"), plan = item.plan || item.lastPlan || {};
    append(header, heading, close); append(dialog, header, notice, body);
    fileDetails = { dialog: dialog, body: body, notice: notice, item: item, version: item.version, plan: item.plan, opener: opener, showAll: false, page: 0, editing: false,
      sourceRoot: plan.source_path || item.path, targetRoot: item.draft.completed ? plan.source_path || item.path : text(item.draft.work_root).replace(/[\\/]+$/, "") + "/" + text(item.draft.release_name) };
    dialog.addEventListener("cancel", function (event) { event.preventDefault(); closeFileDetails(true); });
    dialog.addEventListener("close", function () { if (fileDetails && fileDetails.dialog === dialog) closeFileDetails(true); });
    dialog.addEventListener("keydown", function (event) { if (event.key === "Escape") { event.preventDefault(); closeFileDetails(true); } });
    view.appendChild(dialog); if (!item.plan) markFileDetailsStale(); renderFileDetails();
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    if (typeof close.focus === "function") close.focus();
  }
  function renderFileTree(parent, item) {
    var plan = item.plan || item.lastPlan || {}, sourceRoot = plan.source_path || item.path;
    var targetRoot = item.draft.completed ? sourceRoot : text(item.draft.work_root).replace(/[\\/]+$/, "") + "/" + text(item.draft.release_name);
    var toolbar = element("div", "organizer-tools"); parent.appendChild(toolbar);
    ui.fileDetailsButton = button("查看逐文件移动详情…", function () { openFileDetails(item, ui.fileDetailsButton); }, item.previewing || controller.state.executing);
    toolbar.appendChild(ui.fileDetailsButton);
    var content = element("div", "organizer-structure-content"); parent.appendChild(content); ui.structureContent = content;
    if (!window.NimdaOrganizerStructure || !window.NimdaCommon.DirectoryBrowser) {
      content.appendChild(element("p", "organizer-error", "目录组件加载失败，请刷新重试。")); return;
    }
    if (!item.structurePreview || item.structurePreview.plan !== plan || item.structurePreview.version !== item.version) {
      item.structurePreview = { plan: plan, version: item.version, value: window.NimdaOrganizerStructure.buildPreview({ sourceRoot: sourceRoot, targetRoot: targetRoot,
        files: list(plan.files).map(function (row) { return { source_rel: row.source_rel, target_rel: fileDestination(item, row), changed: fileMoved(item, row) }; }),
        sourceDirectories: plan.source_directories, sourceComplete: plan.source_structure_complete }) };
    }
    if (!item.structureTree) item.structureTree = {};
    var model = item.structurePreview.value, stats = model.stats || {};
    toolbar.appendChild(element("span", "organizer-note", (stats.directoryMoves || 0) + " 个目录移动 · " + (stats.fileMoves || 0) + " 个独立文件移动"));
    var tabs = element("div", "organizer-tools organizer-structure-tabs"), host = element("div", "organizer-directory-browser");
    content.appendChild(tabs); content.appendChild(host);
    var beforeButton, afterButton;
    function showSide(side) {
      item.structureSide = side;
      beforeButton.setAttribute("aria-pressed", String(side === "before"));
      afterButton.setAttribute("aria-pressed", String(side === "after"));
      host.setAttribute("data-structure-side", side);
      destroyStructureBrowser();
      var state = item.structureTree[side] || (item.structureTree[side] = {});
      ui.structureBrowser = window.NimdaCommon.DirectoryBrowser.mount(host, { roots: side === "before" ? [model.before] : model.after,
        state: state, title: side === "before" ? "整理前" : "整理后", pageSize: 100, maxTreeNodes: 400 });
    }
    beforeButton = button("整理前", function () { showSide("before"); });
    afterButton = button("整理后", function () { showSide("after"); });
    append(tabs, beforeButton, afterButton); showSide(item.structureSide || "after");
    list(model.warnings).forEach(function (warning) { content.appendChild(element("p", "organizer-error", typeof warning === "string" ? warning : warning.message)); });
  }
  function recordAttribute(record, kind, fallback) {
    var row = list(record && record.attributes).find(function (value) { return value && value.type === kind; });
    return row ? row.data : fallback;
  }
  function destroyStructureBrowser() {
    if (ui.structureBrowser) ui.structureBrowser.destroy();
    ui.structureBrowser = null;
  }
  function renderDbSummary(parent, item, disabled) {
    var records = list(item.draft.records);
    if (!records.length) parent.appendChild(element("p", "organizer-note", "暂无关联 DB 记录"));
    records.forEach(function (entry, index) {
      var card = element("article", "organizer-db-record"), row = element("div", "organizer-db-summary-row"), record = entry.record || {}, selected = selectedPositions(entry);
      card.setAttribute("data-record-index", String(index)); parent.appendChild(card); card.appendChild(row);
      var date = recordAttribute(record, "date", {}) || {}, presses = pressRows(record).filter(function (_press, index) { return selected.indexOf(index) >= 0; });
      append(row, element("strong", "", recordName(record)), element("span", "organizer-badge", item.executed ? "已保存" : entry.ref ? "已关联" : entry.confirmed ? "待保存" : "待补齐"));
      var subtitle = [isoDate(date.start), isoDate(date.end)].filter(Boolean).join(" ～ ");
      if (subtitle) row.appendChild(element("span", "organizer-note", subtitle));
      row.appendChild(element("span", "organizer-db-press-summary", presses.length ? presses.map(function (press) { return [press.press_format || "格式待填", press.press_group || "无压制组", press.press_path].filter(Boolean).join(" · "); }).join("；") : "压制记录尚未选择"));
      var tools = element("div", "organizer-tools organizer-record-actions"), isOpen = item.recordSettingsIndex === index;
      var settings = button(isOpen ? "收起关联设置" : selected.length ? "关联设置" : "选择压制记录", function () { item.recordSettingsIndex = isOpen ? null : index; render(); });
      settings.setAttribute("aria-expanded", String(isOpen));
      append(tools, button(!entry.ref && !entry.confirmed ? "补齐作品信息" : "编辑作品信息", function () { openWorkEditor(item, index); }, disabled || item.previewing || !!item.jsonErrors[index]), settings); card.appendChild(tools);
      if (item.jsonErrors[index]) card.appendChild(element("p", "organizer-error", item.jsonErrors[index]));
      if (isOpen) renderRecord(card, item, entry, index, disabled);
    });
  }
  function semanticRecord(record) {
    var result = Object.create(null), seen = Object.create(null);
    if (!record) return result;
    Object.keys(record).forEach(function (key) { if (key !== "attributes") result["record." + key] = record[key]; });
    list(record.attributes).forEach(function (row, index) {
      if (!row) return;
      var type = text(row.type || "attribute-" + index); seen[type] = (seen[type] || 0) + 1;
      result[type + (seen[type] > 1 ? ".#" + seen[type] : "")] = row.data;
    });
    return result;
  }
  function changedFields(before, after, limit) {
    var rows = [], truncated = false;
    function walk(left, right, path, depth) {
      if (JSON.stringify(left) === JSON.stringify(right)) return;
      if (rows.length >= limit) { truncated = true; return; }
      if (depth < 8 && left && right && typeof left === "object" && typeof right === "object") {
        var keys = Array.from(new Set(Object.keys(left).concat(Object.keys(right))));
        keys.forEach(function (key) { walk(left[key], right[key], path ? path + "." + key : key, depth + 1); });
      } else rows.push({ path: path, before: left, after: right });
    }
    walk(semanticRecord(before), semanticRecord(after), "", 0); return { rows: rows, truncated: truncated };
  }
  function renderDbChanges(parent, changes) {
    var active = list(changes).filter(function (change) { return change.action !== "unchanged"; });
    if (!active.length) return;
    var box = section(parent, "DB 实际变更", "organizer-db-changes");
    active.forEach(function (change) {
      box.appendChild(element("h4", "", (change.action === "create" ? "新增：" : "修改：") + recordName(change.after || change.before)));
      if (change.action === "create") return;
      var difference = changedFields(change.before, change.after, 12);
      difference.rows.forEach(function (field) {
        var row = element("div", "organizer-db-change-row");
        var label = field.path.replace(/^collection-type\./, "收集.").replace(/press_path/g, "压制目录").replace(/press_format/g, "压制格式").replace(/press_group/g, "压制组");
        function value(raw) { var shown = raw == null ? "未设置" : typeof raw === "object" ? JSON.stringify(raw) : text(raw); if (/^date\./.test(field.path)) shown = isoDate(shown); return shown.length > 240 ? shown.slice(0, 240) + "…" : shown; }
        append(row, element("span", "organizer-note", label), element("code", "", value(field.before)), element("span", "", "→"), element("code", "", value(field.after))); box.appendChild(row);
      });
      if (difference.truncated) box.appendChild(element("p", "organizer-note", "其余字段变化请查看完整差异。"));
    });
    jsonView(details(box, "完整 DB 差异 JSON", false), active);
    return box;
  }
  function targetPath(item) {
    return text(item.draft && item.draft.work_root).replace(/[\\/]+$/, "") + "/" + text(item.draft && item.draft.release_name);
  }
  function issueTab(issue) {
    var code = text(issue.code);
    if (/catalog|record|binding|press-selection|name-required|format-required|date|country|domain|database/.test(code)) return "database";
    if (/work-root|target-root|target-name|release-name|strategy|title-unresolved|format-unresolved/.test(code)) return "target";
    return "preview";
  }
  function selectDetailTab(id, focus) {
    if (ui.detailTabs) ui.detailTabs.select(id, { focus: !!focus });
  }
  function openOperationDetails(item, route) {
    var center = window.NimdaOperationCenter, id = item[route + "OperationId"];
    if (!center || typeof center.open !== "function") {
      if (featureContext.setStatus) featureContext.setStatus("处理详情组件未加载，请刷新后重试。", true);
      return;
    }
    if (id && typeof center.get === "function" && !center.get(id)) {
      if (featureContext.setStatus) featureContext.setStatus("此处理记录已不在当前会话，请在处理详情中加载历史日志。", true);
      return;
    }
    var task = !id && typeof center.latest === "function" ? center.latest(API + (route === "shortcut" ? "shortcuts" : route)) : null;
    center.open(id || task && task.id);
  }
  function currentIssues(item) {
    var issues = item.plan ? list(item.plan.issues).slice() : [];
    Object.keys(item.jsonErrors).forEach(function (index) {
      issues.push({ code: "record-json", message: "作品 " + (Number(index) + 1) + "：" + item.jsonErrors[index], blocking: true });
    });
    return issues;
  }
  function refreshDetailNotices(item) {
    if (!ui.detailNotices || !item.draft) return;
    var issues = currentIssues(item), blockers = issues.filter(function (issue) { return issue.blocking; });
    clear(ui.detailNotices); ui.detailNotices.hidden = !issues.length;
    if (issues.length) {
      ui.detailNotices.appendChild(element("strong", "", "待处理 " + issues.length + " 项" + (blockers.length ? " · 阻断 " + blockers.length + " 项" : "")));
      issues.slice(0, item.showAllIssues ? issues.length : 3).forEach(function (issue) {
        var row = element("div", "organizer-issue-row"), tab = issueTab(issue);
        append(row, element("span", issue.blocking ? "organizer-error" : "organizer-note", "[" + issue.code + "] " + issue.message + (issue.path ? " · " + issue.path : "")),
          button("查看" + ({ preview: "目录预览", target: "整理目标", database: "关联 DB" }[tab]), function () { selectDetailTab(tab, true); }));
        ui.detailNotices.appendChild(row);
      });
      if (issues.length > 3) ui.detailNotices.appendChild(button(item.showAllIssues ? "收起其他事项" : "显示其余 " + (issues.length - 3) + " 项", function () { item.showAllIssues = !item.showAllIssues; refreshDetailNotices(item); }));
    }
    if (ui.targetSummary) ui.targetSummary.textContent = "目标：" + targetPath(item);
    if (ui.detailTabs) {
      var records = list(item.draft.records), missing = records.filter(function (entry) { return !entry.confirmed || !selectedPositions(entry).length; }).length;
      var dbIssues = issues.filter(function (issue) { return issueTab(issue) === "database"; }).length;
      var targetIssues = issues.filter(function (issue) { return issueTab(issue) === "target"; }).length;
      ui.detailTabs.setBadge("database", dbIssues ? "待处理 " + dbIssues : !records.length ? "未关联" : missing ? "待补齐 " + missing : records.length + " 条");
      ui.detailTabs.setBadge("target", targetIssues ? "待处理 " + targetIssues : "");
      ui.detailTabs.setBadge("preview", !item.plan && !item.result ? "待重新预览" : "");
    }
  }
  function renderTargetPanel(target, item, disabled) {
    checkbox(target, "媒体目录已整理完成", item.draft.completed, function (checked) { controller.setCompleted(item.id, checked); }, disabled);
    var rules = element("div", "organizer-fields"), select = element("select"), box = element("label", "organizer-field");
    var strategies = [{ id: "auto", label: "自动" }].concat(controller.state.strategies);
    var seen = Object.create(null); strategies.forEach(function (strategy) { if (seen[strategy.id]) return; seen[strategy.id] = true; var option = element("option", "", strategy.label || strategy.id); option.value = strategy.id; select.appendChild(option); });
    select.value = item.strategy; select.disabled = disabled; select.setAttribute("aria-label", "整理规则");
    select.addEventListener("change", function () { controller.edit(item.id, function (_draft, current) { current.strategy = select.value; }, "strategy"); });
    append(box, element("span", "", "整理规则"), select); rules.appendChild(box); target.appendChild(rules);
    inputField(rules, "目标作品根目录", item.draft.work_root, function (value) { controller.setTarget(item.id, "work_root", value); }, disabled);
    inputField(rules, "目标压制文件夹名称", item.draft.release_name, function (value) { controller.setTarget(item.id, "release_name", value); }, disabled);
  }
  function renderDatabasePanel(db, item, disabled) {
    var dbTools = element("div", "organizer-tools"); db.appendChild(dbTools);
    var addDb = button("新增 DB 记录", function () { openWorkEditor(item, null); }, disabled || item.previewing); addDb.className += " organizer-primary-action";
    var findDb = button(item.searchDb ? "收起已有 DB 查找" : "关联已有 DB", function () { item.searchDb = !item.searchDb; render(); }, disabled || item.previewing);
    findDb.setAttribute("aria-expanded", String(!!item.searchDb)); append(dbTools, addDb, findDb);
    if (item.searchDb) {
      var searchTools = element("div", "organizer-tools"), query = element("input");
      query.type = "search"; query.value = item.query; query.placeholder = "搜索已有 DB 作品名或目录"; query.setAttribute("aria-label", "查找已有 DB 记录"); query.disabled = disabled;
      query.addEventListener("input", function () { item.query = query.value; });
      append(searchTools, query, button(item.searching ? "查找中…" : "查找 DB", function () { controller.search(item.id, query.value); }, disabled || item.searching)); db.appendChild(searchTools);
      if (item.searchSummary) db.appendChild(element("p", "organizer-note", item.searchSummary));
      item.candidates.forEach(function (candidate) {
        var row = element("div", "organizer-candidate"); append(row, element("span", "", (candidate.name || recordName(candidate.record)) + " · " + text(candidate.path)), button("关联此 DB 记录", function () { controller.addCandidate(item.id, candidate); }, disabled || list(item.draft.records).some(function (entry) { return identity(entry.ref) === identity(candidate.ref); }))); db.appendChild(row);
      });
    }
    renderDbSummary(db, item, disabled);
    if (item.plan) {
      var dbChanges = renderDbChanges(db, item.plan.db_changes); if (dbChanges) ui.validPreviewSections.push(dbChanges);
      if (item.plan.shortcut_preview && item.plan.shortcut_preview.message) {
        var note = element("p", "organizer-note", "快捷方式：" + item.plan.shortcut_preview.message); db.appendChild(note); ui.validPreviewSections.push(note);
      }
    }
    if (item.result && list(item.result.shortcut_refs).length) {
      var shortcuts = section(db, "本目录快捷方式");
      shortcuts.appendChild(button("重新预检本目录快捷方式", function () { controller.shortcuts(item.id, false); }, item.shortcutBusy));
      var shortcut = item.result.shortcut_preview;
      if (shortcut) {
        renderShortcutPreview(shortcuts, shortcut);
        shortcuts.appendChild(button("确认生成本目录快捷方式", function () { controller.shortcuts(item.id, true); }, item.shortcutBusy || !shortcut.plan_id || shortcutBlocked(shortcut) || !shortcutCreatable(shortcut)));
      }
      if (item.result.shortcut_result) {
        shortcuts.appendChild(element("p", "organizer-note", item.result.shortcut_result.message || "快捷方式处理已返回，具体结果请查看处理详情。"));
        shortcuts.appendChild(button("查看快捷方式处理详情", function () { openOperationDetails(item, "shortcut"); }));
      }
    }
  }
  function destroyDetailTabs() {
    if (ui.detailTabs) ui.detailTabs.destroy();
    ui.detailTabs = null;
  }
  function renderDetail(item) {
    rememberSections(); ui.renderedChild = item ? item.id : "";
    destroyDetailTabs(); destroyStructureBrowser();
    ui.enumControls = []; ui.recordJsonEditors = {}; ui.validPreviewSections = [];
    ui.detailNotices = ui.targetSummary = ui.fileHeading = ui.structureContent = null;
    clear(ui.detail); if (!item) { ui.detail.appendChild(element("p", "organizer-note", "请选择目录")); return; }
    var disabled = controller.state.executing || item.locked || item.executed;
    var header = element("section", "organizer-detail-header"); ui.detail.appendChild(header);
    append(header, element("h2", "", item.name), element("p", "organizer-path", "来源：" + item.path));
    var toolbar = element("div", "organizer-tools"); header.appendChild(toolbar);
    toolbar.setAttribute("aria-label", "当前子目录操作");
    ui.previewOne = button(item.previewing ? "正在预览…" : "重新预览此目录", function () { controller.preview(item.id); }, disabled || item.previewing || Object.keys(item.jsonErrors).length);
    ui.lockOne = button(item.locked ? "已锁定 · 解锁修改" : "确认并锁定此预览", function () { if (item.locked) controller.unlock(item.id); else controller.lock(item.id); }, controller.state.executing || item.executed || (!item.locked && (!item.plan || !item.plan.can_execute || item.previewing)));
    ui.executeOne = button("执行此目录", function () { controller.execute([item.id]); }, !item.locked || controller.state.executing);
    append(toolbar, ui.previewOne, ui.lockOne, ui.executeOne);
    if (item.error) header.appendChild(element("p", "organizer-error", item.error));
    if (!item.draft) return;
    var overview = planOverview(item), summary = element("section", "organizer-overview");
    ui.mediaStatus = element("strong", "organizer-media-status", overview.media); ui.databaseStatus = element("strong", "organizer-db-status", overview.database);
    ui.overviewNote = element("span", "organizer-note", overviewText(overview));
    append(summary, ui.mediaStatus, ui.databaseStatus, ui.overviewNote); header.appendChild(summary);
    ui.targetSummary = element("code", "organizer-target-path"); header.appendChild(ui.targetSummary);
    ui.detailNotices = element("section", "organizer-problems"); header.appendChild(ui.detailNotices);
    if (item.result) {
      var result = element("section", "organizer-result-summary");
      append(result, element("strong", "", "本目录处理结果：" + ({ succeeded: "成功", warning: "完成，有警告", failed: "失败", skipped: "未执行" }[item.result.status] || item.result.status)),
        element("p", "", item.result.message || "请查看详细处理结果。"));
      var resultTools = element("div", "organizer-tools");
      resultTools.appendChild(button("查看处理详情", function () { openOperationDetails(item, "execute"); }));
      if (list(item.result.shortcut_refs).length) resultTools.appendChild(button("查看快捷方式", function () { selectDetailTab("database", true); }));
      result.appendChild(resultTools); header.appendChild(result);
    }
    var tabHost = element("div"), panels = element("div", "organizer-tab-panels");
    append(ui.detail, tabHost, panels);
    var files = element("section", "organizer-tab-panel"), target = element("section", "organizer-tab-panel"), db = element("section", "organizer-tab-panel");
    ui.fileHeading = element("h3", "organizer-panel-title", overview.outdated ? "目录结构预览（待重新预览）" : "目录结构预览");
    files.appendChild(ui.fileHeading);
    append(panels, files, target, db);
    if (!item.detailTabs) item.detailTabs = { activeId: "preview" };
    ui.detailTabs = window.NimdaCommon.SectionTabs.mount(tabHost, {
      label: "当前目录功能", state: item.detailTabs, defaultId: "preview",
      items: [{ id: "preview", label: "目录预览", panel: files }, { id: "target", label: "整理目标", panel: target }, { id: "database", label: "关联 DB", panel: db }],
      onChange: function (id) {
        closeFileDetails(false);
        // Hidden panels share the draft, but their derived text must be refreshed
        // after edits elsewhere without remounting the directory browser.
        if (id === "database" && ui.databasePanelVersion !== item.version) {
          rememberSections(); clear(db); ui.recordJsonEditors = {}; ui.enumControls = [];
          ui.validPreviewSections = ui.validPreviewSections.filter(function (node) { return !!node.parentNode; });
          renderDatabasePanel(db, item, disabled); ui.databasePanelVersion = item.version;
        } else if (id === "target" && ui.targetPanelVersion !== item.version) {
          clear(target); renderTargetPanel(target, item, disabled); ui.targetPanelVersion = item.version;
        }
      }
    });
    renderFileTree(files, item); renderTargetPanel(target, item, disabled); renderDatabasePanel(db, item, disabled);
    ui.targetPanelVersion = ui.databasePanelVersion = item.version;
    refreshDetailNotices(item);
  }
  function render(reason, changedId) {
    if (!controller || !view) return;
    var state = controller.state;
    if (fileDetails && reason === "file" && fileDetails.item.id === state.activeId) {
      fileDetails.version = fileDetails.item.version; fileDetails.plan = fileDetails.item.plan; markFileDetailsStale();
    } else if (fileDetails && (fileDetails.item.id !== state.activeId || controller.child(fileDetails.item.id) !== fileDetails.item ||
      state.executing || state.scanning || fileDetails.version !== fileDetails.item.version || fileDetails.plan !== fileDetails.item.plan ||
      (reason && reason !== "auto-preview" && reason !== "selection-check" && reason !== "scan" && (!changedId || changedId === fileDetails.item.id)))) closeFileDetails(false);
    if (reason === "json" || inputEditing) {
      if (ui.lockOne) ui.lockOne.disabled = true;
      if (ui.executeOne) ui.executeOne.disabled = true;
      var activeItem = controller.child(state.activeId);
      if (ui.previewOne && activeItem) ui.previewOne.disabled = state.executing || activeItem.previewing || activeItem.executed || Object.keys(activeItem.jsonErrors).length > 0;
      if (activeItem && activeItem.lastPlan) {
        var overview = planOverview(activeItem);
        if (ui.mediaStatus) ui.mediaStatus.textContent = overview.media;
        if (ui.databaseStatus) ui.databaseStatus.textContent = overview.database;
        if (ui.overviewNote) ui.overviewNote.textContent = overviewText(overview);
        if (ui.fileHeading) ui.fileHeading.textContent = "目录结构预览（待重新预览）";
        if (ui.structureContent) ui.structureContent.hidden = true;
        if (ui.childStates && ui.childStates[activeItem.id]) ui.childStates[activeItem.id].textContent = statusLabel(activeItem);
        list(ui.validPreviewSections).forEach(function (node) { node.hidden = true; });
      }
      if (activeItem) refreshDetailNotices(activeItem);
      return;
    }
    ui.root.value = state.root; ui.root.disabled = state.executing;
    ui.choose.disabled = state.executing || state.choosing; ui.scan.disabled = state.executing || state.scanning || !state.root.trim();
    ui.scan.textContent = state.scanning ? "扫描中…" : "预览目录";
    ui.execute.disabled = state.executing || !state.children.some(function (row) { return row.selected; });
    ui.previewSelected.disabled = state.executing || !state.children.some(function (row) { return row.selected && !row.locked && !row.executed; });
    ui.shortcutChoice.disabled = state.executing;
    ui.autoPreviewStatus.textContent = state.autoPreview.total ?
      (state.autoPreview.running ? "正在自动预览 " : state.autoPreview.cancelled ? "已停止后续自动预览（当前只读请求可完成） " : "自动预览完成 ") + state.autoPreview.completed + " / " + state.autoPreview.total + "；共 " + state.children.length + " 个子目录。" : "";
    ui.stopAutoPreview.hidden = !state.autoPreview.running;
    clear(ui.summary); if (state.error) ui.summary.appendChild(element("p", "organizer-error", state.error));
    if (state.rootFiles.length) jsonView(details(ui.summary, "所选目录根层有 " + state.rootFiles.length + " 个散落文件（本次不整理）", false), state.rootFiles);
    if (state.children.length > 64) ui.summary.appendChild(element("p", "organizer-note", "本次仅自动预览前 64 个目录；其余可单独预览。建议分批选择不超过 64 项；继续预览可能使上一批缓存失效，需重新预览确认。"));
    state.issues.forEach(function (issue) { ui.summary.appendChild(element("p", "organizer-note", issue.message || text(issue))); });
    clear(ui.list); ui.childStates = Object.create(null);
    state.children.forEach(function (item) {
      var row = element("div", "organizer-child" + (item.id === state.activeId ? " is-active" : ""));
      checkbox(row, "", item.selected, function (checked) { controller.setSelected(item.id, checked); }, state.executing || item.executed).setAttribute("aria-label", "选择 " + item.name);
      var choose = button(item.name, function () { controller.select(item.id); }); choose.className += " organizer-child-name";
      ui.childStates[item.id] = element("span", "organizer-child-state", statusLabel(item));
      append(row, choose, ui.childStates[item.id]); ui.list.appendChild(row);
    });
    if (reason !== "auto-preview" && (!changedId || changedId === state.activeId)) renderDetail(controller.child(state.activeId));
  }
  function mount(nextContext) {
    featureContext = nextContext || {};
    if (controller) return;
    var nav = document.querySelector(".app-tabs"), shell = document.querySelector(".nimda-app-shell") || document.body;
    navButton = document.getElementById("tab-directory-organizer");
    if (!navButton && nav) { navButton = element("button", "app-tab", "目录整理"); navButton.id = "tab-directory-organizer"; navButton.type = "button"; navButton.setAttribute("data-tab", "directory-organizer"); nav.appendChild(navButton); ownedTab = true; }
    view = document.getElementById("directory-organizer-view");
    if (!view) { view = element("main", "directory-organizer-view"); view.id = "directory-organizer-view"; view.hidden = true; shell.appendChild(view); ownedView = true; }
    clear(view);
    controller = createController({ request: function (url, options) { return (featureContext.fetchJson || window.NimdaCommon.fetchJson)(url, options); },
      setStatus: function (value, error) { if (featureContext.setStatus) featureContext.setStatus(value, error); }, onChange: function (_state, reason, changedId) { render(reason, changedId); } });
    var header = element("div", "organizer-header"), tools = element("div", "organizer-tools"), layout = element("div", "organizer-layout");
    append(header, element("h1", "", "目录整理"));
    ui.root = element("input", "organizer-root"); ui.root.placeholder = "输入待整理目录路径"; ui.root.setAttribute("aria-label", "待整理目录路径");
    ui.root.addEventListener("input", function () { controller.setRoot(ui.root.value); });
    ui.choose = button("选择实际文件夹…", function () { controller.choose(); }); ui.scan = button("预览目录", function () { controller.scan(); });
    append(tools, ui.root, ui.choose, ui.scan); header.appendChild(tools);
    var batch = element("div", "organizer-tools");
    ui.previewSelected = button("依次预览勾选目录", async function () { var rows = controller.state.children.filter(function (item) { return item.selected && !item.locked && !item.executed; }); for (var i = 0; i < rows.length; i++) await controller.preview(rows[i].id); });
    ui.execute = button("执行已勾选并锁定的目录", function () { controller.execute(controller.state.children.filter(function (item) { return item.selected; }).map(function (item) { return item.id; })); });
    append(batch, ui.previewSelected, ui.execute);
    ui.shortcutChoice = checkbox(batch, "整理后检查快捷方式（预览后另行确认生成）", false, function (checked) { controller.state.generateShortcuts = checked; });
    ui.autoPreviewStatus = element("p", "organizer-note"); ui.autoPreviewStatus.setAttribute("aria-live", "polite");
    ui.stopAutoPreview = button("停止后续自动预览", function () { controller.stopAutoPreview(); }); ui.stopAutoPreview.hidden = true;
    ui.summary = element("div"); ui.list = element("aside", "organizer-children"); ui.list.setAttribute("aria-label", "直属子目录"); ui.detail = element("section", "organizer-detail");
    append(layout, ui.list, ui.detail); append(view, header, batch, ui.autoPreviewStatus, ui.stopAutoPreview, ui.summary, layout); render();
  }
  window.NimdaCommon.ensureFeatureRegistry(window).register({ id: "directory-organizer", label: "目录整理", tabId: "tab-directory-organizer", viewId: "directory-organizer-view", order: 30,
    init: mount, activate: refreshEnumFields,
    deactivate: function () { workEditorEpoch += 1; closeFileDetails(false); if (controller) controller.stopAutoPreview(); },
    refreshAfterConfig: refreshEnumFields,
    dispose: function () { workEditorEpoch += 1; closeFileDetails(false); destroyDetailTabs(); destroyStructureBrowser(); if (controller) controller.dispose(); controller = null; if (ownedView && view && view.parentNode) view.parentNode.removeChild(view); else if (view) clear(view); if (ownedTab && navButton && navButton.parentNode) navButton.parentNode.removeChild(navButton); view = null; ui = {}; ownedView = ownedTab = false; } });
})();
