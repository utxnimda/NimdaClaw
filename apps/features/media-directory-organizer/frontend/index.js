(function () {
  "use strict";

  function ensureFeatureRegistry() {
    var registry = window.JpTvBrowseFeatureRegistry || {};
    if (!Array.isArray(registry.features)) registry.features = [];
    registry.register = function (feature) {
      if (!feature || !feature.id) return;
      var features = Array.isArray(this.features) ? this.features : (this.features = []);
      for (var i = 0; i < features.length; i++) {
        if (features[i] && features[i].id === feature.id) {
          if (features[i] !== feature && typeof features[i].dispose === "function") {
            try {
              features[i].dispose();
            } catch (_e) {}
          }
          features[i] = feature;
          return;
        }
      }
      features.push(feature);
    };
    window.JpTvBrowseFeatureRegistry = registry;
    return registry;
  }

  var registry = ensureFeatureRegistry();
  var featureCtx = null;
  var cleanupCallbacks = [];
  var featureActive = false;
  var started = false;
  var requestSerial = 0;
  var resourcePickerSerial = 0;
  var recognitionSerial = 0;
  var movePlanViews = new WeakMap();
  var renderedMoveViews = Object.create(null);
  var moveViewSerial = 0;
  var MOVE_FILE_PAGE_SIZE = 200;

  function createResourcePickerState(open) {
    return {
      open: !!open,
      loading: false,
      searching: false,
      error: "",
      cache: null,
      current: null,
      history: [],
      searchDraft: "",
      searchQuery: "",
      searchResults: null,
    };
  }

  function createRecognitionState(query) {
    return {
      query: String(query || ""),
      loading: false,
      error: "",
      notice: "",
      results: null,
      requestSnapshot: "",
      inputFingerprint: "",
    };
  }

  function createSharedTargetState() {
    return {
      confirmed: false,
      selectedMembers: Object.create(null),
      pressPaths: Object.create(null),
      sourceOwners: Object.create(null),
    };
  }

  var MULTIPLE_WORKS_VALUE = "__organizer_multiple_works__";

  var state = {
    config: null,
    root: "",
    plan: null,
    routeOverrides: Object.create(null),
    fileWorkOverrides: Object.create(null),
    sourceWorkOverrides: Object.create(null),
    sourceWorkBindingEdits: Object.create(null),
    sourceWorkDrafts: Object.create(null),
    sourceWorkEditorSource: "",
    sourceWorkEditorMode: "",
    landingSourceWorkBindings: null,
    multipleWorkSources: Object.create(null),
    sourcePressOverrides: Object.create(null),
    dirty: false,
    busy: "",
    notice: "",
    noticeError: false,
    execution: null,
    recovery: null,
    landing: null,
    draftWork: null,
    draftWorks: [],
    sharedTarget: createSharedTargetState(),
    shortcutPending: null,
    resourcePicker: createResourcePickerState(false),
    recognition: createRecognitionState(""),
  };

  function ctx() {
    return featureCtx || {};
  }

  function view() {
    return document.getElementById("media-directory-organizer-view");
  }

  function esc(value) {
    if (ctx().esc) return ctx().esc(value == null ? "" : value);
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function addCleanup(callback) {
    cleanupCallbacks.push(callback);
  }

  function disposeFeature() {
    requestSerial += 1;
    resourcePickerSerial += 1;
    recognitionSerial += 1;
    while (cleanupCallbacks.length) {
      try {
        cleanupCallbacks.pop()();
      } catch (_e) {}
    }
    featureCtx = null;
    featureActive = false;
    started = false;
    movePlanViews = new WeakMap();
    renderedMoveViews = Object.create(null);
  }

  function setSharedStatus(message, isError) {
    if (ctx().setStatus) ctx().setStatus(message || "", !!isError);
  }

  function firstString() {
    for (var i = 0; i < arguments.length; i++) {
      var value = arguments[i];
      if (value != null && String(value).trim()) return String(value).trim();
    }
    return "";
  }

  function firstObject() {
    for (var i = 0; i < arguments.length; i++) {
      var value = arguments[i];
      if (value && typeof value === "object" && !Array.isArray(value)) return value;
    }
    return {};
  }

  function shortcutPlanDetails(payload) {
    payload = payload && typeof payload === "object" ? payload : {};
    var nested = firstObject(payload.shortcut_plan);
    var rows = Array.isArray(payload.shortcuts)
      ? payload.shortcuts
      : Array.isArray(nested.shortcuts)
      ? nested.shortcuts
      : Array.isArray(nested.items)
      ? nested.items
      : [];
    var summary = firstObject(payload.shortcut_summary, nested.shortcut_summary, nested.summary);
    var scope = firstObject(payload.shortcut_scope, nested.shortcut_scope, nested.scope);
    return {
      rows: rows,
      summary: summary,
      planId: firstString(
        payload.shortcut_plan_id,
        nested.shortcut_plan_id,
        nested.plan_id,
      ),
      scope: scope,
      present: !!(
        Object.prototype.hasOwnProperty.call(payload, "shortcuts") ||
        Object.prototype.hasOwnProperty.call(payload, "shortcut_summary") ||
        Object.prototype.hasOwnProperty.call(payload, "shortcut_plan_id") ||
        payload.shortcut_plan
      ),
    };
  }

  function shortcutScopeValues(payload) {
    payload = payload && typeof payload === "object" ? payload : {};
    var details = shortcutPlanDetails(payload);
    var scope = details.scope;
    return {
      root: firstString(payload.root, scope.root),
      workRefs: Array.isArray(payload.work_refs)
        ? payload.work_refs
        : Array.isArray(scope.work_refs)
        ? scope.work_refs
        : null,
    };
  }

  function nonEmptyObject(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return Object.keys(value).length ? value : null;
  }

  function shortcutIssues(payload) {
    payload = payload && typeof payload === "object" ? payload : {};
    var nested = firstObject(payload.shortcut_plan);
    return Array.isArray(payload.issues)
      ? payload.issues
      : Array.isArray(nested.issues)
      ? nested.issues
      : [];
  }

  function isAlreadyOrganizedPlan(plan) {
    return !!(plan && plan.media_state === "already_organized");
  }

  function alreadyOrganizedShortcutState(plan) {
    if (!isAlreadyOrganizedPlan(plan)) return "";
    var value = firstString(plan.shortcut_state).toLowerCase();
    return ["complete", "pending", "blocked"].indexOf(value) >= 0
      ? value
      : "blocked";
  }

  function visiblePlanIssues(plan) {
    var issues = plan && Array.isArray(plan.issues) ? plan.issues : [];
    if (!isAlreadyOrganizedPlan(plan)) return issues;
    return issues.filter(function (issue) {
      return String((issue && issue.code) || "") !== "shortcut-scope-empty";
    });
  }

  function shortcutRepairDetails(payload) {
    payload = payload && typeof payload === "object" ? payload : {};
    var nested = firstObject(payload.shortcut_plan);
    var repair = firstObject(payload.repair, nested.repair);
    var registration = firstObject(repair.registration, payload.registration);
    var rawCandidates = Array.isArray(repair.candidates)
      ? repair.candidates
      : Array.isArray(payload.repair_candidates)
      ? payload.repair_candidates
      : [];
    var candidates = rawCandidates
      .filter(function (item) {
        return item && typeof item === "object" && !Array.isArray(item);
      })
      .map(function (item) {
        return {
          draftWork: nonEmptyObject(item.draft_work) || nonEmptyObject(item.draft),
          catalogRef: nonEmptyObject(item.catalog_ref),
          label: firstString(item.label, item.work_name),
          sharedTargetMembers: Array.isArray(item.shared_target_members)
            ? item.shared_target_members
            : [],
          raw: item,
        };
      })
      .filter(function (item) {
        return !!item.draftWork;
      });
    var directDraft =
      nonEmptyObject(repair.draft_work) ||
      nonEmptyObject(registration.draft) ||
      nonEmptyObject(payload.draft_work);
    if (!candidates.length && directDraft) {
      candidates.push({
        draftWork: directDraft,
        catalogRef:
          nonEmptyObject(repair.catalog_ref) || nonEmptyObject(payload.catalog_ref),
        label: firstString(repair.label, directDraft.name),
      });
    }
    return {
      required: !!(
        payload.repair_required === true ||
        repair.required === true ||
        repair.repair_required === true ||
        candidates.length
      ),
      previewEndpoint: firstString(
        repair.preview_endpoint,
        payload.repair_preview_endpoint,
        "/api/media-directory-organizer/landing/repair/preview",
      ),
      applyEndpoint: firstString(
        repair.apply_endpoint,
        payload.repair_apply_endpoint,
        "/api/media-directory-organizer/landing/repair/apply",
      ),
      planId: firstString(payload.repair_plan_id, repair.repair_plan_id, repair.plan_id),
      candidates: candidates,
      directDraft: directDraft,
      independentAppendAllowed: !!(
        payload.independent_append_allowed === true ||
        repair.independent_append_allowed === true
      ),
      reasonCodes: Array.isArray(repair.reason_codes) ? repair.reason_codes : [],
    };
  }

  function issueCodeNeedsCatalogRepair(code) {
    return [
      "catalog-work-not-found",
      "shortcut-catalog-path-missing",
      "shortcut-catalog-path-invalid",
      "shortcut-catalog-path-mismatch",
      "shortcut-catalog-press-invalid",
      "shortcut-catalog-press-empty",
      "shortcut-catalog-press-not-found",
      "shortcut-press-path-missing",
      "shortcut-database-target-mismatch",
      "repair-catalog-selection-required",
      "repair-target-missing",
    ].indexOf(String(code || "")) >= 0;
  }

  function issuesNeedCatalogRepair(issues) {
    return (Array.isArray(issues) ? issues : []).some(function (issue) {
      return issueCodeNeedsCatalogRepair(issue && issue.code);
    });
  }

  function firstIssueMessage(issues) {
    var rows = Array.isArray(issues) ? issues : [];
    for (var i = 0; i < rows.length; i++) {
      var message = firstString(rows[i] && rows[i].message, rows[i] && rows[i].error);
      if (message) return message;
    }
    return "";
  }

  function configDefaultRoot(config) {
    var paths = config && config.paths && typeof config.paths === "object" ? config.paths : {};
    return firstString(
      paths.default_work_root,
      paths.work_root,
      config && config.default_work_root,
      config && config.root,
    );
  }

  function formatBytes(raw) {
    var value = Number(raw) || 0;
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    return (index === 0 ? String(Math.round(value)) : value.toFixed(2)) + " " + units[index];
  }

  function fetchJson(url, options) {
    if (ctx().fetchJson) return ctx().fetchJson(url, options || {});
    return fetch(url, options || {}).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          return { res: response, data: data };
        });
    });
  }

  function responseError(out, fallback) {
    var data = out && out.data;
    if (data && data.error) return String(data.error);
    var status = out && out.res ? out.res.status : 0;
    return fallback + (status ? "（HTTP " + status + "）" : "");
  }

  function allowedRoots(config) {
    var paths = config && config.paths && typeof config.paths === "object" ? config.paths : {};
    return Array.isArray(paths.allowed_resource_roots) ? paths.allowed_resource_roots : [];
  }

  function pathAllowedByOrganizer(path) {
    var normalized = pathKey(path);
    var roots = allowedRoots(state.config);
    if (!normalized || !roots.length) return !!normalized;
    return roots.some(function (root) {
      var normalizedRoot = pathKey(root);
      return !!normalizedRoot &&
        (normalized === normalizedRoot || normalized.indexOf(normalizedRoot + "\\") === 0);
    });
  }

  function resourcePickerTree(payload) {
    return payload && payload.tree && typeof payload.tree === "object"
      ? payload.tree
      : { type: "folder", name: "资源库", relpath: "", path: "", children: [], children_loaded: true };
  }

  function resourceFolderChildren(node) {
    return Array.isArray(node && node.children)
      ? node.children.filter(function (child) {
          return child && child.type === "folder";
        })
      : [];
  }

  function matchedResourceFolders(node, out) {
    out = out || [];
    if (!node || typeof node !== "object") return out;
    if (node.type === "folder" && node._resource_search_match && firstString(node.path)) {
      out.push(node);
    }
    resourceFolderChildren(node).forEach(function (child) {
      matchedResourceFolders(child, out);
    });
    return out;
  }

  function resourceFolderMeta(node) {
    var folderCount = Number(node && node.child_count);
    var fileCount = Number(node && node.file_count);
    var parts = [];
    if (Number.isFinite(folderCount)) parts.push(folderCount + " 个直接子目录");
    if (Number.isFinite(fileCount)) parts.push(fileCount + " 个文件");
    if (Number(node && node.size) > 0) parts.push(formatBytes(node.size));
    return parts.join(" · ") || "文件夹";
  }

  function renderResourcePickerFolder(folder, isSearchResult) {
    var path = firstString(folder && folder.path);
    var relpath = firstString(folder && folder.relpath);
    var allowed = pathAllowedByOrganizer(path);
    var unavailable = folder && folder.exists === false;
    var selectDisabled = !path || !allowed || unavailable;
    var note = firstString(folder && folder.error);
    if (!allowed && path) note = "不在当前整理器允许范围内";
    if (unavailable) note = "目录当前不可用";
    return (
      '<li class="organizer-resource-picker-row' +
      (selectDisabled ? " is-unavailable" : "") +
      '">' +
      '<div class="organizer-resource-picker-folder-main">' +
      '<span class="organizer-resource-picker-folder-icon" aria-hidden="true">📁</span>' +
      '<span><strong>' +
      esc(firstString(folder && folder.name, path, "未命名目录")) +
      '</strong><code title="' +
      esc(path) +
      '">' +
      esc(path || relpath) +
      '</code><small>' +
      esc(note || resourceFolderMeta(folder)) +
      "</small></span></div>" +
      '<div class="organizer-resource-picker-row-actions">' +
      '<button type="button" class="btn secondary sm" data-organizer-action="resource-picker-open-folder" data-resource-relpath="' +
      esc(relpath) +
      '"' +
      (state.resourcePicker.loading || state.resourcePicker.searching ? " disabled" : "") +
      ">" +
      (isSearchResult ? "打开位置" : "进入") +
      "</button>" +
      '<button type="button" class="btn sm" data-organizer-action="resource-picker-select" data-resource-path="' +
      esc(path) +
      '"' +
      (selectDisabled || state.resourcePicker.loading || state.resourcePicker.searching ? " disabled" : "") +
      ">选择</button>" +
      "</div></li>"
    );
  }

  function renderResourcePicker() {
    var picker = state.resourcePicker;
    if (!picker || !picker.open) return "";
    var current = picker.current || resourcePickerTree(picker.cache);
    var searchMode = !!picker.searchQuery;
    var folders = searchMode
      ? matchedResourceFolders(resourcePickerTree(picker.searchResults))
      : resourceFolderChildren(current);
    var currentPath = firstString(current && current.path);
    var currentAllowed = pathAllowedByOrganizer(currentPath);
    var currentUnavailable = current && current.exists === false;
    var canSelectCurrent = !!currentPath && currentAllowed && !currentUnavailable && !picker.loading && !picker.searching;
    var cacheMissing = picker.cache && !picker.cache.cached;
    var cacheEmpty = picker.cache && !resourceFolderChildren(resourcePickerTree(picker.cache)).length;
    var status = "";
    if (picker.loading) {
      status = '<p class="organizer-resource-picker-status">正在读取资源库目录...</p>';
    } else if (picker.searching) {
      status = '<p class="organizer-resource-picker-status">正在搜索资源库缓存...</p>';
    } else if (picker.error) {
      status = '<p class="organizer-resource-picker-status is-error">' + esc(picker.error) + "</p>";
    } else if (cacheMissing || cacheEmpty) {
      status =
        '<div class="organizer-resource-picker-status is-warning"><p>' +
        esc(firstString(picker.cache && picker.cache.cache_note, "暂无可用的资源库目录缓存，请先到“资源库目录”完成配置和扫描。")) +
        '</p><button type="button" class="btn secondary sm" data-organizer-action="resource-picker-go-library">打开资源库目录</button></div>';
    } else if (!folders.length) {
      status = '<p class="organizer-resource-picker-status">' +
        (searchMode ? "没有匹配的文件夹。" : "当前目录没有子文件夹。") +
        "</p>";
    }
    return (
      '<div class="organizer-resource-picker-backdrop" data-resource-picker-backdrop>' +
      '<section class="organizer-resource-picker-dialog" role="dialog" aria-modal="true" aria-labelledby="organizer-resource-picker-title">' +
      '<header class="organizer-resource-picker-head"><div><h2 id="organizer-resource-picker-title">从资源库选择作品根目录</h2>' +
      '<p>只浏览已有资源库缓存；选择目录后仍需重新预览，才可执行整理。</p></div>' +
      '<button type="button" class="btn secondary sm organizer-resource-picker-close" data-organizer-action="resource-picker-close" aria-label="关闭资源库选择器">关闭</button></header>' +
      '<div class="organizer-resource-picker-search">' +
      '<label for="organizer-resource-picker-search-input">搜索文件夹</label>' +
      '<input id="organizer-resource-picker-search-input" type="search" value="' +
      esc(picker.searchDraft) +
      '" placeholder="输入文件夹名" autocomplete="off" data-resource-picker-search-input />' +
      '<button type="button" class="btn secondary sm" data-organizer-action="resource-picker-search"' +
      (picker.loading || picker.searching ? " disabled" : "") +
      ">" +
      (picker.searching ? "搜索中..." : "搜索") +
      "</button>" +
      (picker.searchQuery || picker.searchDraft
        ? '<button type="button" class="btn secondary sm" data-organizer-action="resource-picker-clear-search"' +
          (picker.loading || picker.searching ? " disabled" : "") +
          ">清除</button>"
        : "") +
      "</div>" +
      '<div class="organizer-resource-picker-location">' +
      '<button type="button" class="btn secondary sm" data-organizer-action="resource-picker-back"' +
      (!picker.history.length || picker.loading || picker.searching ? " disabled" : "") +
      ">返回上级</button>" +
      '<span><small>' +
      (searchMode ? "搜索结果" : "当前位置") +
      "</small><code>" +
      esc(searchMode ? picker.searchQuery : currentPath || "资源库") +
      "</code></span></div>" +
      '<div class="organizer-resource-picker-body">' +
      status +
      (folders.length
        ? '<ul class="organizer-resource-picker-list">' +
          folders.map(function (folder) {
            return renderResourcePickerFolder(folder, searchMode);
          }).join("") +
          "</ul>"
        : "") +
      "</div>" +
      '<footer class="organizer-resource-picker-footer"><span><small>当前目录</small><code>' +
      esc(currentPath || "请选择资源库中的具体文件夹") +
      "</code></span><div>" +
      '<button type="button" class="btn secondary" data-organizer-action="resource-picker-close">取消</button>' +
      '<button type="button" class="btn" data-organizer-action="resource-picker-select-current" data-resource-path="' +
      esc(currentPath) +
      '"' +
      (canSelectCurrent ? "" : " disabled") +
      ">使用当前文件夹</button></div></footer>" +
      "</section></div>"
    );
  }

  function assignmentRouteId(assignment) {
    return firstString(assignment && assignment.route_id, assignment && assignment.source_dir);
  }

  function structuredText(value) {
    if (value == null || value === "") return "";
    if (Array.isArray(value)) {
      return value
        .map(function (item) {
          return structuredText(item);
        })
        .filter(Boolean)
        .join("；");
    }
    if (typeof value === "object") {
      return Object.keys(value)
        .map(function (key) {
          var rendered = structuredText(value[key]);
          return rendered ? key + "：" + rendered : "";
        })
        .filter(Boolean)
        .join("；");
    }
    return String(value);
  }

  function pathKey(value) {
    return String(value || "")
      .replace(/\//g, "\\")
      .replace(/\\+$/g, "")
      .toLocaleLowerCase();
  }

  function relativePathUnder(path, parent) {
    var rawPath = String(path || "");
    var rawParent = String(parent || "").replace(/[\\/]+$/g, "");
    if (!rawPath || !rawParent) return rawPath;
    var normalizedPath = pathKey(rawPath);
    var normalizedParent = pathKey(rawParent);
    if (normalizedPath === normalizedParent) return ".";
    if (normalizedPath.indexOf(normalizedParent + "\\") !== 0) return rawPath;
    return rawPath.slice(rawParent.length).replace(/^[\\/]+/g, "");
  }

  function moveBelongsToAssignment(move, assignment) {
    var routeId = firstString(assignment && assignment.route_id);
    if (routeId) return String(move && move.route_id || "") === routeId;
    var source = pathKey(move && move.source);
    var sourceDir = pathKey(assignment && assignment.source_dir);
    return !!sourceDir && (source === sourceDir || source.indexOf(sourceDir + "\\") === 0);
  }

  function legacyTargetOverrides() {
    var out = Object.create(null);
    var assignments = state.plan && Array.isArray(state.plan.assignments)
      ? state.plan.assignments
      : [];
    assignments.forEach(function (assignment) {
      if (firstString(assignment.route_id)) return;
      var source = firstString(assignment.source_dir);
      if (!source || !Object.prototype.hasOwnProperty.call(state.routeOverrides, source)) return;
      out[source] = state.routeOverrides[source];
    });
    return out;
  }

  function sourcePressOverridesPayload() {
    var out = Object.create(null);
    Object.keys(state.sourcePressOverrides || {}).forEach(function (source) {
      var override = state.sourcePressOverrides[source];
      if (!override || typeof override !== "object") return;
      var pressFormat = firstString(override.press_format).trim();
      var pressGroup = firstString(override.press_group).trim();
      var sourceKey = firstString(source).trim();
      if (!sourceKey || (!pressFormat && !pressGroup)) return;
      var payload = Object.create(null);
      if (pressFormat) payload.press_format = pressFormat;
      if (pressGroup) payload.press_group = pressGroup;
      out[sourceKey] = payload;
    });
    return out;
  }

  function canRepairWithCurrentMediaPlan(plan) {
    return !!(
      plan &&
      !plan.registration_required &&
      Array.isArray(plan.assignments) &&
      plan.assignments.length &&
      Array.isArray(plan.moves) &&
      plan.moves.length
    );
  }

  function mediaPlanHasMultipleWorks(plan) {
    var seen = Object.create(null);
    var count = 0;
    (plan && Array.isArray(plan.assignments) ? plan.assignments : []).forEach(
      function (assignment) {
        var workName = firstString(assignment && assignment.work_name).trim();
        if (!workName) return;
        var catalogFile = firstString(assignment && assignment.catalog_file).trim();
        var key = (catalogFile + "\u0000" + workName).toLocaleLowerCase();
        if (seen[key]) return;
        seen[key] = true;
        count += 1;
      },
    );
    return count > 1;
  }

  function withRepairMediaPlan(payload, pending) {
    if (!pending || pending.includeMediaMove !== true) return payload;
    payload.include_media_move = true;
    payload.target_overrides = legacyTargetOverrides();
    payload.route_target_overrides = cloneJson(state.routeOverrides || {});
    payload.file_work_overrides = cloneJson(state.fileWorkOverrides || {});
    payload.source_work_bindings = sourceWorkBindingsPayload();
    payload.source_work_overrides = sourceWorkOverridesPayload(payload.source_work_bindings);
    payload.source_press_overrides = sourcePressOverridesPayload();
    return payload;
  }

  function summaryCards(plan) {
    var summary = (plan && plan.summary) || {};
    var assignmentCount = summary.assignment_count;
    if (assignmentCount == null) {
      assignmentCount = plan && Array.isArray(plan.assignments) ? plan.assignments.length : 0;
    }
    var unresolvedCount = summary.unresolved_file_count;
    if (unresolvedCount == null) {
      unresolvedCount = plan && Array.isArray(plan.unresolved_files)
        ? plan.unresolved_files.length
        : 0;
    }
    var issueCount = isAlreadyOrganizedPlan(plan)
      ? visiblePlanIssues(plan).length
      : summary.issue_count || 0;
    var cards = [
      ["来源目录", summary.source_directory_count || 0],
      ["目标目录", summary.target_directory_count || 0],
      ["路由任务", assignmentCount || 0],
      ["移动文件", summary.file_count || 0],
      ["回退分类", summary.fallback_file_count || 0],
      ["未决文件", unresolvedCount || 0],
      ["总大小", formatBytes(summary.bytes || 0)],
      ["问题", issueCount],
    ];
    return cards
      .map(function (item) {
        return (
          '<div class="organizer-summary-card"><span>' +
          esc(item[0]) +
          "</span><strong>" +
          esc(item[1]) +
          "</strong></div>"
        );
      })
      .join("");
  }

  function authorityLabel(value) {
    if (value === "database_press_path") return "数据库 press_path";
    if (value === "derived_from_catalog") return "数据库推导";
    if (value === "user_override") return "人工修正";
    return value || "未知";
  }

  function portableDirname(value) {
    var normalized = String(value || "")
      .replace(/\//g, "\\")
      .replace(/\\+$/g, "");
    var separator = normalized.lastIndexOf("\\");
    return separator >= 0 ? normalized.slice(0, separator) : ".";
  }

  function portableBasename(value) {
    var normalized = String(value || "")
      .replace(/\//g, "\\")
      .replace(/\\+$/g, "");
    if (!normalized || normalized === ".") return normalized || ".";
    var separator = normalized.lastIndexOf("\\");
    return separator >= 0 ? normalized.slice(separator + 1) : normalized;
  }

  function portablePathParts(value) {
    return String(value || "")
      .replace(/\//g, "\\")
      .split("\\")
      .filter(function (part) {
        return part && part !== ".";
      });
  }

  function portableJoin(base, component) {
    var root = String(base || "").replace(/[\\/]+$/g, "");
    if (!root || root === ".") return String(component || "");
    return root + "\\" + String(component || "");
  }

  function moveTargetDirectory(move, assignment) {
    var destination = firstString(
      move && move.destination_relpath,
      relativePathUnder(move && move.target, state.root),
    );
    if (destination) return portableDirname(destination);
    var targetRelative = firstString(
      move && move.target_relative_path,
      move && move.target_relpath,
      relativePathUnder(move && move.target, assignment && assignment.target_dir),
    );
    var fallback = portableDirname(targetRelative);
    if (fallback !== ".") return fallback;
    return firstString(
      move && move.category_relpath,
      relativePathUnder(move && move.category_dir, state.root),
      assignment && assignment.target_relpath,
      assignment && assignment.target_dir,
      ".",
    );
  }

  function assignmentTargetRoot(assignment) {
    return firstString(
      assignment && assignment.target_relpath,
      relativePathUnder(assignment && assignment.target_dir, state.root),
      assignment && assignment.target_dir,
      ".",
    );
  }

  function moveDirectoryInsideAssignment(move, assignment, rootDirectory) {
    var targetRelative = firstString(
      move && move.target_relative_path,
      move && move.target_relpath,
      relativePathUnder(move && move.target, assignment && assignment.target_dir),
    );
    if (targetRelative) return portableDirname(targetRelative);
    var targetDirectory = moveTargetDirectory(move, assignment);
    var relativeDirectory = relativePathUnder(targetDirectory, rootDirectory);
    return relativeDirectory || ".";
  }

  function createMoveDirectoryNode(name, path, depth) {
    return {
      name: name,
      path: path,
      depth: depth,
      children: Object.create(null),
      moves: [],
      directBytes: 0,
      totalFiles: 0,
      totalBytes: 0,
      totalDirectories: 1,
    };
  }

  function sortedMoveDirectoryChildren(node) {
    if (node.sortedChildren) return node.sortedChildren;
    return Object.keys(node.children)
      .map(function (key) {
        return node.children[key];
      })
      .sort(function (left, right) {
        return String(left.name).localeCompare(String(right.name), "zh-CN", {
          numeric: true,
          sensitivity: "base",
        });
      });
  }

  function summarizeMoveDirectoryNode(node) {
    var children = sortedMoveDirectoryChildren(node);
    node.sortedChildren = children;
    node.totalFiles = node.moves.length;
    node.totalBytes = node.directBytes;
    node.totalDirectories = 1;
    children.forEach(function (child) {
      summarizeMoveDirectoryNode(child);
      node.totalFiles += child.totalFiles;
      node.totalBytes += child.totalBytes;
      node.totalDirectories += child.totalDirectories;
    });
  }

  function buildMoveDirectoryTree(moves, assignment) {
    var rootPath = assignmentTargetRoot(assignment);
    var root = createMoveDirectoryNode(
      rootPath === "." ? "整理目标" : rootPath,
      rootPath,
      0,
    );
    var targetDirectories = Object.create(null);
    var nodesByPath = Object.create(null);
    nodesByPath[pathKey(root.path)] = root;
    moves.forEach(function (move) {
      var targetDirectory = moveTargetDirectory(move, assignment);
      targetDirectories[pathKey(targetDirectory) || targetDirectory] = true;
      var relativeDirectory = moveDirectoryInsideAssignment(move, assignment, rootPath);
      var node = root;
      portablePathParts(relativeDirectory).forEach(function (component) {
        var key = pathKey(component) || component;
        if (!node.children[key]) {
          node.children[key] = createMoveDirectoryNode(
            component,
            portableJoin(node.path, component),
            node.depth + 1,
          );
          nodesByPath[pathKey(node.children[key].path)] = node.children[key];
        }
        node = node.children[key];
      });
      node.moves.push(move);
      node.directBytes += Number(move && move.size) || 0;
    });
    summarizeMoveDirectoryNode(root);
    return {
      root: root,
      targetDirectoryCount: Object.keys(targetDirectories).length,
      nodesByPath: nodesByPath,
    };
  }

  function moveClassificationBasis(move) {
    var rule = structuredText(move && move.classification_rule);
    var reason = structuredText(
      firstString(
        move && move.classification_reason,
        move && move.layout_reason,
        move && move.reason,
      ),
    );
    return [rule, reason]
      .filter(function (value, index, values) {
        return value && values.indexOf(value) === index;
      })
      .join("；");
  }

  function moveFilePage(node, moveView) {
    var pageCount = Math.max(1, Math.ceil(node.moves.length / MOVE_FILE_PAGE_SIZE));
    var page = Number(moveView.filePages[pathKey(node.path)]) || 0;
    page = Math.max(0, Math.min(pageCount - 1, Math.floor(page)));
    return { index: page, count: pageCount, start: page * MOVE_FILE_PAGE_SIZE,
      end: Math.min(node.moves.length, (page + 1) * MOVE_FILE_PAGE_SIZE) };
  }

  function renderMoveFilePager(node, moveView, page) {
    if (page.count <= 1) return "";
    var attrs = ' data-organizer-action="move-files-page" data-move-view="' + esc(moveView.id) +
      '" data-move-node="' + esc(pathKey(node.path)) + '"';
    return '<div class="organizer-file-preview-pager">' +
      '<span role="status" aria-live="polite">文件预览：第 ' + esc(page.start + 1) + '–' + esc(page.end) +
      ' 条 / 共 ' + esc(node.moves.length) + ' 条</span>' +
      '<button type="button" class="btn secondary sm"' + attrs + ' data-move-page="' + esc(page.index - 1) +
      '" aria-label="' + esc(node.name + '：上一页文件') + '"' + (page.index === 0 ? ' disabled' : '') + '>上一页</button>' +
      '<button type="button" class="btn secondary sm"' + attrs + ' data-move-page="' + esc(page.index + 1) +
      '" aria-label="' + esc(node.name + '：下一页文件') + '"' + (page.index + 1 === page.count ? ' disabled' : '') + '>下一页</button>' +
      '<small>分页仅影响显示，执行计划包含全部文件。</small></div>';
  }

  function renderMoveDirectoryFiles(node, assignment, moveView) {
    if (!node.moves.length) return "";
    var page = moveFilePage(node, moveView);
    var visible = node.moves.slice(page.start, page.end);
    var rows = visible.map(function (move) {
      var sourceRelpath = firstString(
        move.source_relpath,
        relativePathUnder(move.source, assignment.source_dir),
      );
      var targetPath = firstString(
        move.destination_relpath,
        move.target,
        move.target_relative_path,
      );
      var targetName = portableBasename(
        firstString(move.target_relative_path, targetPath, move.source),
      );
      return (
        "<tr><td><code>" +
        esc(sourceRelpath || move.source || "-") +
        "</code></td><td><code title=\"" +
        esc(targetPath) +
        "\">" +
        esc(targetName || "-") +
        "</code></td><td>" +
        esc(moveClassificationBasis(move) || "继承当前路由") +
        "</td></tr>"
      );
    }).join("");
    return (
      renderMoveFilePager(node, moveView, page) +
      '<div class="organizer-move-table-wrap"><table class="organizer-move-table">' +
      "<thead><tr><th>来源相对路径</th><th>目标文件</th><th>分类规则 / 依据</th></tr></thead>" +
      "<tbody>" +
      rows +
      "</tbody></table></div>"
    );
  }

  function moveDirectoryIsOpen(node, moveView) {
    var expanded = moveView.expandedDirectories[pathKey(node.path)];
    return expanded == null ? node.depth === 0 : expanded;
  }

  function renderMoveDirectoryBody(node, assignment, moveView) {
    var children = sortedMoveDirectoryChildren(node);
    return renderMoveDirectoryFiles(node, assignment, moveView) + (children.length
      ? '<ul class="organizer-directory-children">' + children.map(function (child) {
          return "<li>" + renderMoveDirectoryNode(child, assignment, moveView) + "</li>";
        }).join("") + "</ul>"
      : "");
  }

  function renderMoveDirectoryNode(node, assignment, moveView) {
    var children = sortedMoveDirectoryChildren(node);
    var open = moveDirectoryIsOpen(node, moveView);
    var directLabel = node.moves.length !== node.totalFiles
      ? "（本层 " + node.moves.length + "）"
      : "";
    var childLabel = children.length
      ? " · " + children.length + " 个下级目录"
      : "";
    return (
      '<details class="organizer-directory-node" data-directory-depth="' +
      esc(node.depth) +
      '" data-move-view="' + esc(moveView.id) + '" data-move-node="' + esc(pathKey(node.path)) + '"' +
      (open ? " open" : "") +
      "><summary>" +
      '<span class="organizer-directory-label"><span aria-hidden="true">📁</span><code title="' +
      esc(node.path) +
      '">' +
      esc(node.name) +
      "</code></span>" +
      '<span class="organizer-directory-meta">' +
      esc(node.totalFiles) +
      " 个文件" +
      esc(directLabel) +
      " · " +
      esc(formatBytes(node.totalBytes)) +
      esc(childLabel) +
      "</span></summary>" +
      '<div class="organizer-directory-body" data-move-body>' +
      (open ? renderMoveDirectoryBody(node, assignment, moveView) : "") +
      "</div></details>"
    );
  }

  function assignmentMoveView(plan, assignment) {
    // Server previews replace their moves array; edits live in separate override maps.
    // Keep preparation and expansion state scoped to that reviewed snapshot.
    var moves = plan && Array.isArray(plan.moves) ? plan.moves : [];
    var prepared = movePlanViews.get(plan);
    if (!prepared || prepared.moves !== moves || prepared.moveCount !== moves.length) {
      prepared = { moves: moves, moveCount: moves.length, byRoute: Object.create(null), assignments: new WeakMap() };
      moves.forEach(function (move) {
        var route = String(move && move.route_id || "");
        (prepared.byRoute[route] || (prepared.byRoute[route] = [])).push(move);
      });
      movePlanViews.set(plan, prepared);
    }
    var result = prepared.assignments.get(assignment);
    if (result) return result;
    var routeId = firstString(assignment && assignment.route_id);
    var matched = routeId ? prepared.byRoute[routeId] || [] : moves.filter(function (move) {
      return moveBelongsToAssignment(move, assignment);
    });
    var targetDirectories = Object.create(null);
    matched.forEach(function (move) {
      var directory = moveTargetDirectory(move, assignment);
      targetDirectories[pathKey(directory) || directory] = true;
    });
    result = { id: String(++moveViewSerial), assignment: assignment, moves: matched,
      targetDirectoryCount: Object.keys(targetDirectories).length, tree: null,
      open: false, expandedDirectories: Object.create(null), filePages: Object.create(null) };
    prepared.assignments.set(assignment, result);
    return result;
  }

  function renderAssignmentMoveBody(moveView) {
    if (!moveView.tree) moveView.tree = buildMoveDirectoryTree(moveView.moves, moveView.assignment);
    return '<ul class="organizer-move-tree"><li>' +
      renderMoveDirectoryNode(moveView.tree.root, moveView.assignment, moveView) + "</li></ul>";
  }

  function onMoveDetailsToggle(event) {
    var details = event.target;
    if (!details || !details.getAttribute) return;
    var target = view();
    if (!target || !target.contains(details)) return;
    var moveView = renderedMoveViews[details.getAttribute("data-move-view")];
    if (!moveView) return;
    var nodePath = details.getAttribute("data-move-node");
    var body = details.querySelector(":scope > [data-move-body]");
    if (!body) return;
    if (nodePath == null) {
      if (moveView.open === !!details.open) return;
      moveView.open = !!details.open;
      body.innerHTML = details.open ? renderAssignmentMoveBody(moveView) : "";
    } else {
      var node = moveView.tree && moveView.tree.nodesByPath[nodePath];
      if (!node) return;
      if (moveDirectoryIsOpen(node, moveView) === !!details.open) return;
      moveView.expandedDirectories[nodePath] = !!details.open;
      body.innerHTML = details.open ? renderMoveDirectoryBody(node, moveView.assignment, moveView) : "";
    }
  }

  function changeMoveFilePage(button) {
    var moveView = renderedMoveViews[button.getAttribute("data-move-view")];
    var nodePath = button.getAttribute("data-move-node");
    var node = moveView && moveView.tree && moveView.tree.nodesByPath[nodePath];
    var page = Number(button.getAttribute("data-move-page"));
    if (!node || !Number.isInteger(page) || page < 0 || page >= Math.ceil(node.moves.length / MOVE_FILE_PAGE_SIZE)) return;
    var body = button.closest("[data-move-body]");
    if (!body) return;
    var previousPage = moveFilePage(node, moveView).index;
    moveView.filePages[nodePath] = page;
    body.innerHTML = renderMoveDirectoryBody(node, moveView.assignment, moveView);
    var nextPage = page + (page > previousPage ? 1 : -1);
    var focusButton = body.querySelector(':scope > .organizer-file-preview-pager [data-move-page="' + nextPage + '"]:not(:disabled)') ||
      body.querySelector(':scope > .organizer-file-preview-pager button:not(:disabled)');
    if (focusButton && focusButton.focus) focusButton.focus({ preventScroll: true });
  }

  function renderAssignmentMoves(plan, assignment) {
    var moveView = assignmentMoveView(plan, assignment);
    if (!moveView.moves.length) {
      return '<p class="organizer-move-empty">当前路由没有可显示的文件明细。</p>';
    }
    renderedMoveViews[moveView.id] = moveView;
    return (
      '<details class="organizer-move-details" data-move-view="' + esc(moveView.id) + '"' +
      (moveView.open ? " open" : "") + '><summary>文件明细（' +
      esc(moveView.moves.length) +
      " 个文件 / " +
      esc(moveView.targetDirectoryCount) +
      " 个目标目录）</summary>" +
      '<div data-move-body>' + (moveView.open ? renderAssignmentMoveBody(moveView) : "") +
      "</div></details>"
    );
  }

  function renderAssignments(plan) {
    var assignments = plan && Array.isArray(plan.assignments) ? plan.assignments : [];
    var inputDisabled = state.busy || state.landing || state.shortcutPending ? " disabled" : "";
    if (!assignments.length) {
      return '<p class="organizer-empty">当前预览没有生成可调整的目标路由。</p>';
    }
    return assignments
      .map(function (assignment, index) {
        var source = String(assignment.source_dir || "");
        var routeId = assignmentRouteId(assignment);
        var targetValue = Object.prototype.hasOwnProperty.call(state.routeOverrides, routeId)
          ? state.routeOverrides[routeId]
          : firstString(assignment.target_relpath, assignment.target_dir);
        var details = [
          assignment.work_name,
          assignment.press_format,
          assignment.press_group,
        ]
          .filter(function (value) {
            return value != null && String(value).trim();
          })
          .join(" / ");
        var classifierIds = structuredText(assignment.classifier_ids);
        var classificationRules = structuredText(assignment.classification_rules);
        var suggestedTarget = firstString(assignment.suggested_target_relpath);
        return (
          '<article class="organizer-assignment" data-organizer-route-card="' +
          esc(routeId) +
          '">' +
          '<div class="organizer-assignment-head">' +
          '<div><h3>目标路由 ' +
          esc(index + 1) +
          '</h3><p class="organizer-route-id">route：<code>' +
          esc(routeId || "-") +
          "</code></p></div>" +
          '<span class="organizer-authority">' +
          esc(authorityLabel(assignment.target_authority)) +
          "</span>" +
          "</div>" +
          '<dl class="organizer-assignment-meta">' +
          "<div><dt>来源目录</dt><dd><code>" +
          esc(source) +
          "</code></dd></div>" +
          "<div><dt>作品 / 压制</dt><dd>" +
          esc(details || "-") +
          "</dd></div>" +
          "<div><dt>内容</dt><dd>" +
          esc(assignment.file_count || 0) +
          " 个文件，" +
          esc(formatBytes(assignment.bytes || 0)) +
          "</dd></div>" +
          (classifierIds
            ? "<div><dt>分类器</dt><dd>" + esc(classifierIds) + "</dd></div>"
            : "") +
          (classificationRules
            ? "<div><dt>路由依据</dt><dd>" + esc(classificationRules) + "</dd></div>"
            : "") +
          "</dl>" +
          '<label class="organizer-target-field">' +
          "<span>目标目录（相对作品根目录）</span>" +
          '<input type="text" class="organizer-target-input" data-organizer-target="1" data-route-id="' +
          esc(routeId) +
          '" value="' +
          esc(targetValue) +
          '" autocomplete="off" spellcheck="false"' +
          inputDisabled +
          " />" +
          (suggestedTarget
            ? '<small class="organizer-target-suggestion">建议目标：<code>' +
              esc(suggestedTarget) +
              "</code></small>"
            : "") +
          "</label>" +
          renderAssignmentMoves(plan, assignment) +
          "</article>"
        );
      })
      .join("");
  }

  function normalizedSourceWorkCandidate(raw) {
    if (typeof raw === "string") {
      return { name: firstString(raw), catalogRef: null, work: { name: firstString(raw) } };
    }
    raw = raw && typeof raw === "object" ? raw : {};
    var work = firstObject(raw.work, raw.draft_work, raw.entry);
    var catalogRef = nonEmptyObject(raw.catalog_ref);
    var name = firstString(raw.name, raw.work_name, work.name, catalogRef && catalogRef.work_name);
    return name ? { name: name, catalogRef: catalogRef ? cloneJson(catalogRef) : null, work: work } : null;
  }

  function sourceWorkCandidateKey(candidate) {
    candidate = candidate && typeof candidate === "object" ? candidate : {};
    var catalogRef = nonEmptyObject(candidate.catalogRef) || nonEmptyObject(candidate.catalog_ref);
    if (!catalogRef) return "";
    var source = firstString(catalogRef.yaml_source_rel);
    var index = Number(catalogRef.index_in_file);
    var workName = firstString(catalogRef.work_name, candidate.name);
    var sourceSha256 = firstString(catalogRef.source_sha256);
    if (!source || !Number.isInteger(index) || index < 0 || !workName) return "";
    return encodeURIComponent(JSON.stringify([source, index, workName, sourceSha256]));
  }

  function sourceWorkCandidateLabel(candidate, duplicateNames) {
    var label = firstString(candidate && candidate.name, "未命名作品");
    var catalogRef = nonEmptyObject(candidate && candidate.catalogRef);
    if (!catalogRef) return label;
    var duplicate = duplicateNames && duplicateNames[label.toLocaleLowerCase()] > 1;
    if (!duplicate) return label;
    return (
      label +
      " — " +
      firstString(catalogRef.yaml_source_rel, "数据库") +
      " #" +
      String(Number(catalogRef.index_in_file) + 1)
    );
  }

  function sourceWorkBindingRows(plan) {
    plan = plan && typeof plan === "object" ? plan : {};
    var rowsByName = Object.create(null);
    var orderedNames = [];

    function ensureRow(sourceName, sourcePath) {
      sourceName = firstString(sourceName, portableBasename(sourcePath));
      if (!sourceName) return null;
      if (!rowsByName[sourceName]) {
        rowsByName[sourceName] = {
          sourceName: sourceName,
          sourcePath: firstString(sourcePath, portableJoin(state.root, sourceName)),
          selected: true,
          requestedMode: "",
          requestedWorkName: "",
          suggestedWorkName: "",
          suggestionAuthority: "",
          state: "unbound",
          catalogRef: null,
          work: {},
          resolvedWorks: [],
          candidates: [],
          registrationRequired: false,
          catalogPressRequired: false,
          catalogRepairRequired: false,
          nextAction: "",
          inferredPress: {},
          inferredPresses: [],
          registration: {},
          diagnostics: [],
        };
        orderedNames.push(sourceName);
      } else if (!rowsByName[sourceName].sourcePath && sourcePath) {
        rowsByName[sourceName].sourcePath = String(sourcePath);
      }
      return rowsByName[sourceName];
    }

    function addCandidate(row, candidate) {
      candidate = normalizedSourceWorkCandidate(candidate);
      if (!row || !candidate) return;
      var key = catalogRefKey(candidate.catalogRef) || candidate.name.toLocaleLowerCase();
      if (row.candidates.some(function (item) {
        return (catalogRefKey(item.catalogRef) || item.name.toLocaleLowerCase()) === key;
      })) return;
      row.candidates.push(candidate);
    }

    var rawRows = Array.isArray(plan.source_work_bindings)
      ? plan.source_work_bindings
      : [];
    rawRows.forEach(function (raw) {
      raw = raw && typeof raw === "object" ? raw : {};
      var row = ensureRow(
        firstString(raw.source_name),
        firstString(raw.source_path, raw.source_dir),
      );
      if (!row) return;
      row.requestedMode = firstString(raw.requested_mode);
      row.requestedWorkName = firstString(raw.requested_work_name);
      row.suggestedWorkName = firstString(raw.suggested_work_name, raw.work_title_hint);
      row.suggestionAuthority = firstString(raw.suggestion_authority);
      row.selected = raw.selected !== false;
      row.state = firstString(raw.state, "unbound");
      row.catalogRef = nonEmptyObject(raw.catalog_ref) ? cloneJson(raw.catalog_ref) : null;
      row.resolvedWorks = (Array.isArray(raw.resolved_works)
        ? raw.resolved_works
        : raw.work
        ? [raw.work]
        : [])
        .map(normalizedSourceWorkCandidate)
        .filter(Boolean);
      row.work = row.resolvedWorks.length
        ? firstObject(row.resolvedWorks[0].work)
        : firstObject(raw.work);
      if (!row.catalogRef && row.resolvedWorks.length === 1) {
        row.catalogRef = nonEmptyObject(row.resolvedWorks[0].catalogRef)
          ? cloneJson(row.resolvedWorks[0].catalogRef)
          : null;
      }
      row.registrationRequired = raw.registration_required === true;
      row.catalogPressRequired = raw.catalog_press_required === true;
      row.catalogRepairRequired = raw.catalog_repair_required === true;
      row.nextAction = firstString(raw.next_action);
      row.inferredPresses = (Array.isArray(raw.inferred_press)
        ? raw.inferred_press
        : raw.inferred_press || raw.press
        ? [firstObject(raw.inferred_press, raw.press)]
        : [])
        .filter(function (press) {
          return press && typeof press === "object" && !Array.isArray(press);
        })
        .map(function (press) { return cloneJson(press); });
      row.inferredPress = row.inferredPresses.length
        ? firstObject(row.inferredPresses[0])
        : {};
      row.registration = firstObject(raw.registration);
      row.diagnostics = Array.isArray(raw.shortcut_diagnostics)
        ? raw.shortcut_diagnostics.slice()
        : [];
      (Array.isArray(raw.candidates) ? raw.candidates : []).forEach(function (candidate) {
        addCandidate(row, candidate);
      });
      row.resolvedWorks.forEach(function (candidate) { addCandidate(row, candidate); });
      if (row.catalogRef || firstString(row.work.name)) {
        addCandidate(row, {
          catalog_ref: row.catalogRef,
          work: row.work,
          work_name: firstString(row.work.name, row.catalogRef && row.catalogRef.work_name),
        });
      }
    });

    (Array.isArray(plan.assignments) ? plan.assignments : []).forEach(function (assignment) {
      var row = ensureRow(
        portableBasename(assignment && assignment.source_dir),
        assignment && assignment.source_dir,
      );
      if (!row) return;
      if (!firstString(row.work.name)) {
        row.work = {
          name: firstString(assignment && assignment.work_name),
          path: firstString(assignment && assignment.work_path),
          domain: firstString(assignment && assignment.domain),
          country: firstString(assignment && assignment.country),
          release_type: firstString(assignment && assignment.release_type),
        };
      }
      if (row.state === "unbound" && firstString(assignment && assignment.work_name)) {
        row.state = "catalog_bound";
      }
      row.inferredPress = Object.assign({}, row.inferredPress, {
        press_format: firstString(row.inferredPress.press_format, assignment && assignment.press_format),
        press_group: firstString(row.inferredPress.press_group, assignment && assignment.press_group),
        press_path: firstString(
          row.inferredPress.press_path,
          assignment && assignment.target_relpath,
        ),
      });
      row.inferredPresses = [cloneJson(row.inferredPress)];
      addCandidate(row, firstString(assignment && assignment.work_name));
    });

    (Array.isArray(plan.unresolved_files) ? plan.unresolved_files : []).forEach(function (item) {
      var sourcePath = firstString(item && item.source_dir, item && item.source);
      var row = ensureRow(portableBasename(sourcePath), sourcePath);
      if (!row) return;
      if (
        row.state === "catalog_bound" ||
        row.state === "manual_matched" ||
        row.state === "automatic_matched" ||
        row.state === "settled"
      ) return;
      if (row.state === "unbound" || row.state === "unresolved") {
        row.state = "selection_required";
      }
      (Array.isArray(item && item.candidates) ? item.candidates : []).forEach(function (candidate) {
        addCandidate(row, candidate);
      });
      (Array.isArray(item && item.unmatched_work_name_hints)
        ? item.unmatched_work_name_hints
        : []).forEach(function (candidate) {
        addCandidate(row, candidate);
      });
    });

    var registration = firstObject(plan.registration);
    (Array.isArray(registration.sources) ? registration.sources : []).forEach(function (source) {
      var row = ensureRow(source && source.name, source && source.path);
      if (!row) return;
      row.registration = row.registration && Object.keys(row.registration).length
        ? row.registration
        : { draft: firstObject(registration.draft) };
      row.inferredPress = Object.assign({}, row.inferredPress, {
        press_format: firstString(source && source.suggested_press_format),
        press_group: firstString(source && source.suggested_press_group),
        press_path: firstString(source && source.suggested_press_path),
      });
      row.inferredPresses = [cloneJson(row.inferredPress)];
    });

    var familyWorks = Array.isArray(plan.family_works) ? plan.family_works : [];
    orderedNames.forEach(function (sourceName) {
      familyWorks.forEach(function (workName) { addCandidate(rowsByName[sourceName], workName); });
    });

    var topDiagnostics = Array.isArray(plan.shortcut_diagnostics)
      ? plan.shortcut_diagnostics
      : Array.isArray(plan.legacy_shortcut_diagnostics)
      ? plan.legacy_shortcut_diagnostics
      : [];
    topDiagnostics.forEach(function (diagnostic) {
      diagnostic = diagnostic && typeof diagnostic === "object" ? diagnostic : {};
      var sourceName = firstString(
        diagnostic.source_name,
        portableBasename(diagnostic.source_path || diagnostic.source_dir),
      );
      var row = sourceName && rowsByName[sourceName];
      if (row) row.diagnostics.push(diagnostic);
    });

    return orderedNames.map(function (name) { return rowsByName[name]; });
  }

  function sourceWorkBindingEdits() {
    if (!state.sourceWorkBindingEdits || typeof state.sourceWorkBindingEdits !== "object") {
      state.sourceWorkBindingEdits = Object.create(null);
    }
    return state.sourceWorkBindingEdits;
  }

  function sourceWorkDrafts() {
    if (!state.sourceWorkDrafts || typeof state.sourceWorkDrafts !== "object") {
      state.sourceWorkDrafts = Object.create(null);
    }
    return state.sourceWorkDrafts;
  }

  function clearSourceWorkBindingState() {
    state.sourceWorkBindingEdits = Object.create(null);
    state.sourceWorkDrafts = Object.create(null);
    state.sourceWorkEditorSource = "";
    state.sourceWorkEditorMode = "";
    state.landingSourceWorkBindings = null;
  }

  function resetPlanScopedStateForRootChange(nextRoot, force) {
    nextRoot = String(nextRoot == null ? "" : nextRoot);
    var changed = force === true ||
      pathKey(String(state.root || "").trim()) !== pathKey(nextRoot.trim());
    state.root = nextRoot;
    if (!changed) return false;

    requestSerial += 1;
    state.plan = null;
    state.execution = null;
    state.recovery = null;
    state.routeOverrides = Object.create(null);
    state.fileWorkOverrides = Object.create(null);
    state.sourceWorkOverrides = Object.create(null);
    clearSourceWorkBindingState();
    state.multipleWorkSources = Object.create(null);
    state.sourcePressOverrides = Object.create(null);
    state.landing = null;
    state.draftWork = null;
    state.draftWorks = [];
    state.sharedTarget = createSharedTargetState();
    state.shortcutPending = null;
    state.busy = "";
    invalidateLandingRecognition("", "作品根目录已变化，旧识别候选已作废。");
    return true;
  }

  function sourceUsesPerFileRouting(row) {
    row = row && typeof row === "object" ? row : {};
    var sourceName = firstString(row.sourceName, row.source_name);
    var sourcePath = firstString(
      row.sourcePath,
      row.source_path,
      sourceName && portableJoin(state.root, sourceName),
    );
    return Object.keys(state.multipleWorkSources || {}).some(function (source) {
      return state.multipleWorkSources[source] === true &&
        (pathKey(source) === pathKey(sourcePath) ||
          String(source).trim().toLocaleLowerCase() ===
            String(sourceName).trim().toLocaleLowerCase());
    });
  }

  function sourceBindingPressTemplate(row) {
    row = row && typeof row === "object" ? row : {};
    var registrationDraft = firstObject(row.registration && row.registration.draft);
    var registrationPresses = draftPresses(registrationDraft).filter(function (press) {
      var sourceNames = Array.isArray(press && press.source_names)
        ? press.source_names.map(String)
        : [];
      return sourceNames.indexOf(row.sourceName) >= 0;
    });
    var inferred = Array.isArray(row.inferredPresses) && row.inferredPresses.length
      ? row.inferredPresses
      : nonEmptyObject(row.inferredPress)
      ? [row.inferredPress]
      : [];
    var existingPresses = draftPresses(firstObject(row.work));
    var presses = registrationPresses.length ? registrationPresses : inferred;
    if (!presses.length && existingPresses.length === 1) presses = existingPresses;
    if (!presses.length) presses = [{}];
    return presses.map(function (press) {
      press = press && typeof press === "object" ? press : {};
      var matchedExisting = existingPresses.find(function (existing) {
        if (
          firstString(press.press_format) &&
          firstString(existing && existing.press_format).toLocaleLowerCase() !==
            firstString(press.press_format).toLocaleLowerCase()
        ) return false;
        if (
          firstString(press.press_group) &&
          firstString(existing && existing.press_group).toLocaleLowerCase() !==
            firstString(press.press_group).toLocaleLowerCase()
        ) return false;
        return true;
      }) || {};
      return {
        source_names: [row.sourceName],
        press_format: firstString(press.press_format, matchedExisting.press_format),
        press_group: firstString(press.press_group, matchedExisting.press_group),
        press_path: firstString(
          press.press_path,
          press.suggested_press_path,
          matchedExisting.press_path,
          row.sourceName,
        ),
      };
    });
  }

  function sourceBindingCatalogPresses(row, edit) {
    var presses = edit && Array.isArray(edit.presses)
      ? edit.presses
      : row && row.catalogPressRequired
      ? sourceBindingPressTemplate(row)
      : [];
    return presses.map(function (press) {
      press = press && typeof press === "object" ? press : {};
      return {
        source_names: [row.sourceName],
        press_format: firstString(press.press_format),
        press_group: firstString(press.press_group),
        press_path: firstString(press.press_path),
      };
    });
  }

  function sourceBindingRowForCatalogRef(row, catalogRef) {
    row = row && typeof row === "object" ? row : {};
    var wanted = sourceWorkCandidateKey({
      name: firstString(catalogRef && catalogRef.work_name),
      catalogRef: catalogRef,
    });
    var candidate = wanted && Array.isArray(row.candidates)
      ? row.candidates.find(function (item) {
          return sourceWorkCandidateKey(item) === wanted;
        })
      : null;
    return candidate && nonEmptyObject(candidate.work)
      ? Object.assign({}, row, { work: candidate.work })
      : row;
  }

  function catalogBindingPayload(catalogRef, row, edit, includePress) {
    row = sourceBindingRowForCatalogRef(row, catalogRef);
    var payload = {
      mode: "catalog",
      catalog_ref: cloneJson(catalogRef),
    };
    var presses = sourceBindingCatalogPresses(row, edit);
    if (!presses.length && includePress) presses = sourceBindingPressTemplate(row);
    if (presses.length) payload.presses = [presses[0]];
    return payload;
  }

  function sourceWorkBindingsPayload(options) {
    options = options && typeof options === "object" ? options : {};
    var forLanding = options.forLanding === true;
    var payload = Object.create(null);
    var edits = sourceWorkBindingEdits();
    Object.keys(edits).forEach(function (sourceName) {
      var edit = edits[sourceName];
      var row = sourceWorkBindingRow(sourceName);
      if (row && sourceUsesPerFileRouting(row)) return;
      if (!edit || typeof edit !== "object") return;
      if (edit.mode === "catalog" && nonEmptyObject(edit.catalog_ref)) {
        payload[sourceName] = catalogBindingPayload(edit.catalog_ref, row || {
          sourceName: sourceName,
          catalogPressRequired: false,
        }, edit, forLanding);
      } else if (edit.mode === "draft" && nonEmptyObject(edit.draft_work)) {
        payload[sourceName] = {
          mode: "draft",
          draft_work: cloneJson(edit.draft_work),
        };
      } else if (edit.mode === "manual" && firstString(edit.work_name)) {
        if (
          row &&
          nonEmptyObject(row.catalogRef) &&
          (forLanding || row.state === "manual_matched")
        ) {
          payload[sourceName] = catalogBindingPayload(
            row.catalogRef,
            row,
            null,
            true,
          );
        } else {
          payload[sourceName] = {
            mode: "manual",
            work_name: firstString(edit.work_name),
          };
        }
      }
    });
    sourceWorkBindingRows(state.plan).forEach(function (row) {
      if (sourceUsesPerFileRouting(row)) return;
      if (payload[row.sourceName]) return;
      var legacyWorkName = firstString(
        state.sourceWorkOverrides && state.sourceWorkOverrides[row.sourcePath],
        state.sourceWorkOverrides && state.sourceWorkOverrides[row.sourceName],
      );
      if (legacyWorkName && !forLanding) return;
      if (nonEmptyObject(row.catalogRef)) {
        payload[row.sourceName] = catalogBindingPayload(
          row.catalogRef,
          row,
          null,
          forLanding,
        );
        return;
      }
      if (row.resolvedWorks.length === 1 && nonEmptyObject(row.resolvedWorks[0].catalogRef)) {
        if (
          row.state === "catalog_bound" ||
          row.state === "manual_matched" ||
          row.state === "automatic_matched" ||
          row.state === "settled"
        ) {
          payload[row.sourceName] = catalogBindingPayload(
            row.resolvedWorks[0].catalogRef,
            row,
            null,
            forLanding,
          );
        }
      }
      if (!payload[row.sourceName] && !forLanding) {
        payload[row.sourceName] = { mode: "automatic" };
      }
    });
    return payload;
  }

  function sourceWorkOverridesPayload(bindingPayload) {
    var legacy = cloneJson(state.sourceWorkOverrides || {});
    var bindingNames = Object.keys(bindingPayload || {});
    if (!bindingNames.length) return legacy;
    var blockedNames = Object.create(null);
    var blockedPaths = Object.create(null);
    bindingNames.forEach(function (sourceName) {
      blockedNames[String(sourceName).trim().toLocaleLowerCase()] = true;
      blockedPaths[pathKey(portableJoin(state.root, sourceName))] = true;
    });
    sourceWorkBindingRows(state.plan).forEach(function (row) {
      if (bindingNames.indexOf(row.sourceName) < 0) return;
      blockedPaths[pathKey(row.sourcePath)] = true;
    });
    Object.keys(legacy).forEach(function (source) {
      if (
        blockedNames[String(source).trim().toLocaleLowerCase()] ||
        blockedPaths[pathKey(source)]
      ) {
        delete legacy[source];
      }
    });
    return legacy;
  }

  function sourceBindingsNeedLanding() {
    var payload = sourceWorkBindingsPayload();
    if (Object.keys(payload).some(function (sourceName) {
      return (
        payload[sourceName].mode === "draft" ||
        (payload[sourceName].mode === "catalog" &&
          Array.isArray(payload[sourceName].presses) &&
          payload[sourceName].presses.length > 0)
      );
    })) return true;
    return sourceWorkBindingRows(state.plan).some(function (row) {
      return row.catalogRepairRequired && payload[row.sourceName] &&
        payload[row.sourceName].mode === "catalog";
    });
  }

  function sourceWorkLandingPayloadIssue(payload) {
    payload = payload && typeof payload === "object" ? payload : {};
    var rows = sourceWorkBindingRows(state.plan);
    var missing = rows.filter(function (row) {
      var binding = payload[row.sourceName];
      return !binding || (binding.mode !== "catalog" && binding.mode !== "draft");
    });
    if (missing.length) {
      return "以下一级目录尚未精确绑定已有 DB 作品或完整新作品信息：" +
        missing.map(function (row) { return row.sourceName; }).join("、");
    }
    var invalidPress = rows.filter(function (row) {
      var binding = payload[row.sourceName] || {};
      var presses = binding.mode === "draft"
        ? draftPresses(firstObject(binding.draft_work))
        : Array.isArray(binding.presses)
        ? binding.presses
        : [];
      if (presses.length !== 1) return true;
      var press = presses[0] || {};
      return !firstString(press.press_format) ||
        !firstString(press.press_group) ||
        !firstString(press.press_path);
    });
    if (invalidPress.length) {
      return "以下一级目录还需填写唯一的压制格式、压制组和 press_path：" +
        invalidPress.map(function (row) { return row.sourceName; }).join("、");
    }
    return "";
  }

  function sourceBindingDraftTemplate(row) {
    var registrationDraft = firstObject(row && row.registration && row.registration.draft);
    var existing = firstObject(row && row.work);
    var edit = sourceWorkBindingEdits()[row.sourceName] || {};
    var template = Object.keys(registrationDraft).length
      ? cloneJson(registrationDraft)
      : {
          name: firstString(
            edit.work_name,
            row.requestedWorkName,
            row.suggestedWorkName,
            existing.name,
          ),
          date: { start: "", end: "" },
          domain: firstString(existing.domain, "animation"),
          country: firstString(existing.country, "japan"),
          release_type: firstString(existing.release_type, "tv"),
          path: state.root,
          presses: [],
        };
    template.name = firstString(
      edit.work_name,
      template.name,
      row.suggestedWorkName,
      row.sourceName,
    );
    template.path = state.root;
    if (!template.date || typeof template.date !== "object") {
      template.date = { start: "", end: "" };
    }
    var presses = draftPresses(template);
    if (!presses.length) {
      template.presses = [
        {
          source_names: [row.sourceName],
          press_format: firstString(row.inferredPress.press_format),
          press_group: firstString(row.inferredPress.press_group),
          press_path: firstString(row.inferredPress.press_path),
        },
      ];
    } else {
      var inferredPress = firstObject(row.inferredPress);
      var selectedPress = presses.find(function (press) {
        return Array.isArray(press && press.source_names) &&
          press.source_names.map(String).indexOf(row.sourceName) >= 0;
      }) || presses.find(function (press) {
        return firstString(press && press.press_format).toLocaleLowerCase() ===
            firstString(inferredPress.press_format).toLocaleLowerCase() &&
          firstString(press && press.press_group).toLocaleLowerCase() ===
            firstString(inferredPress.press_group).toLocaleLowerCase();
      }) || presses.find(function (press) {
        return firstString(press && press.press_format).toLocaleLowerCase() ===
          firstString(inferredPress.press_format).toLocaleLowerCase();
      }) || presses[0];
      template.presses = [cloneJson(selectedPress)];
      template.presses[0].source_names = [row.sourceName];
    }
    return template;
  }

  function sourceBindingStateLabel(value) {
    var labels = {
      catalog_bound: "已绑定数据库",
      manual_matched: "手输名称已匹配",
      automatic_matched: "已自动匹配",
      automatic_multiple: "自动匹配到多条，需选择",
      automatic_pending: "正在自动识别",
      manual_unresolved: "手输名称未匹配",
      catalog_missing: "数据库未找到",
      selection_required: "需要选择",
      draft_ready: "新作品信息待落地",
      settled: "已按数据库目录落地",
      not_selected: "本次未选择",
      unresolved: "尚未识别",
      invalid: "绑定无效",
      unbound: "尚未绑定",
    };
    return labels[firstString(value)] || firstString(value, "尚未绑定");
  }

  function renderSourceShortcutDiagnostics(row) {
    var diagnostics = Array.isArray(row && row.diagnostics) ? row.diagnostics : [];
    if (!diagnostics.length) return "";
    return (
      '<aside class="organizer-source-shortcut-diagnostics"><strong>旧快捷方式诊断（不作为作品绑定依据）</strong><ul>' +
      diagnostics
        .map(function (diagnostic) {
          diagnostic = diagnostic && typeof diagnostic === "object" ? diagnostic : {};
          return (
            "<li><span>" +
            esc(firstString(diagnostic.state, diagnostic.status, "diagnostic")) +
            "</span><code>" +
            esc(firstString(diagnostic.shortcut_path, diagnostic.path, "-")) +
            "</code>" +
            (firstString(diagnostic.target_path, diagnostic.existing_target_path)
              ? "<small>→ " + esc(firstString(diagnostic.target_path, diagnostic.existing_target_path)) + "</small>"
              : "") +
            (firstString(diagnostic.message)
              ? "<p>" + esc(diagnostic.message) + "</p>"
              : "") +
            "</li>"
          );
        })
        .join("") +
      "</ul><p>错误或过期快捷方式只会显示为冲突/诊断；不会自动覆盖，也不会反向替你选择数据库作品。</p></aside>"
    );
  }

  function renderSourceWorkDraftEditor(row) {
    if (state.sourceWorkEditorSource !== row.sourceName) return "";
    var draft = sourceWorkDrafts()[row.sourceName];
    if (!draft) return "";
    if (state.sourceWorkEditorMode === "search") {
      return (
        '<div class="organizer-source-work-editor">' +
        renderLandingRecognition(draft) +
        '<button type="button" class="btn secondary sm" data-organizer-action="source-work-editor-close"' +
        (state.busy ? " disabled" : "") +
        ">关闭查找</button></div>"
      );
    }
    var date = firstObject(draft.date);
    var presses = draftPresses(draft);
    var disabled = state.busy || state.shortcutPending ? " disabled" : "";
    return (
      '<div class="organizer-source-work-editor"><div class="organizer-plan-head"><div><h4>手动填写完整作品信息</h4><p>只编辑当前一级来源；保存后仍需生成 DB + 归类 + 快捷方式完整预览。</p></div><span class="organizer-ready is-blocked">待预览</span></div>' +
      '<div class="organizer-registration-grid">' +
      '<label><span>作品名</span><input type="text" data-source-draft-field="name" data-source-name="' + esc(row.sourceName) + '" value="' + esc(draft.name || "") + '"' + disabled + " /></label>" +
      '<label><span>开始日期</span><input type="date" data-source-draft-date="start" data-source-name="' + esc(row.sourceName) + '" value="' + esc(date.start || "") + '"' + disabled + " /></label>" +
      '<label><span>结束日期（可空）</span><input type="date" data-source-draft-date="end" data-source-name="' + esc(row.sourceName) + '" value="' + esc(date.end || "") + '"' + disabled + " /></label>" +
      '<label><span>作品类型</span><select data-source-draft-field="domain" data-source-name="' + esc(row.sourceName) + '"' + disabled + '><option value="animation"' + (draft.domain === "animation" ? " selected" : "") + '>动画</option><option value="television"' + (draft.domain === "television" ? " selected" : "") + '>电视剧</option></select></label>' +
      '<label><span>国家</span><select data-source-draft-field="country" data-source-name="' + esc(row.sourceName) + '"' + disabled + '><option value="japan"' + (draft.country === "japan" ? " selected" : "") + '>日本</option><option value="korea"' + (draft.country === "korea" ? " selected" : "") + '>韩国</option><option value="china"' + (draft.country === "china" ? " selected" : "") + '>中国</option></select></label>' +
      '<label><span>发行类型</span><select data-source-draft-field="release_type" data-source-name="' + esc(row.sourceName) + '"' + disabled + '><option value="tv"' + (draft.release_type === "tv" ? " selected" : "") + '>TV</option><option value="ova"' + (draft.release_type === "ova" ? " selected" : "") + '>OVA</option><option value="movie"' + (draft.release_type === "movie" ? " selected" : "") + '>Movie</option></select></label>' +
      '<label class="organizer-registration-wide"><span>作品 path（当前根目录）</span><input type="text" value="' + esc(state.root) + '" readonly /></label></div>' +
      presses
        .map(function (press, pressIndex) {
          return (
            '<fieldset class="organizer-registration-press"><legend>当前来源压制</legend><p><code>' + esc(row.sourceName) + '</code></p><div class="organizer-registration-grid">' +
            '<label><span>压制格式</span><input type="text" data-source-draft-press="press_format" data-source-name="' + esc(row.sourceName) + '" data-press-index="' + esc(pressIndex) + '" value="' + esc(press.press_format || "") + '"' + disabled + " /></label>" +
            '<label><span>压制组</span><select data-source-draft-press="press_group" data-source-name="' + esc(row.sourceName) + '" data-press-index="' + esc(pressIndex) + '"' + disabled + ">" + renderGroupOptions(press.press_group) + "</select></label>" +
            '<label class="organizer-registration-wide"><span>press_path / 整理目标</span><input type="text" data-source-draft-press="press_path" data-source-name="' + esc(row.sourceName) + '" data-press-index="' + esc(pressIndex) + '" value="' + esc(press.press_path || "") + '"' + disabled + " /></label></div></fieldset>"
          );
        })
        .join("") +
      '<div class="organizer-pending-actions"><button type="button" class="btn secondary" data-organizer-action="source-work-editor-close"' + disabled + '>收起完整信息</button></div></div>'
    );
  }

  function renderSourceWorkBindings(plan) {
    var rows = sourceWorkBindingRows(plan);
    if (!rows.length) return "";
    var edits = sourceWorkBindingEdits();
    var disabled = state.busy || state.shortcutPending ? " disabled" : "";
    var cards = rows
      .map(function (row, index) {
        var edit = edits[row.sourceName] || {};
        var perFileMode = sourceUsesPerFileRouting(row);
        var rowDisabled = disabled || perFileMode ? " disabled" : "";
        var selectedCatalogRef = edit.mode === "catalog"
          ? nonEmptyObject(edit.catalog_ref)
          : nonEmptyObject(row.catalogRef);
        var selectedCandidateKey = selectedCatalogRef
          ? sourceWorkCandidateKey({
              name: firstString(selectedCatalogRef.work_name),
              catalogRef: selectedCatalogRef,
            })
          : "";
        var manualValue = edit.mode === "manual"
          ? firstString(edit.work_name)
          : edit.mode === "draft"
          ? firstString(edit.draft_work && edit.draft_work.name)
          : row.requestedMode === "manual"
          ? row.requestedWorkName
          : row.suggestedWorkName;
        var exactCandidates = row.candidates.filter(function (candidate) {
          return !!sourceWorkCandidateKey(candidate);
        });
        var duplicateNames = Object.create(null);
        exactCandidates.forEach(function (candidate) {
          var key = candidate.name.toLocaleLowerCase();
          duplicateNames[key] = (duplicateNames[key] || 0) + 1;
        });
        var catalogOptions = ['<option value="">不改用已有 DB 记录</option>']
          .concat(exactCandidates.map(function (candidate) {
            var candidateKey = sourceWorkCandidateKey(candidate);
            return (
              '<option value="' + esc(candidateKey) + '"' +
              (candidateKey === selectedCandidateKey ? " selected" : "") +
              ">" + esc(sourceWorkCandidateLabel(candidate, duplicateNames)) + "</option>"
            );
          }))
          .join("");
        var stateValue = firstString(row.state, "unbound");
        var needsRegistration = !!(
          !row.catalogPressRequired &&
          !row.catalogRepairRequired &&
          (stateValue === "catalog_missing" ||
          stateValue === "manual_unresolved" ||
          stateValue === "draft_ready" ||
          row.registrationRequired ||
          row.nextAction === "search" ||
          row.nextAction === "complete_manual")
        );
        var currentNames = row.resolvedWorks.map(function (candidate) {
          return candidate.name;
        });
        if (!currentNames.length && firstString(row.work && row.work.name)) {
          currentNames = [row.work.name];
        }
        var catalogPresses = sourceBindingCatalogPresses(row, edit);
        var catalogPressEditor = row.catalogPressRequired ||
          (edit.mode === "catalog" && catalogPresses.length)
          ? '<div class="organizer-source-work-press"><strong>已有作品、压制记录待补</strong><p>数据库作品已确定，但当前一级目录还没有对应压制行。核对格式、组和 press_path 后走完整落地预览。</p>' +
            catalogPresses.map(function (press, pressIndex) {
              return (
                '<div class="organizer-source-work-press-grid">' +
                '<label><span>压制格式</span><input type="text" data-source-catalog-press="press_format" data-source-name="' + esc(row.sourceName) + '" data-press-index="' + esc(pressIndex) + '" value="' + esc(press.press_format || "") + '"' + rowDisabled + ' /></label>' +
                '<label><span>压制组</span><select data-source-catalog-press="press_group" data-source-name="' + esc(row.sourceName) + '" data-press-index="' + esc(pressIndex) + '"' + rowDisabled + '>' + renderGroupOptions(press.press_group) + '</select></label>' +
                '<label class="organizer-registration-wide"><span>press_path / 整理目标</span><input type="text" data-source-catalog-press="press_path" data-source-name="' + esc(row.sourceName) + '" data-press-index="' + esc(pressIndex) + '" value="' + esc(press.press_path || "") + '"' + rowDisabled + ' /></label>' +
                '</div>'
              );
            }).join("") +
            '</div>'
          : "";
        return (
          '<article class="organizer-source-work-binding" data-source-work-card="' + esc(row.sourceName) + '"><header><div><strong>一级压制目录 ' + esc(index + 1) + '</strong><code>' + esc(row.sourceName) + '</code></div><span class="organizer-source-binding-state is-' + esc(stateValue) + '">' + esc(sourceBindingStateLabel(stateValue)) + "</span></header>" +
          (perFileMode
            ? '<p class="organizer-source-work-warning">当前目录已启用“多作品逐文件分流”；目录级 DB 绑定已清除并暂停。请在下方逐文件选择，或先恢复为单作品目录。</p>'
            : "") +
          '<div class="organizer-source-work-control">' +
          (row.suggestedWorkName
            ? '<p class="organizer-source-work-derived">目录名推导作品：<strong>' + esc(row.suggestedWorkName) + '</strong>' + (row.resolvedWorks.length ? '（已精确匹配 DB）' : '（未匹配 DB，需确认或补录）') + '</p>'
            : "") +
          '<label><span>选择已有 DB 作品（精确记录）</span><select data-source-work-catalog="' + esc(row.sourceName) + '"' + rowDisabled + '>' + catalogOptions + '</select></label>' +
          '<label><span>或自由输入作品名</span><input type="text" data-source-work-manual="' + esc(row.sourceName) + '" value="' + esc(manualValue) + '" placeholder="输入后按 Enter 或移开焦点" autocomplete="off" spellcheck="false"' + rowDisabled + ' /></label>' +
          '<div class="organizer-source-work-current"><span>当前自动/已确认：' + esc(currentNames.length ? currentNames.join("、") : "未绑定") + '</span>' +
          (selectedCatalogRef ? '<code>' + esc(catalogRefKey(selectedCatalogRef)) + '</code>' : '') +
          "</div></div>" +
          (stateValue === "automatic_multiple" || stateValue === "selection_required"
            ? '<p class="organizer-source-work-warning">当前来源匹配到多条记录，请在“已有 DB 作品”中明确选择；不会按显示名称猜测。</p>'
            : "") +
          catalogPressEditor +
          (row.catalogRepairRequired && !row.catalogPressRequired
            ? '<div class="organizer-source-work-next"><strong>已有作品、数据库路径待修复</strong><p>作品记录已经精确确认，但 path 尚未指向当前作品根目录；必须走完整落地预览，不能直接应用普通移动计划。</p></div>'
            : "") +
          (needsRegistration
            ? '<div class="organizer-source-work-next"><strong>手输名称尚未在数据库中找到</strong><p>请选择“查找作品信息”获取可确认候选，或“手动填写完整信息”；在完整落地预览前不会写数据库、移动文件或创建快捷方式。</p><div><button type="button" class="btn secondary sm" data-organizer-action="source-work-search" data-source-name="' + esc(row.sourceName) + '"' + rowDisabled + '>查找作品信息</button><button type="button" class="btn secondary sm" data-organizer-action="source-work-manual" data-source-name="' + esc(row.sourceName) + '"' + rowDisabled + '>手动填写完整信息</button></div></div>'
            : "") +
          renderSourceShortcutDiagnostics(row) +
          renderSourceWorkDraftEditor(row) +
          "</article>"
        );
      })
      .join("");
    return (
      '<section class="organizer-section organizer-source-work-bindings"><div class="organizer-plan-head"><div><h2>一级压制目录作品绑定</h2><p>每个被扫描的一级目录均可单独核对。已有、缺失和错误旧快捷方式不会改变这里的选择。</p></div><span>' + esc(rows.length) + " 个来源</span></div>" +
      '<div class="organizer-source-work-list">' + cards + "</div>" +
      (sourceBindingsNeedLanding()
        ? '<div class="organizer-summary-actions"><button type="button" class="btn" data-organizer-action="landing-preview"' + (state.busy || state.landing ? " disabled" : "") + '>生成“DB → 归类 → 快捷方式”完整预览</button><span>包含手动补录作品；必须重新预览后才能执行。</span></div>'
        : "") +
      "</section>"
    );
  }

  function sourceWorkBindingRow(sourceName) {
    return sourceWorkBindingRows(state.plan).find(function (row) {
      return row.sourceName === String(sourceName || "");
    }) || null;
  }

  function sourceBindingEdited(message) {
    state.landing = null;
    state.landingSourceWorkBindings = null;
    invalidatePreview(message || "来源目录的作品绑定已编辑，请重新预览。");
  }

  function setSourceWorkBinding(sourceName, workName) {
    sourceName = firstString(sourceName);
    workName = firstString(workName);
    var row = sourceWorkBindingRow(sourceName);
    if (!sourceName || !row) return false;
    var edits = sourceWorkBindingEdits();
    if (!workName) {
      delete edits[sourceName];
      delete sourceWorkDrafts()[sourceName];
      if (row.sourcePath) delete state.sourceWorkOverrides[row.sourcePath];
      sourceBindingEdited("已清除该一级目录的人工作品绑定，请重新预览。");
      previewPlan();
      return true;
    }
    edits[sourceName] = { mode: "manual", work_name: workName };
    delete sourceWorkDrafts()[sourceName];
    if (row.sourcePath) delete state.sourceWorkOverrides[row.sourcePath];
    if (state.sourceWorkEditorSource === sourceName) {
      state.sourceWorkEditorSource = "";
      state.sourceWorkEditorMode = "";
      invalidateLandingRecognition("", "");
    }
    sourceBindingEdited(
      "已按手输作品名查询数据库，正在重新预览；即使名称与已有记录相同，也会由服务端要求精确确认。",
    );
    previewPlan();
    return true;
  }

  function selectSourceCatalogBinding(sourceName, candidateKey) {
    sourceName = firstString(sourceName);
    candidateKey = firstString(candidateKey);
    var row = sourceWorkBindingRow(sourceName);
    if (!sourceName || !row) return false;
    var edits = sourceWorkBindingEdits();
    if (!candidateKey) {
      delete edits[sourceName];
      delete sourceWorkDrafts()[sourceName];
      if (row.sourcePath) delete state.sourceWorkOverrides[row.sourcePath];
      sourceBindingEdited("已取消当前一级目录的精确 DB 覆盖，请重新预览自动识别结果。");
      previewPlan();
      return true;
    }
    var candidate = row.candidates.find(function (item) {
      return sourceWorkCandidateKey(item) === candidateKey;
    });
    if (!candidate || !nonEmptyObject(candidate.catalogRef)) return false;
    var edit = {
      mode: "catalog",
      catalog_ref: cloneJson(candidate.catalogRef),
      work_name: candidate.name,
    };
    if (row.catalogPressRequired) {
      edit.presses = sourceBindingPressTemplate(
        sourceBindingRowForCatalogRef(row, candidate.catalogRef),
      );
    }
    edits[sourceName] = edit;
    delete sourceWorkDrafts()[sourceName];
    if (row.sourcePath) delete state.sourceWorkOverrides[row.sourcePath];
    if (state.sourceWorkEditorSource === sourceName) {
      state.sourceWorkEditorSource = "";
      state.sourceWorkEditorMode = "";
      invalidateLandingRecognition("", "");
    }
    sourceBindingEdited("已按精确 catalog_ref 选择数据库作品，正在重新预览该来源绑定。");
    previewPlan();
    return true;
  }

  function updateSourceCatalogPress(sourceName, field, value, pressIndex) {
    sourceName = firstString(sourceName);
    var row = sourceWorkBindingRow(sourceName);
    if (!row || ["press_format", "press_group", "press_path"].indexOf(field) < 0) {
      return false;
    }
    pressIndex = Number(pressIndex);
    var edits = sourceWorkBindingEdits();
    var previous = edits[sourceName] || {};
    var catalogRef = nonEmptyObject(previous.catalog_ref) || nonEmptyObject(row.catalogRef);
    if (!catalogRef) return false;
    var catalogRow = sourceBindingRowForCatalogRef(row, catalogRef);
    var presses = sourceBindingCatalogPresses(catalogRow, previous);
    if (!Number.isInteger(pressIndex) || !presses[pressIndex]) return false;
    presses[pressIndex][field] = field === "press_group"
      ? String(value || "").trim()
      : value;
    edits[sourceName] = {
      mode: "catalog",
      catalog_ref: cloneJson(catalogRef),
      work_name: firstString(catalogRef.work_name, row.work && row.work.name),
      presses: presses,
    };
    if (row.sourcePath) delete state.sourceWorkOverrides[row.sourcePath];
    sourceBindingEdited("当前来源的压制记录已编辑，请重新生成完整落地预览。");
    return true;
  }

  function openSourceWorkEditor(sourceName, mode) {
    var row = sourceWorkBindingRow(sourceName);
    if (!row) return false;
    var drafts = sourceWorkDrafts();
    if (!drafts[row.sourceName]) drafts[row.sourceName] = sourceBindingDraftTemplate(row);
    state.sourceWorkEditorSource = row.sourceName;
    state.sourceWorkEditorMode = mode === "search" ? "search" : "manual";
    if (state.sourceWorkEditorMode === "manual") {
      sourceWorkBindingEdits()[row.sourceName] = {
        mode: "draft",
        draft_work: cloneJson(drafts[row.sourceName]),
      };
      sourceBindingEdited("请填写当前来源的完整作品信息，再生成完整落地预览。");
    } else {
      state.recognition = createRecognitionState(
        firstString(drafts[row.sourceName].name, row.requestedWorkName, row.sourceName),
      );
      state.notice = "已打开当前一级来源的作品信息查找；候选仍需手动确认。";
      state.noticeError = false;
    }
    render();
    return true;
  }

  function closeSourceWorkEditor() {
    state.sourceWorkEditorSource = "";
    state.sourceWorkEditorMode = "";
    invalidateLandingRecognition("", "");
    render();
  }

  function updateSourceWorkDraft(sourceName, field, value, section, pressIndex) {
    sourceName = firstString(sourceName);
    var draft = sourceWorkDrafts()[sourceName];
    if (!draft) return false;
    if (section === "date") {
      if (!draft.date || typeof draft.date !== "object") draft.date = {};
      draft.date[field] = value;
    } else if (section === "press") {
      var presses = draftPresses(draft);
      if (!Number.isInteger(pressIndex) || !presses[pressIndex]) return false;
      presses[pressIndex][field] = field === "press_group"
        ? String(value || "").trim()
        : value;
    } else {
      draft[field] = value;
    }
    draft.path = state.root;
    sourceWorkBindingEdits()[sourceName] = {
      mode: "draft",
      draft_work: cloneJson(draft),
    };
    var row = sourceWorkBindingRow(sourceName);
    if (row && row.sourcePath) delete state.sourceWorkOverrides[row.sourcePath];
    if (state.sourceWorkEditorMode === "search") {
      invalidateLandingRecognition(
        firstString(draft.name),
        "作品信息已变化，旧查找候选已作废；请重新查找。",
      );
    }
    sourceBindingEdited("当前来源的完整作品信息已编辑，请重新生成完整落地预览。");
    return true;
  }

  function renderUnresolvedFileChoice(item, index, disabled) {
    var source = firstString(item.source);
    var selected = Object.prototype.hasOwnProperty.call(state.fileWorkOverrides, source)
      ? String(state.fileWorkOverrides[source])
      : "";
    var candidates = Array.isArray(item.candidates) ? item.candidates : [];
    var workNameHints = [];
    [item.work_name_hints, item.unmatched_work_name_hints].forEach(function (values) {
      if (!Array.isArray(values)) return;
      values.forEach(function (value) {
        var hint = firstString(value);
        if (hint && workNameHints.indexOf(hint) < 0) workNameHints.push(hint);
      });
    });
    var options = ['<option value="">请选择作品</option>']
      .concat(
        candidates.map(function (workName) {
          var value = String(workName || "");
          return (
            '<option value="' +
            esc(value) +
            '"' +
            (value === selected ? " selected" : "") +
            ">" +
            esc(value) +
            "</option>"
          );
        }),
      )
      .join("");
    return (
      '<article class="organizer-unresolved-item">' +
      "<div><strong>未决文件 " +
      esc(index + 1) +
      "</strong><code>" +
      esc(firstString(item.source_relpath, source)) +
      "</code><p>" +
      esc(item.reason || "无法自动确定所属作品") +
      "</p>" +
      (workNameHints.length
        ? '<p class="organizer-unresolved-hints"><strong>括号作品候选：</strong>' +
          esc(workNameHints.join("、")) +
          "</p>"
        : "") +
      "</div>" +
      '<label><span>人工选择作品</span><select data-file-source="' +
      esc(source) +
      '"' +
      disabled +
      (candidates.length ? "" : " disabled") +
      ">" +
      options +
      "</select></label></article>"
    );
  }

  function renderUnresolvedFiles(plan) {
    var unresolved = plan && Array.isArray(plan.unresolved_files)
      ? plan.unresolved_files
      : [];
    if (!unresolved.length) {
      return '<p class="organizer-ok">没有待人工选择作品的文件。</p>';
    }
    var hasSourceBindingRows = !!(
      plan && Array.isArray(plan.source_work_bindings) && plan.source_work_bindings.length
    );
    var disabled = state.busy ? " disabled" : "";
    var groups = [];
    var groupsBySource = Object.create(null);
    unresolved.forEach(function (item) {
      var sourceDir = firstString(item.source_dir, item.source);
      if (!groupsBySource[sourceDir]) {
        groupsBySource[sourceDir] = {
          sourceDir: sourceDir,
          items: [],
          candidates: [],
          missingHints: [],
        };
        groups.push(groupsBySource[sourceDir]);
      }
      var group = groupsBySource[sourceDir];
      group.items.push(item);
      (Array.isArray(item.candidates) ? item.candidates : []).forEach(function (workName) {
        var value = firstString(workName);
        if (value && group.candidates.indexOf(value) < 0) group.candidates.push(value);
      });
      var unmatchedHints = Array.isArray(item.unmatched_work_name_hints)
        ? item.unmatched_work_name_hints
        : [];
      unmatchedHints.forEach(function (hint) {
        var value = firstString(hint);
        if (value && group.missingHints.indexOf(value) < 0) {
          group.missingHints.push(value);
        }
      });
    });
    return (
      '<div class="organizer-unresolved-list">' +
      groups
        .map(function (group) {
          var sourceDir = group.sourceDir;
          var multiple = !!state.multipleWorkSources[sourceDir];
          var selected = Object.prototype.hasOwnProperty.call(
            state.sourceWorkOverrides,
            sourceDir,
          )
            ? String(state.sourceWorkOverrides[sourceDir])
            : "";
          var overrideValues = group.items
            .map(function (item) {
              var source = firstString(item.source);
              return Object.prototype.hasOwnProperty.call(state.fileWorkOverrides, source)
                ? String(state.fileWorkOverrides[source])
                : "";
            })
            .filter(Boolean);
          if (
            !selected &&
            overrideValues.length === group.items.length &&
            overrideValues.every(function (value) {
              return value === overrideValues[0];
            })
          ) {
            selected = overrideValues[0];
          }
          var options = ['<option value="">请先确认该目录是单作品还是多作品</option>']
            .concat(
              group.candidates.map(function (workName) {
                return (
                  '<option value="' +
                  esc(workName) +
                  '"' +
                  (!multiple && workName === selected ? " selected" : "") +
                  ">整个目录归为：" +
                  esc(workName) +
                  "</option>"
                );
              }),
            )
            .concat([
              '<option value="' +
                MULTIPLE_WORKS_VALUE +
                '"' +
                (multiple ? " selected" : "") +
                ">确认该目录包含多个作品</option>",
            ])
            .join("");
          var multipleFiles =
            '<div class="organizer-unresolved-source-files">' +
            (group.missingHints.length
              ? hasSourceBindingRows
                ? '<aside class="organizer-unresolved-catalog-guide"><strong>存在尚未入库的作品候选：</strong>' +
                  esc(group.missingHints.join("、")) +
                  '<p>若这些候选确实是独立作品，请先使用上方对应一级目录的“自由输入 / 查找作品信息 / 手动填写完整信息”。未完成数据库落地前不会移动文件。</p></aside>'
                : '<aside class="organizer-unresolved-catalog-guide"><strong>存在尚未入库的作品候选：</strong>' +
                  esc(group.missingHints.join("、")) +
                  '<p>若这些候选确实是独立作品，请先在“作品数据”补录作品记录（path 使用当前作品根目录），保存后返回重新预览；未补录前不会移动文件。</p>' +
                  '<button type="button" class="btn secondary sm" data-organizer-action="unresolved-catalog-guide" data-source-work-dir="' +
                  esc(sourceDir) + '"' + disabled + '>前往作品数据补录</button></aside>'
              : "") +
            group.items
              .map(function (item, index) {
                return renderUnresolvedFileChoice(item, index, disabled);
              })
              .join("") +
            "</div>";
          if (hasSourceBindingRows) {
            return (
              '<article class="organizer-unresolved-source">' +
              '<div class="organizer-unresolved-source-head"><div><strong>逐文件分流（仅多作品目录）</strong><code>' +
              esc(sourceDir) +
              "</code><p>单作品绑定统一使用上方一级压制目录卡片；只有确认本目录确实混有多个作品时，才展开逐文件选择。</p></div>" +
              '<button type="button" class="btn secondary sm" data-organizer-action="' +
              (multiple ? "source-work-multiple-disable" : "source-work-multiple-enable") +
              '" data-source-work-dir="' + esc(sourceDir) + '"' + disabled + ">" +
              (multiple ? "恢复为单作品目录" : "确认目录包含多个作品") +
              "</button></div>" +
              (multiple ? multipleFiles : "") +
              "</article>"
            );
          }
          return (
            '<article class="organizer-unresolved-source">' +
            '<div class="organizer-unresolved-source-head"><div><strong>未决来源目录</strong><code>' +
            esc(sourceDir) +
            "</code><p>该目录有 " +
            esc(group.items.length) +
            " 个文件尚未确定作品。请先做一次目录级判断。</p></div>" +
            '<label><span>目录作品结构</span><select data-source-work-dir="' +
            esc(sourceDir) +
            '"' +
            disabled +
            ">" +
            options +
            "</select></label></div>" +
            (multiple
              ? multipleFiles
              : '<p class="organizer-unresolved-hints">选择单个作品会由服务端一次性应用到该来源目录的全部文件；只有确认多作品后才需要逐文件选择。</p>') +
            "</article>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function renderIssues(plan) {
    var issues = visiblePlanIssues(plan);
    if (!issues.length) {
      return '<p class="organizer-ok">没有发现阻止执行的问题。</p>';
    }
    return (
      '<ul class="organizer-issue-list">' +
      issues
        .map(function (issue) {
          var source = firstString(issue.source_key, issue.path);
          var pressFormat = firstString(issue.press_format);
          var pressFormats = Array.isArray(issue.press_formats)
            ? issue.press_formats
                .map(function (value) {
                  return firstString(value).trim();
                })
                .filter(function (value, index, values) {
                  return value && values.indexOf(value) === index;
                })
            : [];
          var detectedPressFormats = Array.isArray(issue.detected_press_formats)
            ? issue.detected_press_formats
                .map(function (value) {
                  return firstString(value).trim();
                })
                .filter(function (value, index, values) {
                  return value && values.indexOf(value) === index;
                })
            : [];
          var pressGroups = Array.isArray(issue.press_groups)
            ? issue.press_groups
                .map(function (value) {
                  return firstString(value).trim();
                })
                .filter(function (value, index, values) {
                  return value && values.indexOf(value) === index;
                })
            : [];
          var selectedOverride = source && state.sourcePressOverrides[source];
          var selectedFormat = selectedOverride
            ? firstString(selectedOverride.press_format)
            : "";
          var selectedGroup = selectedOverride
            ? firstString(selectedOverride.press_group)
            : "";
          var formatControl = "";
          if (issue.code === "format-ambiguous" && source && pressFormats.length) {
            var formatOptions = ['<option value="">请选择数据库压制格式</option>']
              .concat(
                pressFormats.map(function (candidateFormat) {
                  return (
                    '<option value="' +
                    esc(candidateFormat) +
                    '"' +
                    (candidateFormat === selectedFormat ? " selected" : "") +
                    ">" +
                    esc(candidateFormat) +
                    "</option>"
                  );
                }),
              )
              .join("");
            formatControl =
              '<label class="organizer-issue-resolution"><span>人工选择数据库压制格式</span><select data-source-press-path="' +
              esc(source) +
              '" data-source-press-field="press_format"' +
              (state.busy ? " disabled" : "") +
              ">" +
              formatOptions +
              "</select><small>" +
              (detectedPressFormats.length
                ? "文件名检测到：" + esc(detectedPressFormats.join("、")) + "；"
                : "") +
              "仅列出数据库中可用的候选格式；选择后会自动重新预览，不会移动文件。</small></label>";
          }
          var groupControl = "";
          if (
            (issue.code === "group-unresolved" || issue.code === "group-ambiguous") &&
            source &&
            pressGroups.length
          ) {
            var options = ['<option value="">请选择数据库压制组</option>']
              .concat(
                pressGroups.map(function (pressGroup) {
                  return (
                    '<option value="' +
                    esc(pressGroup) +
                    '"' +
                    (pressGroup === selectedGroup ? " selected" : "") +
                    ">" +
                    esc(pressGroup) +
                    "</option>"
                  );
                }),
              )
              .join("");
            groupControl =
              '<label class="organizer-issue-resolution"><span>人工选择数据库压制组' +
              (pressFormat ? "（" + esc(pressFormat) + "）" : "") +
              '</span><select data-source-press-path="' +
              esc(source) +
              '" data-source-press-field="press_group"' +
              (state.busy ? " disabled" : "") +
              ">" +
              options +
              "</select><small>仅列出该作品数据库中可用的候选组；选择后会自动重新预览，不会移动文件。</small></label>";
          }
          return (
            '<li class="organizer-issue-item"><strong>[' +
            esc(issue.code || "issue") +
            "]</strong> " +
            esc(issue.message || "未知问题") +
            (issue.path ? "<code>" + esc(issue.path) + "</code>" : "") +
            formatControl +
            groupControl +
            "</li>"
          );
        })
        .join("") +
      "</ul>"
    );
  }

  function cloneJson(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function recognitionState() {
    if (!state.recognition || typeof state.recognition !== "object") {
      state.recognition = createRecognitionState("");
    }
    return state.recognition;
  }

  function recognitionHasTransientState(recognition) {
    recognition = recognition || recognitionState();
    return !!(
      recognition.loading ||
      recognition.error ||
      recognition.results ||
      recognition.inputFingerprint
    );
  }

  function draftPresses(work) {
    if (!work || typeof work !== "object") return [];
    if (Array.isArray(work.presses)) return work.presses;
    return Array.isArray(work.collectioned_ordered) ? work.collectioned_ordered : [];
  }

  function bangumiRecognitionEligible(work) {
    return !!(
      work &&
      String(work.domain || "").trim().toLowerCase() === "animation" &&
      String(work.country || "").trim().toLowerCase() === "japan"
    );
  }

  function landingRecognitionDraft() {
    var sourceName = firstString(state.sourceWorkEditorSource);
    if (sourceName && sourceWorkDrafts()[sourceName]) {
      return sourceWorkDrafts()[sourceName];
    }
    return state.draftWork;
  }

  function landingRecognitionPayload(query) {
    var root = String(state.root || "").trim();
    var draft = cloneJson(landingRecognitionDraft());
    if (draft && typeof draft === "object") draft.path = root;
    return {
      root: root,
      draft_work: draft,
      query: String(query == null ? recognitionState().query : query).trim(),
      include_bangumi: bangumiRecognitionEligible(draft),
    };
  }

  function landingRecognitionSnapshot(payload) {
    return JSON.stringify(payload || landingRecognitionPayload());
  }

  function invalidateLandingRecognition(query, message) {
    recognitionSerial += 1;
    var current = recognitionState();
    var nextQuery = query == null ? current.query : String(query);
    state.recognition = createRecognitionState(nextQuery);
    state.recognition.notice = String(message || "");
    var target = view();
    if (!target) return;
    var results = target.querySelector("[data-landing-recognition-results]");
    if (results && message) {
      results.innerHTML =
        '<p class="organizer-recognition-status is-stale">' + esc(message) + "</p>";
    }
    target.querySelectorAll('[data-organizer-action="landing-recognition-apply"]').forEach(function (button) {
      button.disabled = true;
    });
  }

  function textItems(value) {
    var values = Array.isArray(value) ? value : value == null || value === "" ? [] : [value];
    return values
      .map(function (item) {
        return structuredText(item);
      })
      .filter(Boolean);
  }

  function safeBangumiSubjectUrl(value) {
    var raw = String(value || "").trim();
    return /^https:\/\/bangumi\.tv\/subject\/\d+$/.test(raw) ? raw : "";
  }

  function safeBangumiSearchUrl(value) {
    var raw = String(value || "").trim();
    return /^https:\/\/bangumi\.tv\/subject_search\/[^?#]+(?:\?cat=2)?$/.test(raw)
      ? raw
      : "";
  }

  function formatRecognitionConfidence(value) {
    if (value == null || value === "") return "未评分";
    var numeric = Number(value);
    if (!Number.isFinite(numeric)) return String(value);
    if (numeric >= 0 && numeric <= 1) numeric *= 100;
    numeric = Math.max(0, Math.min(100, numeric));
    return Math.round(numeric) + "%";
  }

  function candidateIsBangumi(candidate) {
    return String((candidate && candidate.source) || "")
      .trim()
      .toLowerCase()
      .indexOf("bangumi") >= 0;
  }

  function pressSourceIdentity(press) {
    var names = Array.isArray(press && press.source_names)
      ? press.source_names
      : press && press.source_name
      ? [press.source_name]
      : [];
    return names
      .map(function (name) {
        return String(name || "").trim().toLowerCase();
      })
      .filter(Boolean)
      .sort()
      .join("\u0000");
  }

  function currentPressForCandidate(currentPresses, proposedPress, index) {
    var identity = pressSourceIdentity(proposedPress);
    if (identity) {
      for (var i = 0; i < currentPresses.length; i++) {
        if (pressSourceIdentity(currentPresses[i]) === identity) return currentPresses[i];
      }
    }
    return currentPresses[index] || null;
  }

  function renderRecognitionPressChanges(candidate, currentWork) {
    var proposedWork = candidate && candidate.proposed_work;
    var proposedPresses = draftPresses(proposedWork);
    if (!proposedPresses.length) {
      return '<p class="organizer-recognition-status is-warning">该候选没有可核对的 press_path 建议。</p>';
    }
    var currentPresses = draftPresses(currentWork);
    var rows = proposedPresses
      .map(function (press, index) {
        var current = currentPressForCandidate(currentPresses, press, index) || {};
        var sourceNames = Array.isArray(press && press.source_names)
          ? press.source_names
          : press && press.source_name
          ? [press.source_name]
          : [];
        var currentPath = String(current.press_path || "");
        var proposedPath = String((press && press.press_path) || "");
        var changed = pathKey(currentPath) !== pathKey(proposedPath);
        return (
          "<tr><td><code>" +
          esc(sourceNames.join(" / ") || "来源 " + (index + 1)) +
          "</code></td><td>" +
          esc([press && press.press_format, press && press.press_group].filter(Boolean).join(" / ") || "-") +
          "</td><td><code>" +
          esc(currentPath || "（空）") +
          "</code></td><td" +
          (changed ? ' class="is-changed"' : "") +
          "><code>" +
          esc(proposedPath || "（空）") +
          "</code></td></tr>"
        );
      })
      .join("");
    return (
      '<div class="organizer-recognition-table-wrap"><table class="organizer-recognition-table">' +
      "<thead><tr><th>来源</th><th>格式 / 组</th><th>当前 press_path</th><th>建议 press_path</th></tr></thead>" +
      "<tbody>" +
      rows +
      "</tbody></table></div>"
    );
  }

  function renderRecognitionCandidate(candidate, index, currentWork) {
    candidate = candidate && typeof candidate === "object" ? candidate : {};
    var proposed = candidate.proposed_work && typeof candidate.proposed_work === "object"
      ? candidate.proposed_work
      : null;
    var date = proposed && proposed.date && typeof proposed.date === "object" ? proposed.date : {};
    var reasons = textItems(candidate.reasons);
    var warnings = textItems(candidate.warnings);
    var fingerprint = String(candidate.input_fingerprint || "");
    var recognition = recognitionState();
    var responseFingerprint = String(recognition.inputFingerprint || "");
    var candidateId = String(candidate.candidate_id || "");
    var snapshotMatches = !!(
      recognition.requestSnapshot &&
      recognition.requestSnapshot ===
        landingRecognitionSnapshot(landingRecognitionPayload(recognition.query))
    );
    var canApply = !!(
      candidateId &&
      proposed &&
      fingerprint &&
      responseFingerprint &&
      fingerprint === responseFingerprint &&
      snapshotMatches &&
      !recognition.loading &&
      !state.busy
    );
    var bangumi = candidateIsBangumi(candidate);
    var subjectUrl = bangumi ? safeBangumiSubjectUrl(candidate.subject_url) : "";
    var candidateName = firstString(
      proposed && proposed.name,
      candidate.name_cn,
      candidate.name,
      "未命名候选",
    );
    var originalName = firstString(candidate.name);
    var chineseName = firstString(candidate.name_cn);
    var summary = firstString(candidate.summary);
    if (summary.length > 700) summary = summary.slice(0, 700) + "…";
    var facts = [
      '<div><dt>建议作品名</dt><dd>' + esc(candidateName) + "</dd></div>",
      '<div><dt>日期</dt><dd>' +
        esc(firstString(date.start, candidate.start_date, "-") + " → " + firstString(date.end, candidate.end_date, "-")) +
        "</dd></div>",
      '<div><dt>类型</dt><dd>' +
        esc(
          [proposed && proposed.domain, proposed && proposed.country, proposed && proposed.release_type]
            .filter(Boolean)
            .join(" / ") || "-",
        ) +
        "</dd></div>",
    ];
    if (bangumi && (originalName || chineseName)) {
      facts.push(
        '<div><dt>Bangumi 名称</dt><dd>' +
          esc([originalName, chineseName].filter(Boolean).join(" / ")) +
          "</dd></div>",
      );
    }
    if (bangumi && candidate.total_episodes != null && candidate.total_episodes !== "") {
      facts.push('<div><dt>总集数</dt><dd>' + esc(candidate.total_episodes) + "</dd></div>");
    }
    if (bangumi && firstString(candidate.platform)) {
      facts.push('<div><dt>平台</dt><dd>' + esc(candidate.platform) + "</dd></div>");
    }
    if (bangumi && (candidate.score != null || candidate.rank != null)) {
      facts.push(
        '<div><dt>评分 / 排名</dt><dd>' +
          esc(
            [
              candidate.score == null ? "" : candidate.score,
              candidate.rank == null ? "" : "#" + candidate.rank,
            ].filter(function (value) {
              return value !== "";
            }).join(" / "),
          ) +
          "</dd></div>",
      );
    }
    return (
      '<article class="organizer-recognition-candidate">' +
      '<header><div><span class="organizer-recognition-source">' +
      esc(bangumi ? "Bangumi" : firstString(candidate.source, "本地识别")) +
      "</span><h4>" +
      esc(candidateName) +
      '</h4></div><strong class="organizer-recognition-confidence">匹配度 ' +
      esc(formatRecognitionConfidence(candidate.confidence)) +
      "</strong></header>" +
      '<dl class="organizer-recognition-facts">' +
      facts.join("") +
      "</dl>" +
      (reasons.length
        ? '<div class="organizer-recognition-reasons"><strong>匹配依据</strong><ul>' +
          reasons.map(function (reason) {
            return "<li>" + esc(reason) + "</li>";
          }).join("") +
          "</ul></div>"
        : "") +
      renderRecognitionPressChanges(candidate, currentWork) +
      (summary
        ? '<details class="organizer-recognition-summary"><summary>Bangumi 简介</summary><p>' +
          esc(summary) +
          "</p></details>"
        : "") +
      (warnings.length
        ? '<ul class="organizer-recognition-warnings">' +
          warnings.map(function (warning) {
            return "<li>" + esc(warning) + "</li>";
          }).join("") +
          "</ul>"
        : "") +
      '<footer><span>' +
      (subjectUrl
        ? '<a href="' + esc(subjectUrl) + '" target="_blank" rel="noopener noreferrer">打开 Bangumi 条目</a>'
        : bangumi
        ? "未提供安全的 Bangumi 条目链接"
        : "采用后仍需重新生成完整落地预览") +
      '</span><button type="button" class="btn" data-organizer-action="landing-recognition-apply" data-recognition-candidate-id="' +
      esc(candidateId) +
      '"' +
      (canApply ? "" : " disabled") +
      ">填入表单（不会写入或移动）</button></footer>" +
      (!canApply
        ? '<p class="organizer-recognition-status is-warning">该候选缺少有效校验指纹或完整作品草稿，不能填入。</p>'
        : "") +
      "</article>"
    );
  }

  function renderRecognitionCandidateGroup(title, candidates, currentWork) {
    return (
      '<section class="organizer-recognition-group"><h3>' +
      esc(title) +
      "</h3>" +
      (candidates.length
        ? '<div class="organizer-recognition-candidates">' +
          candidates.map(function (candidate, index) {
            return renderRecognitionCandidate(candidate, index, currentWork);
          }).join("") +
          "</div>"
        : '<p class="organizer-recognition-status">没有返回候选。</p>') +
      "</section>"
    );
  }

  function renderLandingRecognition(work) {
    var recognition = recognitionState();
    var eligible = bangumiRecognitionEligible(work);
    var results = recognition.results && typeof recognition.results === "object"
      ? recognition.results
      : null;
    var candidates = results && Array.isArray(results.candidates) ? results.candidates : [];
    var localCandidates = candidates.filter(function (candidate) {
      return !candidateIsBangumi(candidate);
    });
    var bangumiCandidates = eligible
      ? candidates.filter(function (candidate) {
          return candidateIsBangumi(candidate);
        })
      : [];
    var warnings = results ? textItems(results.warnings) : [];
    var providerError = eligible && results ? structuredText(results.provider_error) : "";
    var searchUrl = eligible && results ? safeBangumiSearchUrl(results.search_url) : "";
    var disabled = state.busy || recognition.loading || !String(recognition.query || "").trim()
      ? " disabled"
      : "";
    var resultHtml = "";
    if (recognition.loading) {
      resultHtml = '<p class="organizer-recognition-status">正在分析目录和文件名' +
        (eligible ? "，并搜索 Bangumi" : "") +
        "…</p>";
    } else if (recognition.error) {
      resultHtml = '<p class="organizer-recognition-status is-error">' + esc(recognition.error) + "</p>";
    } else if (results) {
      resultHtml =
        (warnings.length
          ? '<ul class="organizer-recognition-warnings">' +
            warnings.map(function (warning) {
              return "<li>" + esc(warning) + "</li>";
            }).join("") +
            "</ul>"
          : "") +
        renderRecognitionCandidateGroup("本地智能候选", localCandidates, work) +
        (eligible
          ? '<section class="organizer-recognition-group"><div class="organizer-recognition-group-head"><h3>Bangumi 候选</h3>' +
            (searchUrl
              ? '<a href="' + esc(searchUrl) + '" target="_blank" rel="noopener noreferrer">查看搜索页</a>'
              : "") +
            "</div>" +
            (providerError
              ? '<p class="organizer-recognition-status is-warning">Bangumi 搜索暂不可用：' +
                esc(providerError) +
                "。本地候选仍可使用。</p>"
              : "") +
            (bangumiCandidates.length
              ? '<div class="organizer-recognition-candidates">' +
                bangumiCandidates.map(function (candidate, index) {
                  return renderRecognitionCandidate(candidate, index, work);
                }).join("") +
                "</div>"
              : '<p class="organizer-recognition-status">没有返回 Bangumi 候选。</p>') +
            "</section>"
          : "");
    } else if (recognition.notice) {
      resultHtml = '<p class="organizer-recognition-status is-stale">' + esc(recognition.notice) + "</p>";
    } else {
      resultHtml =
        '<p class="organizer-recognition-status">识别只会生成可选草稿，不会写数据库、移动文件或创建快捷方式。</p>';
    }
    return (
      '<section class="organizer-recognition">' +
      '<div class="organizer-recognition-head"><div><h3>智能识别作品与整理目标</h3><p>根据目录与文件名推导作品、格式、压制组和 press_path；候选必须由你手动填入。</p></div></div>' +
      '<div class="organizer-recognition-search"><label for="organizer-recognition-query">搜索词</label>' +
      '<input id="organizer-recognition-query" type="search" value="' +
      esc(recognition.query) +
      '" autocomplete="off" spellcheck="false" data-landing-recognition-query' +
      (state.busy || recognition.loading ? " disabled" : "") +
      ' /><button type="button" class="btn secondary" data-organizer-action="landing-recognition-run"' +
      disabled +
      ">" +
      (recognition.loading ? "识别中…" : "智能识别作品与整理目标") +
      "</button></div>" +
      '<div class="organizer-recognition-results" data-landing-recognition-results aria-live="polite">' +
      resultHtml +
      "</div></section>"
    );
  }

  function groupRegistryOptions() {
    var registry = state.config && state.config.group_registry;
    return registry && Array.isArray(registry.options) ? registry.options : [];
  }

  function renderGroupOptions(selected) {
    var current = String(selected || "");
    var options = ['<option value="">请选择组简称或组合简称</option>'];
    var currentFound = false;
    groupRegistryOptions().forEach(function (item) {
      var code = String((item && item.code) || "");
      if (!code) return;
      if (code === current) currentFound = true;
      options.push(
        '<option value="' +
          esc(code) +
          '"' +
          (code === current ? " selected" : "") +
          ">" +
          esc((item && item.label) || code) +
          "</option>",
      );
    });
    if (current && !currentFound) {
      options.push(
        '<option value="' + esc(current) + '" selected>' + esc(current) + "</option>",
      );
    }
    return options.join("");
  }

  function landingDrafts() {
    if (Array.isArray(state.draftWorks) && state.draftWorks.length) {
      return state.draftWorks;
    }
    return state.draftWork ? [state.draftWork] : [];
  }

  function landingDraftForInput(input) {
    var drafts = landingDrafts();
    var index = Number(input && input.getAttribute("data-work-index"));
    if (!Number.isInteger(index) || index < 0) index = 0;
    return drafts[index] || null;
  }

  function sharedTargetState() {
    if (!state.sharedTarget || typeof state.sharedTarget !== "object") {
      state.sharedTarget = createSharedTargetState();
    }
    if (!state.sharedTarget.selectedMembers) {
      state.sharedTarget.selectedMembers = Object.create(null);
    }
    if (!state.sharedTarget.pressPaths) {
      state.sharedTarget.pressPaths = Object.create(null);
    }
    if (!state.sharedTarget.sourceOwners) {
      state.sharedTarget.sourceOwners = Object.create(null);
    }
    return state.sharedTarget;
  }

  function catalogRefKey(catalogRef) {
    catalogRef = firstObject(catalogRef);
    var source = firstString(catalogRef.yaml_source_rel);
    var index = Number(catalogRef.index_in_file);
    if (!source || !Number.isInteger(index) || index < 0) return "";
    return source + "#" + index;
  }

  function sharedTargetGroupKey(pressFormat, pressGroup) {
    return JSON.stringify([
      String(pressFormat || "").trim().toLocaleLowerCase(),
      String(pressGroup || "").trim().toLocaleLowerCase(),
    ]);
  }

  function sharedTargetMemberKey(catalogRef, pressKey) {
    var workKey = catalogRefKey(catalogRef);
    var rowKey = firstString(pressKey);
    return workKey && rowKey ? JSON.stringify([workKey, rowKey]) : "";
  }

  function normalizedSharedTargetCandidate(rawCandidate, candidateIndex) {
    rawCandidate = rawCandidate && typeof rawCandidate === "object" ? rawCandidate : {};
    var original = firstObject(rawCandidate.raw, rawCandidate);
    var draft = firstObject(
      rawCandidate.draftWork,
      original.draft_work,
      original.draft,
    );
    var candidateRef = nonEmptyObject(rawCandidate.catalogRef) || nonEmptyObject(original.catalog_ref);
    var rawMembers = Array.isArray(rawCandidate.sharedTargetMembers)
      ? rawCandidate.sharedTargetMembers
      : Array.isArray(original.shared_target_members)
      ? original.shared_target_members
      : [];
    if (!rawMembers.length && candidateRef) {
      var draftPresses = Array.isArray(draft.presses)
        ? draft.presses
        : Array.isArray(draft.collectioned_ordered)
        ? draft.collectioned_ordered
        : [];
      rawMembers = draftPresses.map(function (press) {
        return Object.assign({ catalog_ref: candidateRef }, press || {});
      });
    }
    var members = rawMembers
      .map(function (rawMember) {
        rawMember = rawMember && typeof rawMember === "object" ? rawMember : {};
        var catalogRef = nonEmptyObject(rawMember.catalog_ref) || candidateRef;
        var pressKey = firstString(rawMember.press_key);
        var pressFormat = firstString(rawMember.press_format);
        var pressGroup = firstString(rawMember.press_group);
        var key = sharedTargetMemberKey(catalogRef, pressKey);
        if (!catalogRef || !key || !pressFormat || !pressGroup) return null;
        return {
          key: key,
          workKey: catalogRefKey(catalogRef),
          groupKey: sharedTargetGroupKey(pressFormat, pressGroup),
          catalogRef: cloneJson(catalogRef),
          workName: firstString(rawMember.work_name, draft.name, rawCandidate.label),
          pressKey: pressKey,
          pressFormat: pressFormat,
          pressGroup: pressGroup,
          pressPath: firstString(rawMember.press_path),
          sourceNames: Array.isArray(rawMember.source_names)
            ? rawMember.source_names
                .map(function (value) { return firstString(value); })
                .filter(function (value, index, values) {
                  return !!value && values.indexOf(value) === index;
                })
            : [],
        };
      })
      .filter(function (member) { return !!member; });
    var date = firstObject(draft.date);
    return {
      index: candidateIndex,
      workKey: catalogRefKey(candidateRef),
      catalogRef: candidateRef ? cloneJson(candidateRef) : null,
      label: firstString(rawCandidate.label, original.label, draft.name, "数据库候选 " + (candidateIndex + 1)),
      dateStart: firstString(date.start),
      dateEnd: firstString(date.end),
      members: members,
    };
  }

  function sharedTargetCandidateBundle(plan, allowSnapshot) {
    plan = plan && typeof plan === "object" ? plan : {};
    var registration = firstObject(plan.registration);
    var rawCandidates = Array.isArray(registration.existing_candidates)
      ? registration.existing_candidates
      : [];
    var supported = registration.shared_target_supported === true;
    if (!rawCandidates.length) {
      var repairCandidates = shortcutRepairDetails(plan).candidates;
      if (repairCandidates.length > 1) {
        rawCandidates = repairCandidates;
        supported = true;
      }
    }
    if (!rawCandidates.length && allowSnapshot !== false) {
      var snapshot = sharedTargetState().candidateSnapshot;
      if (snapshot && typeof snapshot === "object") return snapshot;
    }
    var candidates = rawCandidates
      .map(normalizedSharedTargetCandidate)
      .filter(function (candidate) { return candidate.members.length > 0; });
    var members = [];
    candidates.forEach(function (candidate) {
      candidate.members.forEach(function (member) { members.push(member); });
    });
    var sources = [];
    function addSource(value) {
      value = firstString(value);
      if (value && sources.indexOf(value) < 0) sources.push(value);
    }
    (Array.isArray(registration.sources) ? registration.sources : []).forEach(function (source) {
      addSource(source && source.name);
    });
    members.forEach(function (member) {
      member.sourceNames.forEach(addSource);
    });
    (Array.isArray(plan.assignments) ? plan.assignments : []).forEach(function (assignment) {
      addSource(portableBasename(assignment && assignment.source_dir));
    });
    var distinctWorks = Object.create(null);
    members.forEach(function (member) { distinctWorks[member.workKey] = true; });
    return {
      supported: !!(supported || Object.keys(distinctWorks).length > 1),
      candidates: candidates,
      members: members,
      sources: sources,
    };
  }

  function selectedSharedTargetMembers(bundle) {
    var selected = sharedTargetState().selectedMembers;
    return (bundle && Array.isArray(bundle.members) ? bundle.members : []).filter(function (member) {
      return selected[member.key] === true;
    });
  }

  function suggestedSharedTargetPath(groupMembers) {
    var values = (Array.isArray(groupMembers) ? groupMembers : [])
      .map(function (member) { return firstString(member.pressPath); })
      .filter(function (value, index, rows) { return !!value && rows.indexOf(value) === index; });
    return values.length === 1 ? values[0] : "";
  }

  function directSharedTargetName(pressPath) {
    var normalized = String(pressPath || "")
      .trim()
      .replace(/\//g, "\\")
      .replace(/^\\+|\\+$/g, "");
    if (!normalized || normalized.indexOf("\\") >= 0) return "";
    return normalized.toLocaleLowerCase();
  }

  function sourceIsLandedSharedTarget(sourceName, bindings) {
    var sourceKey = directSharedTargetName(sourceName);
    if (!sourceKey) return false;
    return (Array.isArray(bindings) ? bindings : []).some(function (binding) {
      return directSharedTargetName(binding && binding.press_path) === sourceKey;
    });
  }

  function buildSharedTargetBindings(plan) {
    var shared = sharedTargetState();
    var bundle = sharedTargetCandidateBundle(plan, true);
    if (!shared.confirmed) {
      return { bindings: [], error: "请先明确勾选‘共享同一个实体目录’。" };
    }
    var selected = selectedSharedTargetMembers(bundle);
    if (selected.length < 2) {
      return { bindings: [], error: "至少选择两个不同数据库作品的压制记录。" };
    }
    var grouped = Object.create(null);
    selected.forEach(function (member) {
      if (!grouped[member.groupKey]) grouped[member.groupKey] = [];
      grouped[member.groupKey].push(member);
    });
    var bindings = [];
    var selectedKeys = Object.create(null);
    selected.forEach(function (member) { selectedKeys[member.key] = true; });
    var groupKeys = Object.keys(grouped);
    for (var groupIndex = 0; groupIndex < groupKeys.length; groupIndex++) {
      var groupKey = groupKeys[groupIndex];
      var members = grouped[groupKey];
      var workKeys = Object.create(null);
      members.forEach(function (member) { workKeys[member.workKey] = true; });
      if (Object.keys(workKeys).length < 2) {
        return {
          bindings: [],
          error: members[0].pressFormat + " / " + members[0].pressGroup + " 必须选择至少两个不同播出记录。",
        };
      }
      var pressPath = firstString(
        shared.pressPaths[groupKey],
        suggestedSharedTargetPath(members),
      );
      if (!pressPath) {
        return {
          bindings: [],
          error: "请填写 " + members[0].pressFormat + " / " + members[0].pressGroup + " 共用的 press_path。",
        };
      }
      bindings.push({
        press_format: members[0].pressFormat,
        press_group: members[0].pressGroup,
        press_path: pressPath,
        members: members.map(function (member) {
          var sourceNames = bundle.sources.filter(function (sourceName) {
            return shared.sourceOwners[sourceName] === member.key;
          });
          return {
            catalog_ref: cloneJson(member.catalogRef),
            press_key: member.pressKey,
            source_names: sourceNames,
          };
        }),
      });
    }
    for (var sourceIndex = 0; sourceIndex < bundle.sources.length; sourceIndex++) {
      var sourceName = bundle.sources[sourceIndex];
      if (sourceIsLandedSharedTarget(sourceName, bindings)) continue;
      var owner = firstString(shared.sourceOwners[sourceName]);
      if (!owner || !selectedKeys[owner]) {
        return { bindings: [], error: "来源目录尚未分配给已选播出记录：" + sourceName };
      }
    }
    return { bindings: bindings, error: "" };
  }

  function sharedTargetEdited(message) {
    state.landing = null;
    invalidatePreview(message || "共享实体目录设置已编辑，请重新生成完整落地预览。");
    render();
  }

  function setSharedTargetConfirmed(confirmed) {
    sharedTargetState().confirmed = !!confirmed;
    sharedTargetEdited(
      confirmed
        ? "已启用共享实体目录；请选择精确数据库记录、分配来源并生成预览。"
        : "已取消共享实体目录；不会把多个数据库记录指向同一实体目录。",
    );
  }

  function setSharedTargetMemberSelected(memberKey, selected) {
    var bundle = sharedTargetCandidateBundle(state.plan, true);
    var member = bundle.members.find(function (item) { return item.key === memberKey; });
    if (!member) return false;
    var shared = sharedTargetState();
    if (selected) {
      shared.selectedMembers[member.key] = true;
      if (!firstString(shared.pressPaths[member.groupKey]) && member.pressPath) {
        shared.pressPaths[member.groupKey] = member.pressPath;
      }
      member.sourceNames.forEach(function (sourceName) {
        if (!shared.sourceOwners[sourceName]) shared.sourceOwners[sourceName] = member.key;
      });
    } else {
      delete shared.selectedMembers[member.key];
      Object.keys(shared.sourceOwners).forEach(function (sourceName) {
        if (shared.sourceOwners[sourceName] === member.key) delete shared.sourceOwners[sourceName];
      });
    }
    sharedTargetEdited("共享数据库记录已编辑，请重新生成完整落地预览。");
    return true;
  }

  function setSharedTargetPressPath(groupKey, value) {
    var shared = sharedTargetState();
    shared.pressPaths[String(groupKey || "")] = String(value || "");
    var landedName = directSharedTargetName(value);
    if (landedName) {
      sharedTargetCandidateBundle(state.plan, true).sources.forEach(function (sourceName) {
        if (directSharedTargetName(sourceName) === landedName) {
          delete shared.sourceOwners[sourceName];
        }
      });
    }
    sharedTargetEdited("共享 press_path 已编辑，请重新生成完整落地预览。");
  }

  function setSharedTargetSourceOwner(sourceName, memberKey) {
    var shared = sharedTargetState();
    sourceName = firstString(sourceName);
    memberKey = firstString(memberKey);
    if (!sourceName) return false;
    if (memberKey) shared.sourceOwners[sourceName] = memberKey;
    else delete shared.sourceOwners[sourceName];
    sharedTargetEdited("共享目录的来源分配已编辑，请重新生成完整落地预览。");
    return true;
  }

  function renderSharedTargetRegistration(plan) {
    var bundle = sharedTargetCandidateBundle(plan, true);
    if (!bundle.supported || bundle.candidates.length < 2) return "";
    var shared = sharedTargetState();
    var disabled = state.busy || state.shortcutPending ? " disabled" : "";
    var memberDisabled = state.busy || state.shortcutPending || !shared.confirmed
      ? " disabled"
      : "";
    var selected = selectedSharedTargetMembers(bundle);
    var selectedByGroup = Object.create(null);
    selected.forEach(function (member) {
      if (!selectedByGroup[member.groupKey]) selectedByGroup[member.groupKey] = [];
      selectedByGroup[member.groupKey].push(member);
    });
    var candidateRows = bundle.candidates
      .map(function (candidate) {
        var ref = candidate.catalogRef || {};
        var dateText = [candidate.dateStart, candidate.dateEnd]
          .filter(function (value) { return !!value; })
          .join(" → ");
        var presses = candidate.members
          .map(function (member) {
            var checked = shared.selectedMembers[member.key] === true;
            return (
              '<label class="organizer-shared-target-member' +
              (checked ? " is-selected" : "") +
              '"><input type="checkbox" data-shared-target-member="' +
              esc(member.key) +
              '"' +
              (checked ? " checked" : "") +
              memberDisabled +
              ' /><span><strong>' +
              esc(member.pressFormat + " / " + member.pressGroup) +
              "</strong><small>" +
              esc(member.pressPath || "press_path 尚未填写") +
              "</small></span></label>"
            );
          })
          .join("");
        return (
          '<article class="organizer-shared-target-candidate"><header><div><strong>' +
          esc(candidate.label) +
          "</strong>" +
          (dateText ? "<span>" + esc(dateText) + "</span>" : "") +
          '</div><code>' +
          esc(firstString(ref.yaml_source_rel, "-")) +
          "#" +
          esc(ref.index_in_file == null ? "-" : ref.index_in_file) +
          "</code></header><div class=\"organizer-shared-target-members\">" +
          presses +
          "</div></article>"
        );
      })
      .join("");
    var groupRows = Object.keys(selectedByGroup)
      .map(function (groupKey) {
        var members = selectedByGroup[groupKey];
        var selectedPath = firstString(
          shared.pressPaths[groupKey],
          suggestedSharedTargetPath(members),
        );
        var workCount = Object.keys(
          members.reduce(function (seen, member) {
            seen[member.workKey] = true;
            return seen;
          }, Object.create(null)),
        ).length;
        return (
          '<fieldset class="organizer-shared-target-binding"><legend>' +
          esc(members[0].pressFormat + " / " + members[0].pressGroup) +
          " · " +
          esc(workCount) +
          ' 条数据库记录</legend><label><span>共同 press_path / 实体目录</span><input type="text" data-shared-target-path="' +
          esc(groupKey) +
          '" value="' +
          esc(selectedPath) +
          '" autocomplete="off" spellcheck="false" placeholder="例如 Fate／Zero_BDRip(VCBM)"' +
          disabled +
          " /></label></fieldset>"
        );
      })
      .join("");
    var displayedBindings = Object.keys(selectedByGroup).map(function (groupKey) {
      var members = selectedByGroup[groupKey];
      return {
        press_path: firstString(
          shared.pressPaths[groupKey],
          suggestedSharedTargetPath(members),
        ),
      };
    });
    var sourceRows = "";
    if (selected.length && bundle.sources.length) {
      sourceRows =
        '<fieldset class="organizer-shared-target-sources"><legend>来源目录归属</legend><p>每个待整理的一级来源目录必须且只能归给一个播出记录；不承载文件的记录可以作为别名成员保留为空。</p>' +
        bundle.sources
          .map(function (sourceName) {
            var owner = firstString(shared.sourceOwners[sourceName]);
            if (sourceIsLandedSharedTarget(sourceName, displayedBindings)) {
              return (
                '<label class="organizer-shared-target-source is-landed"><span><code>' +
                esc(sourceName) +
                '</code><small>共同 press_path 已存在；留空不会移动，存在散文件时可明确分配。</small></span><select data-shared-target-source="' +
                esc(sourceName) +
                '"' +
                disabled +
                '><option value=""' +
                (!owner ? " selected" : "") +
                '>已落地目标（不分配 owner）</option>' +
                selected
                  .map(function (member) {
                    return (
                      '<option value="' +
                      esc(member.key) +
                      '"' +
                      (owner === member.key ? " selected" : "") +
                      ">" +
                      esc(member.workName + " · " + member.pressFormat + " / " + member.pressGroup) +
                      "</option>"
                    );
                  })
                  .join("") +
                "</select></label>"
              );
            }
            return (
              '<label><span><code>' +
              esc(sourceName) +
              '</code></span><select data-shared-target-source="' +
              esc(sourceName) +
              '"' +
              disabled +
              '><option value="">请选择播出记录</option>' +
              selected
                .map(function (member) {
                  return (
                    '<option value="' +
                    esc(member.key) +
                    '"' +
                    (owner === member.key ? " selected" : "") +
                    ">" +
                    esc(member.workName + " · " + member.pressFormat + " / " + member.pressGroup) +
                    "</option>"
                  );
                })
                .join("") +
              "</select></label>"
            );
          })
          .join("") +
        "</fieldset>";
    }
    var localCheck = shared.confirmed ? buildSharedTargetBindings(plan) : { error: "" };
    return (
      '<section class="organizer-shared-target"><div class="organizer-shared-target-head"><div><h3>已有播出记录共享实体目录</h3><p>适用于分期播出但实体压制合并保存的情况。每条数据库记录和快捷方式都会保留，媒体目录只整理一次。</p></div><span>默认关闭</span></div>' +
      '<label class="organizer-shared-target-confirm"><input type="checkbox" data-shared-target-confirm' +
      (shared.confirmed ? " checked" : "") +
      disabled +
      ' /><span><strong>我确认所选播出记录共享同一个实体目录</strong><small>勾选只会启用共享预览；未经新的完整预览不能执行。</small></span></label>' +
      (shared.confirmed
        ? '<div class="organizer-shared-target-candidates">' +
          candidateRows +
          "</div>" +
          (groupRows
            ? '<div class="organizer-shared-target-bindings">' + groupRows + "</div>"
            : '<p class="organizer-shared-target-empty">请在至少两个播出记录中选择相同格式和压制组。</p>') +
          sourceRows +
          (localCheck.error
            ? '<p class="organizer-shared-target-error">' + esc(localCheck.error) + "</p>"
            : '<p class="organizer-shared-target-ready">共享绑定已填写完整；请生成只读完整落地预览。</p>')
        : "") +
      "</section>"
    );
  }

  function renderWorkRegistration(plan) {
    var required = !!(plan && plan.registration_required);
    var drafts = landingDrafts();
    var sharedBundle = sharedTargetCandidateBundle(plan, true);
    var sharedAvailable = !!(
      sharedBundle.supported && sharedBundle.candidates.length >= 2
    );
    if (!required && !drafts.length && !sharedAvailable) return "";
    if (!drafts.length && !sharedAvailable) {
      return '<p class="organizer-empty">正在准备新增作品表单…</p>';
    }
    var disabled = state.busy ? " disabled" : "";
    var workForms = drafts.map(function (draft, workIndex) {
      var date = draft.date && typeof draft.date === "object" ? draft.date : {};
      var presses = Array.isArray(draft.presses) ? draft.presses : [];
      var pressRows = presses.map(function (press, index) {
          var sourceNames = Array.isArray(press.source_names) ? press.source_names : [];
          return (
            '<fieldset class="organizer-registration-press"><legend>来源压制 ' +
            esc(index + 1) +
            "</legend>" +
            '<p><strong>来源目录：</strong><code>' +
            esc(sourceNames.join(" / ") || "-") +
            "</code></p>" +
            '<div class="organizer-registration-grid">' +
            '<label><span>压制格式</span><input type="text" data-landing-press="press_format" data-work-index="' + esc(workIndex) + '" data-press-index="' +
            esc(index) + '" value="' + esc(press.press_format || "") + '" autocomplete="off"' + disabled + " /></label>" +
            '<label><span>压制组 / 组合简称</span><select data-landing-press="press_group" data-work-index="' + esc(workIndex) + '" data-press-index="' + esc(index) + '"' + disabled + ">" +
            renderGroupOptions(press.press_group) + "</select></label>" +
            '<label class="organizer-registration-wide"><span>数据库 press_path / 整理目标</span><input type="text" data-landing-press="press_path" data-work-index="' + esc(workIndex) + '" data-press-index="' + esc(index) + '" value="' + esc(press.press_path || "") + '" autocomplete="off" spellcheck="false"' + disabled + " /></label>" +
            "</div></fieldset>"
          );
        }).join("");
      return (
        '<fieldset class="organizer-registration-work"><legend>作品 ' + esc(workIndex + 1) + "</legend>" +
        '<div class="organizer-registration-grid">' +
        '<label><span>作品名</span><input type="text" data-landing-field="name" data-work-index="' + esc(workIndex) + '" value="' + esc(draft.name || "") + '" autocomplete="off"' + disabled + " /></label>" +
        '<label><span>开始日期</span><input type="date" data-landing-date="start" data-work-index="' + esc(workIndex) + '" value="' + esc(date.start || "") + '"' + disabled + " /></label>" +
        '<label><span>结束日期（可空）</span><input type="date" data-landing-date="end" data-work-index="' + esc(workIndex) + '" value="' + esc(date.end || "") + '"' + disabled + " /></label>" +
        '<label><span>作品类型</span><select data-landing-field="domain" data-work-index="' + esc(workIndex) + '"' + disabled + '><option value="animation"' + (draft.domain === "animation" ? " selected" : "") + '>动画</option><option value="television"' + (draft.domain === "television" ? " selected" : "") + ">电视剧</option></select></label>" +
        '<label><span>国家</span><select data-landing-field="country" data-work-index="' + esc(workIndex) + '"' + disabled + '><option value="japan"' + (draft.country === "japan" ? " selected" : "") + '>日本</option><option value="korea"' + (draft.country === "korea" ? " selected" : "") + '>韩国</option><option value="china"' + (draft.country === "china" ? " selected" : "") + ">中国</option></select></label>" +
        '<label><span>发行类型</span><select data-landing-field="release_type" data-work-index="' + esc(workIndex) + '"' + disabled + '><option value="tv"' + (draft.release_type === "tv" ? " selected" : "") + '>TV</option><option value="ova"' + (draft.release_type === "ova" ? " selected" : "") + '>OVA</option><option value="movie"' + (draft.release_type === "movie" ? " selected" : "") + ">Movie</option></select></label>" +
        '<label class="organizer-registration-wide"><span>作品目录（共享根目录，自动写入 DB）</span><input type="text" value="' + esc(draft.path || state.root) + '" readonly /></label></div>' +
        pressRows + "</fieldset>"
      );
    }).join("");
    return (
      '<section class="organizer-section organizer-registration">' +
      renderSharedTargetRegistration(plan) +
      (sharedTargetState().confirmed
        ? "<h2>核对共享实体目录落地</h2><p>将修正所选已有数据库记录，并分别生成快捷方式；当前仍未写数据库或移动文件。</p>"
        : !drafts.length
        ? "<h2>请选择共享数据库记录</h2><p>当前没有新增作品草稿；只有明确启用并填写共享绑定后，才能生成完整落地预览。</p>"
        : drafts.length > 1
        ? "<h2>检测到同根多作品：逐部核对新增</h2><p>每个来源已按作品分组。请分别确认作品名、正式日期和两条压制；预览不会写数据库或移动文件。</p>"
        : "<h2>数据库中没有该作品：手填新增</h2><p>这里只生成完整预览，不会立即写数据库、移动文件或创建快捷方式。</p>" +
          (state.sourceWorkEditorSource ? "" : renderLandingRecognition(drafts[0]))) +
      (sharedTargetState().confirmed ? "" : workForms) +
      '<button type="button" class="btn" data-organizer-action="landing-preview"' +
      (state.busy || state.shortcutPending || (!drafts.length && !sharedTargetState().confirmed)
        ? " disabled"
        : "") +
      ">" +
      (state.busy === "landing-preview"
        ? "生成中…"
        : sharedTargetState().confirmed
        ? "生成“共享 DB → 归类一次 → 多快捷方式”预览"
        : "生成“DB → 归类 → 快捷方式”完整预览") +
      "</button></section>"
    );
  }

  function renderShortcutRows(rows) {
    if (!Array.isArray(rows) || !rows.length) return "";
    return (
      '<ul class="organizer-shortcut-plan">' +
      rows
        .map(function (item) {
          return (
            "<li><code>" +
            esc(item.shortcut_path || item.path || "-") +
            "</code><span>→</span><code>" +
            esc(item.target_path || item.shortcut_target_path || "-") +
            "</code><strong>" +
            esc(item.status || "planned") +
            "</strong></li>"
          );
        })
        .join("") +
      "</ul>"
    );
  }

  function shortcutIssueLabel(code) {
    var labels = {
      "catalog-work-not-found": "缺少作品数据库记录",
      "shortcut-catalog-path-missing": "缺少数据库作品路径",
      "shortcut-catalog-path-invalid": "数据库作品路径无效",
      "shortcut-catalog-path-mismatch": "数据库作品路径不一致",
      "shortcut-catalog-press-invalid": "数据库压制记录不完整",
      "shortcut-catalog-press-empty": "数据库没有压制记录",
      "shortcut-catalog-press-not-found": "数据库没有对应压制记录",
      "shortcut-press-path-missing": "数据库缺少 press_path",
      "shortcut-database-target-mismatch": "整理目标与 press_path 不一致",
      "shortcut-target-missing": "快捷方式目标目录不存在",
      "shortcut-conflict": "快捷方式路径冲突",
      "repair-catalog-selection-required": "必须选择数据库作品",
      "repair-target-missing": "press_path 目标目录不存在",
      "repair-shortcut-preview-failed": "快捷方式修复预览失败",
      "repair-media-root-mismatch": "整理计划根目录不一致",
      "repair-media-plan-not-ready": "媒体整理计划仍有未决问题",
      "repair-media-catalog-selection-required": "必须选择当前计划对应的数据库作品",
      "repair-media-plan-multiple-works": "当前计划包含多个作品",
      "repair-media-plan-unbound": "数据库作品与整理任务无法绑定",
      "repair-media-target-unsafe": "整理目标路径不安全",
      "repair-media-target-mismatch": "整理目标路径不一致",
      "repair-media-target-without-move": "目标目录没有待移动文件",
      "repair-media-target-unregistered": "整理目标缺少对应 press_path",
      "shared-target-confirmation-required": "尚未确认共享实体目录",
      "shared-target-without-media-route": "共享实体目录没有对应媒体路由",
      "shared-target-empty": "共享实体目录为空",
    };
    return labels[String(code || "")] || firstString(code, "快捷方式计划问题");
  }

  function renderShortcutIssues(issues) {
    if (!Array.isArray(issues) || !issues.length) return "";
    return (
      '<section class="organizer-shortcut-issues" aria-label="快捷方式不能执行的原因">' +
      "<h3>当前不能创建的具体原因</h3><ul>" +
      issues
        .map(function (issue) {
          issue = issue && typeof issue === "object" ? issue : {};
          return (
            "<li><strong>" +
            esc(shortcutIssueLabel(issue.code)) +
            "</strong><p>" +
            esc(firstString(issue.message, issue.error, "服务端未提供详细原因")) +
            "</p>" +
            (firstString(issue.path)
              ? "<code>" + esc(issue.path) + "</code>"
              : "") +
            "</li>"
          );
        })
        .join("") +
      "</ul></section>"
    );
  }

  function repairCandidateSummary(candidate, index) {
    var draft = firstObject(candidate && candidate.draftWork);
    var presses = Array.isArray(draft.presses)
      ? draft.presses
      : Array.isArray(draft.collectioned_ordered)
      ? draft.collectioned_ordered
      : [];
    var paths = presses
      .map(function (press) {
        return firstString(press && press.press_path);
      })
      .filter(function (value) {
        return !!value;
      });
    return {
      title: firstString(candidate && candidate.label, draft.name, "修复候选 " + (index + 1)),
      detail: paths.length ? paths.join(" / ") : "请继续核对作品信息和 press_path",
    };
  }

  function renderShortcutRepairChoices(pending, retry, issues) {
    var repair = shortcutRepairDetails(retry);
    var candidates = repair.candidates.slice();
    var includeMediaMove = !!(pending && pending.includeMediaMove);
    var stagedMultiWorkRepair = !!(pending && pending.stagedMultiWorkRepair);
    if (!repair.required && !issuesNeedCatalogRepair(issues)) return "";
    if (!candidates.length) {
      return (
        '<div class="organizer-repair-callout"><p>需要先补齐作品数据库记录，快捷方式才能使用数据库中的日期、作品名和 press_path。</p>' +
        '<button type="button" class="btn" data-organizer-action="shortcut-registration-guide"' +
        (state.busy ? " disabled" : "") +
        ">返回并填写作品数据库信息</button></div>"
      );
    }
    return (
      '<div class="organizer-repair-callout"><div><strong>可以安全修复</strong>' +
      (includeMediaMove
        ? "<p>将按“数据库修复 + 媒体归类 + 快捷方式”串联处理；选择和预览阶段仍不会写入或移动文件。</p></div>"
        : stagedMultiWorkRepair
        ? "<p>当前整理计划涉及多个数据库作品。请先逐个修复 path/press_path；这些步骤只修数据库和快捷方式，不移动媒体。全部绑定后重新预览，才会统一生成媒体归类计划。</p></div>"
        : "<p>下面的操作只预览补录/修正数据库和快捷方式，不会再次移动媒体文件。</p></div>") +
      '<div class="organizer-repair-candidates">' +
      candidates
        .map(function (candidate, index) {
          var summary = repairCandidateSummary(candidate, index);
          return (
            '<button type="button" class="btn" data-organizer-action="shortcut-repair-select" data-repair-candidate-index="' +
            esc(index) +
            '"' +
            (state.busy ? " disabled" : "") +
            "><strong>" +
            esc(candidates.length > 1 ? "选择并填写：" + summary.title : "选择并核对补录信息") +
            "</strong><span>" +
            esc(summary.detail) +
            "</span></button>"
          );
        })
        .join("") +
      (repair.independentAppendAllowed && repair.directDraft
        ? '<button type="button" class="btn danger" data-organizer-action="shortcut-repair-independent"' +
          (state.busy ? " disabled" : "") +
          '><strong>明确新增为独立作品</strong><span>仅在这些候选只是共享 family root 或名称前缀误命中时使用；不会修改候选记录。</span></button>'
        : "") +
      "</div></div>"
    );
  }

  function repairDraftPresses(draft) {
    if (!draft || typeof draft !== "object") return [];
    if (Array.isArray(draft.presses)) return draft.presses;
    if (Array.isArray(draft.collectioned_ordered)) {
      draft.presses = cloneJson(draft.collectioned_ordered);
      return draft.presses;
    }
    draft.presses = [];
    return draft.presses;
  }

  function renderRepairFormatOptions(selected) {
    var markers = firstObject(state.config && state.config.detection && state.config.detection.format_markers);
    var values = Object.keys(markers);
    var current = firstString(selected);
    var configured = values.length > 0;
    var selectedValue = values.find(function (value) {
      return String(value).toLowerCase() === current.toLowerCase();
    });
    if (!configured && current) {
      values.push(current);
      selectedValue = current;
    }
    values.sort(function (left, right) {
      return String(left).localeCompare(String(right), "zh-CN", { sensitivity: "base" });
    });
    return [
      '<option value=""' +
        (!selectedValue ? " selected" : "") +
        ">" +
        esc(configured && current ? "当前格式不在配置中，请重新选择" : "请选择数据库压制格式") +
        "</option>",
    ]
      .concat(
        values.map(function (value) {
          return (
            '<option value="' +
            esc(value) +
            '"' +
            (value === selectedValue ? " selected" : "") +
            ">" +
            esc(value) +
            "</option>"
          );
        }),
      )
      .join("");
  }

  function renderRepairGroupOptions(selected) {
    var current = firstString(selected);
    var registry = groupRegistryOptions();
    var selectedCode = "";
    registry.forEach(function (item) {
      var code = firstString(item && item.code);
      if (code && code.toLowerCase() === current.toLowerCase()) selectedCode = code;
    });
    if (!registry.length && current) {
      return (
        '<option value="">请选择组简称或组合简称</option>' +
        '<option value="' + esc(current) + '" selected>' + esc(current) + "</option>"
      );
    }
    var options = [
      '<option value=""' +
        (!selectedCode ? " selected" : "") +
        ">" +
        esc(current ? "当前组不在数据库注册表中，请重新选择" : "请选择组简称或组合简称") +
        "</option>",
    ];
    if (current && !selectedCode) {
      options.push(
        '<option value="' +
          esc(current) +
          '" selected>' +
          esc(current + "（历史数据库值，仅可保留）") +
          "</option>",
      );
    }
    registry.forEach(function (item) {
      var code = firstString(item && item.code);
      if (!code) return;
      options.push(
        '<option value="' +
          esc(code) +
          '"' +
          (code === selectedCode ? " selected" : "") +
          ">" +
          esc(firstString(item && item.label, code)) +
          "</option>",
      );
    });
    return options.join("");
  }

  function renderShortcutRepairEditor(pending) {
    var draft = pending && pending.repairDraft;
    if (!draft) return "";
    var date = firstObject(draft.date);
    var presses = repairDraftPresses(draft);
    var catalogRef = nonEmptyObject(pending.repairCatalogRef);
    var includeMediaMove = !!pending.includeMediaMove;
    var disabled = state.busy ? " disabled" : "";
    var identityDisabled = state.busy || catalogRef ? " disabled" : "";
    var pressRows = presses
      .map(function (press, index) {
        return (
          '<fieldset class="organizer-registration-press organizer-repair-press"><legend>压制记录 ' +
          esc(index + 1) +
          "</legend>" +
          '<div class="organizer-registration-grid">' +
          '<label><span>压制格式</span><select data-repair-press="press_format" data-repair-press-index="' +
          esc(index) +
          '"' +
          identityDisabled +
          ">" +
          renderRepairFormatOptions(press.press_format) +
          "</select></label>" +
          '<label><span>压制组 / 组合简称</span><select data-repair-press="press_group" data-repair-press-index="' +
          esc(index) +
          '"' +
          identityDisabled +
          ">" +
          renderRepairGroupOptions(press.press_group) +
          "</select></label>" +
          '<label class="organizer-registration-wide"><span>' +
          (includeMediaMove
            ? "数据库 press_path / 本次整理目标"
            : "数据库 press_path / 已整理目标") +
          '</span><input type="text" data-repair-press="press_path" data-repair-press-index="' +
          esc(index) +
          '" value="' +
          esc(press.press_path || "") +
          '" autocomplete="off" spellcheck="false" placeholder="' +
          (includeMediaMove
            ? "可填写本计划将创建的目标目录名"
            : "必须填写已存在的目标目录名") +
          '"' +
          disabled +
          " /></label></div></fieldset>"
        );
      })
      .join("");
    return (
      '<section class="organizer-section organizer-shortcut-pending organizer-repair-editor">' +
      '<div class="organizer-plan-head"><div><h2>' +
      (includeMediaMove ? "核对数据库修复与媒体归类信息" : "核对数据库补录信息") +
      "</h2>" +
      (includeMediaMove
        ? "<p>逐项确认作品路径、每个 press_path 与本次整理目标。预览只读；确认后才会修复数据库、归类媒体并创建快捷方式。</p></div>"
        : "<p>逐项确认作品路径与每个 press_path。生成预览不会写数据库、创建快捷方式或移动媒体。</p></div>") +
      '<span class="organizer-ready is-blocked">待预览</span></div>' +
      (catalogRef
        ? '<p class="organizer-repair-catalog-ref"><strong>修正现有数据库记录：</strong><code>' +
          esc(catalogRef.yaml_source_rel || "-") +
          "#" +
          esc(catalogRef.index_in_file == null ? "-" : catalogRef.index_in_file) +
          "</code></p>"
        : '<p class="organizer-repair-catalog-ref"><strong>数据库动作：</strong>新增作品记录</p>') +
      '<div class="organizer-registration-grid">' +
      '<label><span>作品名</span><input type="text" data-repair-field="name" value="' +
      esc(draft.name || "") +
      '" autocomplete="off"' +
      disabled +
      " /></label>" +
      '<label><span>开始日期</span><input type="date" data-repair-date="start" value="' +
      esc(date.start || "") +
      '"' +
      disabled +
      " /></label>" +
      '<label><span>结束日期（可空）</span><input type="date" data-repair-date="end" value="' +
      esc(date.end || "") +
      '"' +
      disabled +
      " /></label>" +
      '<label><span>作品类型</span><select data-repair-field="domain"' +
      disabled +
      '><option value="animation"' +
      (draft.domain === "animation" ? " selected" : "") +
      '>动画</option><option value="television"' +
      (draft.domain === "television" ? " selected" : "") +
      ">电视剧</option></select></label>" +
      '<label><span>国家</span><select data-repair-field="country"' +
      disabled +
      '><option value="japan"' +
      (draft.country === "japan" ? " selected" : "") +
      '>日本</option><option value="korea"' +
      (draft.country === "korea" ? " selected" : "") +
      '>韩国</option><option value="china"' +
      (draft.country === "china" ? " selected" : "") +
      ">中国</option></select></label>" +
      '<label><span>发行类型</span><select data-repair-field="release_type"' +
      disabled +
      '><option value="tv"' +
      (draft.release_type === "tv" ? " selected" : "") +
      '>TV</option><option value="ova"' +
      (draft.release_type === "ova" ? " selected" : "") +
      '>OVA</option><option value="movie"' +
      (draft.release_type === "movie" ? " selected" : "") +
      ">Movie</option></select></label>" +
      '<label class="organizer-registration-wide"><span>作品 path（固定为当前根目录）</span><input type="text" value="' +
      esc(draft.path || pending.root || state.root) +
      '" readonly /></label></div>' +
      (pressRows || '<p class="organizer-pending-error">没有可填写的压制记录，不能生成修复预览。</p>') +
      '<div class="organizer-pending-actions organizer-pending-actions-primary">' +
      '<button type="button" class="btn danger" data-organizer-action="shortcut-repair-preview"' +
      (presses.length && !state.busy ? "" : " disabled") +
      ">" +
      (state.busy === "shortcut-repair-preview"
        ? "预览中…"
        : includeMediaMove
        ? "生成 DB + 媒体 + 快捷方式预览"
        : "生成 DB + 快捷方式修复预览") +
      "</button>" +
      '<button type="button" class="btn secondary" data-organizer-action="shortcut-repair-back"' +
      disabled +
      ">返回候选</button>" +
      '<button type="button" class="btn secondary" data-organizer-action="shortcut-retry-cancel"' +
      disabled +
      ">退出修复</button></div></section>"
    );
  }

  function renderPlanShortcutSummary(plan) {
    if (!plan || plan.registration_required) return "";
    var details = shortcutPlanDetails(plan);
    var summary = details.summary;
    var settledState = alreadyOrganizedShortcutState(plan);
    if (settledState) {
      var settledConflicts = Number(summary.conflict_count) || 0;
      var settledIssues = visiblePlanIssues(plan);
      var settledComplete = settledState === "complete";
      var settledPending = settledState === "pending";
      return (
        '<section class="organizer-section organizer-shortcut-summary">' +
        '<div class="organizer-plan-head"><div><h2>已整理目录检查</h2><p>' +
        (settledComplete
          ? "媒体归类和快捷方式均已完成。"
          : settledPending
          ? "媒体已整理，仅快捷方式待补建；不会再次移动媒体。"
          : "媒体已经整理，但数据库或快捷方式目标仍需修复。") +
        '</p></div><span class="organizer-ready ' +
        (settledComplete ? "is-ready" : "is-blocked") +
        '">' +
        (settledComplete ? "已完整" : settledPending ? "待补建" : "需修复") +
        "</span></div>" +
        '<p>计划补建 ' +
        esc(summary.planned_count || 0) +
        "，已存在 " +
        esc(summary.already_exists_count || 0) +
        "，冲突 " +
        esc(settledConflicts) +
        "。</p>" +
        (settledPending
          ? '<div class="organizer-summary-actions"><button type="button" class="btn danger" data-organizer-action="shortcut-only-preview"' +
            (state.busy || state.shortcutPending ? " disabled" : "") +
            ">检查并补建快捷方式</button><span>只读复核后仅增量创建缺少的快捷方式。</span></div>"
          : "") +
        (settledState === "blocked" ? renderShortcutIssues(settledIssues) : "") +
        renderShortcutRows(details.rows) +
        "</section>"
      );
    }
    if (!details.present) {
      return (
        '<section class="organizer-section organizer-shortcut-summary">' +
        '<div class="organizer-plan-head"><div><h2>快捷方式预览</h2>' +
        '<p>服务端没有返回快捷方式计划；为避免移动完成后遗漏快捷方式，当前计划不可执行。</p>' +
        '</div><span class="organizer-ready is-blocked">缺少计划</span></div></section>'
      );
    }
    var conflicts = Number(summary.conflict_count) || 0;
    var ready = !!details.planId && conflicts === 0;
    return (
      '<section class="organizer-section organizer-shortcut-summary">' +
      '<div class="organizer-plan-head"><div><h2>快捷方式预览</h2><p>快捷方式确认 ID：<code>' +
      esc(details.planId || "-") +
      '</code></p></div><span class="organizer-ready ' +
      (ready ? "is-ready" : "is-blocked") +
      '">' +
      (ready ? "可执行" : "不可执行") +
      "</span></div>" +
      '<p>计划 ' +
      esc(summary.planned_count || 0) +
      "，已存在 " +
      esc(summary.already_exists_count || 0) +
      "，冲突 " +
      esc(conflicts) +
      "。执行媒体移动后会立即增量创建这些快捷方式。</p>" +
      renderShortcutRows(details.rows) +
      "</section>"
    );
  }

  function renderLandingSummary() {
    var landing = state.landing;
    if (!landing) return "";
    var catalog = landing.catalog_change || {};
    var catalogs = Array.isArray(landing.catalog_changes) ? landing.catalog_changes : [];
    var catalogActions = catalogs.length
      ? catalogs.map(function (item) { return firstString(item && item.action, "-"); }).join(" / ")
      : firstString(catalog.action, "-");
    var catalogTargets = catalogs.length
      ? catalogs.map(function (item) { return firstString(item && item.target, "-"); }).join(" / ")
      : firstString(catalog.target, "-");
    var shortcutSummary = landing.shortcut_summary || {};
    var shortcuts = Array.isArray(landing.shortcuts) ? landing.shortcuts : [];
    var sharedSummary = firstObject(landing.shared_target_summary);
    var sourceWorkSummary = firstObject(landing.source_work_summary);
    var sharedSummaryText = sharedSummary.database_record_count == null
      ? ""
      : String(sharedSummary.database_record_count || 0) +
        " 条数据库记录 / " +
        String(sharedSummary.physical_target_count || 0) +
        " 个实体目录 / " +
        String(sharedSummary.shortcut_count || 0) +
        " 个快捷方式";
    return (
      '<section class="organizer-section organizer-landing-summary">' +
      '<div class="organizer-plan-head"><div><h2>完整落地预览</h2><p>确认 ID：<code>' +
      esc(landing.landing_plan_id || "-") +
      '</code></p></div><span class="organizer-ready ' +
      (landing.ready ? "is-ready" : "is-blocked") +
      '">' +
      (landing.ready ? "可执行" : "不可执行") +
      "</span></div>" +
      '<dl class="organizer-assignment-meta"><div><dt>数据库动作</dt><dd>' +
      esc(catalogActions) +
      "</dd></div><div><dt>数据库文件</dt><dd><code>" +
      esc(catalogTargets) +
      "</code></dd></div><div><dt>快捷方式</dt><dd>计划 " +
      esc(shortcutSummary.planned_count || 0) +
      "，冲突 " +
      esc(shortcutSummary.conflict_count || 0) +
      "</dd></div>" +
      (sharedSummaryText
        ? "<div><dt>共享关系</dt><dd><strong>" + esc(sharedSummaryText) + "</strong></dd></div>"
        : "") +
      "</dl>" +
      '<div class="organizer-summary-actions"><button type="button" class="btn danger" data-organizer-action="apply"' +
      (planCanExecute() ? "" : " disabled") +
      ">" +
      (state.busy === "apply" ? "执行中…" : "确认完整落地") +
      "</button><span>" +
      (sharedSummaryText
        ? "更新所选数据库记录 → 媒体只归类一次 → 分别增量创建快捷方式"
        : "写入数据库 → 执行已预览的媒体归类 → 增量创建快捷方式") +
      "</span></div>" +
      renderShortcutIssues(Array.isArray(landing.issues) ? landing.issues : []) +
      renderShortcutRows(shortcuts) +
      "</section>"
    );
  }

  function renderShortcutRepairPreview(pending) {
    var preview = pending && pending.repairPreview;
    if (!preview) return "";
    var details = shortcutPlanDetails(preview);
    var repair = shortcutRepairDetails(preview);
    var summary = details.summary;
    var issues = shortcutIssues(preview);
    var catalog = firstObject(preview.catalog_change);
    var mediaMovePlanned = preview.media_move_planned === true;
    var ready = !!(preview.ready && repair.planId && !issues.length);
    return (
      '<section class="organizer-section organizer-shortcut-pending organizer-repair-preview">' +
      '<div class="organizer-plan-head"><div><h2>' +
      (mediaMovePlanned
        ? "数据库修复 + 媒体归类 + 快捷方式"
        : "补录数据库并创建快捷方式") +
      "</h2>" +
      (mediaMovePlanned
        ? "<p>此计划将先写入数据库，再执行当前已核对的媒体归类，最后增量创建快捷方式。</p></div>"
        : "<p>此计划只写作品数据库并增量创建快捷方式，不会移动任何媒体文件。</p></div>") +
      '<span class="organizer-ready ' +
      (ready ? "is-ready" : "is-blocked") +
      '">' +
      (ready ? "可执行" : "不可执行") +
      "</span></div>" +
      '<dl class="organizer-assignment-meta"><div><dt>数据库动作</dt><dd>' +
      esc(catalog.action || "-") +
      "</dd></div><div><dt>修复确认 ID</dt><dd><code>" +
      esc(repair.planId || "-") +
      "</code></dd></div><div><dt>快捷方式</dt><dd>计划 " +
      esc(summary.planned_count || 0) +
      "，已存在 " +
      esc(summary.already_exists_count || 0) +
      "，冲突 " +
      esc(summary.conflict_count || 0) +
      "</dd></div></dl>" +
      renderShortcutIssues(issues) +
      '<div class="organizer-pending-actions organizer-pending-actions-primary">' +
      '<button type="button" class="btn secondary" data-organizer-action="shortcut-repair-repreview"' +
      (state.busy ? " disabled" : "") +
      ">" +
      (state.busy === "shortcut-repair-preview" ? "预览中…" : "重新预览修复计划") +
      "</button>" +
      (pending.repairDraft
        ? '<button type="button" class="btn secondary" data-organizer-action="shortcut-repair-back"' +
          (state.busy ? " disabled" : "") +
          ">返回修改补录信息</button>"
        : "") +
      '<button type="button" class="btn danger" data-organizer-action="shortcut-repair-apply"' +
      (ready && !state.busy ? "" : " disabled") +
      ">" +
      (state.busy === "shortcut-repair-apply"
        ? mediaMovePlanned
          ? "执行中…"
          : "补录中…"
        : mediaMovePlanned
        ? "确认修复、归类并创建快捷方式"
        : "确认补录数据库并创建快捷方式") +
      "</button>" +
      '<button type="button" class="btn secondary" data-organizer-action="shortcut-retry-cancel"' +
      (state.busy ? " disabled" : "") +
      ">返回原预览</button></div>" +
      renderShortcutRows(details.rows) +
      "</section>"
    );
  }

  function renderShortcutPending() {
    var pending = state.shortcutPending;
    if (!pending) return "";
    if (pending.repairPreview) return renderShortcutRepairPreview(pending);
    if (pending.repairDraft) return renderShortcutRepairEditor(pending);
    var retry = pending.preview;
    var retryDetails = shortcutPlanDetails(retry);
    var summary = retryDetails.summary;
    var rows = retryDetails.rows;
    var issues = shortcutIssues(retry);
    var retryPlanId = firstString(retry && retry.retry_plan_id, retryDetails.planId);
    var plannedCount = Number(summary.planned_count) || 0;
    var existingCount = Number(summary.already_exists_count) || 0;
    var conflictCount = Number(summary.conflict_count) || 0;
    var totalCount = Number(summary.total_count) || rows.length;
    var alreadyComplete = !!(
      retry &&
      retry.ready &&
      totalCount > 0 &&
      plannedCount === 0 &&
      conflictCount === 0 &&
      existingCount >= totalCount
    );
    var disabled = state.busy ? " disabled" : "";
    var canApply = !!(
      retry && retry.ready && retryPlanId && plannedCount > 0 && !state.busy
    );
    var normalFlow = pending.kind === "organizer";
    var landingFlow = pending.kind === "landing";
    var readyState = alreadyComplete || canApply;
    return (
      '<section class="organizer-section organizer-shortcut-pending">' +
      '<div class="organizer-plan-head"><div><h2>' +
      (alreadyComplete
        ? "快捷方式检查完成"
        : normalFlow
        ? "媒体归类已完成，快捷方式待补建"
        : landingFlow
        ? "数据库和媒体归类已完成，快捷方式待补建"
        : "快捷方式检查与补建") +
      "</h2>" +
      (alreadyComplete
        ? '<p>数据库权威快捷方式均已存在，不需要再次移动媒体或写入链接。</p></div>'
        : normalFlow || landingFlow
        ? '<p>不要重新执行媒体移动；这里只会增量检查并创建缺少的快捷方式。</p></div>'
        : '<p>这里只检查或增量创建快捷方式，不会移动媒体文件。</p></div>') +
      '<span class="organizer-ready ' +
      (readyState ? "is-ready" : "is-blocked") +
      '">' +
      (alreadyComplete ? "已完整" : canApply ? "可补建" : "待处理") +
      "</span></div>" +
      (pending.error && !issues.length
        ? '<p class="organizer-pending-error">' + esc(pending.error) + "</p>"
        : "") +
      (retry
        ? '<p>重试确认 ID：<code>' +
          esc(retryPlanId || "-") +
          "</code>；计划 " +
          esc(plannedCount) +
          "，已存在 " +
          esc(existingCount) +
          "，冲突 " +
          esc(conflictCount) +
          "。</p>"
        : "") +
      renderShortcutIssues(issues) +
      renderShortcutRepairChoices(pending, retry, issues) +
      '<div class="organizer-pending-actions organizer-pending-actions-primary">' +
      '<button type="button" class="btn secondary" data-organizer-action="shortcut-retry-preview"' +
      disabled +
      ">" +
      (state.busy === "shortcut-retry-preview" ? "检查中…" : "重新检查快捷方式") +
      "</button>" +
      '<button type="button" class="btn danger" data-organizer-action="shortcut-retry-apply"' +
      (canApply ? "" : " disabled") +
      ">" +
      (state.busy === "shortcut-retry-apply" ? "补建中…" : "确认补建快捷方式") +
      '</button><button type="button" class="btn secondary" data-organizer-action="shortcut-retry-cancel"' +
      disabled +
      ">返回原预览</button></div>" +
      renderShortcutRows(rows) +
      "</section>"
    );
  }

  function renderRecovery() {
    var recovery = state.recovery;
    if (!recovery) return "";
    return (
      '<section class="organizer-execution-result" aria-live="polite">' +
      "<h2>执行未完成：恢复信息</h2>" +
      '<p class="organizer-notice is-error">' + esc(recovery.error || "执行未完整回滚。") + "</p>" +
      "<p>作品根目录：<code>" + esc(recovery.root || state.root) + "</code></p>" +
      "<p>请先核对文件的原位置、移动后位置及数据库备份，再重新预览。下列信息保留自失败响应；不会自动重试移动或快捷方式。</p>" +
      "<details><summary>查看恢复详情（文件位置与数据库备份）</summary><pre>" +
      esc(JSON.stringify(recovery, null, 2)) +
      "</pre></details></section>"
    );
  }

  function handlePartialApply(out) {
    if (!out.data || out.data.state !== "partial") return false;
    var recovery = cloneJson(out.data);
    resetPlanScopedStateForRootChange(state.root, true);
    state.recovery = recovery;
    state.dirty = true;
    state.notice = "执行未完成，旧计划已作废。请先查看下方恢复信息并核对实际文件、数据库，再重新预览。";
    state.noticeError = true;
    render();
    setSharedStatus(state.notice, true);
    return true;
  }

  function renderExecution(execution) {
    if (!execution) return "";
    var warnings = Array.isArray(execution.cleanup_warnings) ? execution.cleanup_warnings : [];
    return (
      '<section class="organizer-execution-result" aria-live="polite">' +
      "<h2>" +
      (state.shortcutPending ? "媒体移动已完成" : "执行完成") +
      "</h2>" +
      "<p>已移动 <strong>" +
      esc(execution.moved_file_count || 0) +
      "</strong> 个文件，共 " +
      esc(formatBytes(execution.moved_bytes || 0)) +
      "；清理 <strong>" +
      esc(execution.cleaned_directory_count || 0) +
      "</strong> 个空目录。</p>" +
      (warnings.length
        ? '<ul class="organizer-issue-list">' +
          warnings
            .map(function (warning) {
              return "<li>" + esc(warning) + "</li>";
            })
            .join("") +
          "</ul>"
        : "") +
      "</section>"
    );
  }

  function planCanExecute() {
    var plan = state.plan;
    if (state.landing) {
      return !!(
        state.landing.ready &&
        state.landing.landing_plan_id &&
        plan &&
        plan.ready &&
        !state.dirty &&
        !state.busy
      );
    }
    if (isAlreadyOrganizedPlan(plan)) return false;
    var issues = visiblePlanIssues(plan);
    var unresolved = plan && Array.isArray(plan.unresolved_files)
      ? plan.unresolved_files
      : [];
    var shortcutDetails = shortcutPlanDetails(plan);
    var shortcutConflicts = Number(shortcutDetails.summary.conflict_count) || 0;
    return !!(
      plan &&
      plan.ready &&
      plan.plan_id &&
      shortcutDetails.planId &&
      shortcutConflicts === 0 &&
      !issues.length &&
      !unresolved.length &&
      !state.dirty &&
      !state.busy &&
      !state.shortcutPending
    );
  }

  function renderPlan(plan) {
    if (!plan) {
      return '<p class="organizer-empty">输入作品根目录后点击“重新预览”。预览只读取磁盘，不会移动文件。</p>';
    }
    var familyWorks = Array.isArray(plan.family_works) ? plan.family_works : [];
    var settledState = alreadyOrganizedShortcutState(plan);
    var planStatusLabel = settledState
      ? settledState === "complete"
        ? "已完成"
        : settledState === "pending"
        ? "仅待快捷方式"
        : "需修复"
      : plan.ready
      ? "可执行"
      : "不可执行";
    var planStatusReady = settledState === "complete" || (!settledState && plan.ready);
    return (
      '<section class="organizer-plan" aria-label="目录整理计划">' +
      '<div class="organizer-plan-head">' +
      "<div><h2>计划摘要</h2><p>计划 ID：<code>" +
      esc(plan.plan_id || "-") +
      "</code></p></div>" +
      '<span class="organizer-ready ' +
      (planStatusReady ? "is-ready" : "is-blocked") +
      '">' +
      planStatusLabel +
      "</span>" +
      "</div>" +
      '<div class="organizer-summary-grid">' +
      summaryCards(plan) +
      "</div>" +
      (familyWorks.length
        ? '<p class="organizer-family"><strong>数据库作品：</strong>' +
          esc(familyWorks.join(" / ")) +
          "</p>"
        : "") +
      renderSourceWorkBindings(plan) +
      '<section class="organizer-section"><h2>来源与目标</h2>' +
      renderAssignments(plan) +
      "</section>" +
      renderPlanShortcutSummary(plan) +
      '<section class="organizer-section"><h2>未决文件</h2>' +
      renderUnresolvedFiles(plan) +
      "</section>" +
      '<section class="organizer-section"><h2>问题</h2>' +
      renderIssues(plan) +
      "</section>" +
      "</section>"
    );
  }

  function render() {
    var target = view();
    if (!target) return;
    renderedMoveViews = Object.create(null);
    var config = state.config || {};
    var paths = config.paths && typeof config.paths === "object" ? config.paths : {};
    var roots = allowedRoots(config);
    var executeDisabled = planCanExecute() ? "" : " disabled";
    var previewDisabled = state.busy || state.shortcutPending ? " disabled" : "";
    var inputDisabled = state.busy || state.shortcutPending ? " disabled" : "";
    var registrationRequired = !!(
      state.plan && state.plan.registration_required && landingDrafts().length
    );
    var shortcutOnlyDisabled =
      state.busy ||
      state.landing ||
      state.shortcutPending ||
      state.draftWorks.length > 1 ||
      sharedTargetState().confirmed ||
      !String(state.root || "").trim()
        ? " disabled"
        : "";
    var shortcutOnlyAction = "shortcut-only-preview";
    var integratedRepairAvailable = !!(
      shortcutRepairDetails(state.plan).required && canRepairWithCurrentMediaPlan(state.plan)
    );
    var shortcutOnlyLabel = sharedTargetState().confirmed
      ? "共享记录请使用完整落地"
      : state.draftWorks.length > 1
      ? "多作品请使用完整落地"
      : registrationRequired
      ? "检查 DB 候选 / 补录快捷方式（不移动）"
      : integratedRepairAvailable
      ? "修复数据库 + 整理媒体 + 快捷方式"
      : "检查/补建快捷方式";
    var noticeClass = state.noticeError ? " is-error" : "";
    target.innerHTML =
      '<section class="organizer-panel">' +
      '<header class="organizer-header">' +
      "<div><h1>媒体目录整理</h1>" +
      "<p>先预览并逐项核对目标目录和快捷方式；执行时不会改文件名，也不会覆盖已有文件。</p></div>" +
      '<button type="button" class="btn secondary sm" data-organizer-action="reload-config"' +
      previewDisabled +
      ">重读配置</button>" +
      "</header>" +
      '<div class="organizer-config-meta">' +
      "<p><strong>作品数据库：</strong><code>" +
      esc(paths.catalog_root || "（配置尚未加载）") +
      "</code></p>" +
      "<p><strong>允许范围：</strong>" +
      (roots.length
        ? roots
            .map(function (root) {
              return "<code>" + esc(root) + "</code>";
            })
            .join(" ")
        : "（未限制或配置尚未加载）") +
      "</p>" +
      "</div>" +
      '<div class="organizer-root-row">' +
      '<label for="organizer-root-input">作品根目录</label>' +
      '<input id="organizer-root-input" type="text" value="' +
      esc(state.root) +
      '" autocomplete="off" spellcheck="false" placeholder="例如 U:\\Little Busters!"' +
      inputDisabled +
      " />" +
      '<button type="button" class="btn secondary" data-organizer-action="resource-picker-open"' +
      inputDisabled +
      ">从资源库选择</button>" +
      '<button type="button" class="btn" data-organizer-action="preview"' +
      previewDisabled +
      ">" +
      (state.busy === "preview" ? "预览中..." : "重新预览") +
      '</button><button type="button" class="btn secondary" data-organizer-action="' +
      shortcutOnlyAction +
      '"' +
      shortcutOnlyDisabled +
      ">" +
      shortcutOnlyLabel +
      "</button>" +
      "</div>" +
      '<p class="organizer-dirty"' +
      (state.dirty ? "" : " hidden") +
      ">" +
      (state.recovery
        ? "上次执行未完成；请先核对恢复信息，再重新预览，旧计划不能执行。"
        : "目标目录或作品根目录已经编辑；必须重新预览，当前计划不能执行。") +
      "</p>" +
      '<p class="organizer-notice' +
      noticeClass +
      '" aria-live="polite">' +
      esc(state.notice) +
      "</p>" +
      renderRecovery() +
      renderExecution(state.execution) +
      renderShortcutPending() +
      renderWorkRegistration(state.plan) +
      renderLandingSummary() +
      renderPlan(state.plan) +
      (isAlreadyOrganizedPlan(state.plan)
        ? '<section class="organizer-execute-panel"><div><h2>媒体操作</h2>' +
          '<p>当前目录已经整理完成，不会再次启用媒体移动；如有缺失，只处理快捷方式。</p></div></section>'
        : '<section class="organizer-execute-panel">' +
          "<div><h2>执行计划</h2><p>执行前依次核对摘要、输入完整计划 ID，并进行最终确认。</p></div>" +
          '<button type="button" class="btn danger" data-organizer-action="apply"' +
          executeDisabled +
          ">" +
          (state.busy === "apply"
            ? "执行中..."
            : state.landing
            ? "确认完整落地"
            : "执行移动并创建快捷方式") +
          "</button>" +
          "</section>") +
      renderResourcePicker() +
      "</section>";
  }

  function setNotice(message, isError) {
    state.notice = message || "";
    state.noticeError = !!isError;
    render();
  }

  function invalidatePreview(message) {
    state.dirty = true;
    state.execution = null;
    state.notice = message || "已编辑，请重新预览。";
    state.noticeError = false;
    var target = view();
    if (!target) return;
    var dirty = target.querySelector(".organizer-dirty");
    if (dirty) dirty.hidden = false;
    var notice = target.querySelector(".organizer-notice");
    if (notice) {
      notice.classList.remove("is-error");
      notice.textContent = state.notice;
    }
    var applyButton = target.querySelector('[data-organizer-action="apply"]');
    if (applyButton) applyButton.disabled = true;
    var landingSummary = target.querySelector(".organizer-landing-summary");
    if (landingSummary && !state.landing) landingSummary.hidden = true;
  }

  function unresolvedItemsForSource(sourceDir) {
    var unresolved = state.plan && Array.isArray(state.plan.unresolved_files)
      ? state.plan.unresolved_files
      : [];
    return unresolved.filter(function (item) {
      return firstString(item.source_dir, item.source) === sourceDir;
    });
  }

  function clearFileWorkOverridesForSource(sourceDir) {
    unresolvedItemsForSource(sourceDir).forEach(function (item) {
      var source = firstString(item.source);
      if (source) delete state.fileWorkOverrides[source];
    });
  }

  function selectSourceWorkMode(sourceDir, rawChoice) {
    sourceDir = String(sourceDir || "").trim();
    var choice = String(rawChoice || "").trim();
    if (!sourceDir || !unresolvedItemsForSource(sourceDir).length) return "";

    clearFileWorkOverridesForSource(sourceDir);
    if (choice === MULTIPLE_WORKS_VALUE) {
      delete state.sourceWorkOverrides[sourceDir];
      state.multipleWorkSources[sourceDir] = true;
      state.dirty = true;
      state.execution = null;
      state.notice = "已确认该来源目录包含多个作品；现在才需要逐文件选择。";
      state.noticeError = false;
      render();
      return "multiple";
    }

    delete state.multipleWorkSources[sourceDir];
    if (!choice) {
      delete state.sourceWorkOverrides[sourceDir];
      invalidatePreview("已清除目录作品判断，请重新选择单作品或多作品。");
      render();
      return "cleared";
    }

    state.sourceWorkOverrides[sourceDir] = choice;
    invalidatePreview(
      "已将整个来源目录指定为“" + choice + "”，正在重新生成安全预览。",
    );
    return "single";
  }

  function setSourceMultipleWorkMode(sourceDir, enabled) {
    sourceDir = String(sourceDir || "").trim();
    if (!sourceDir || !unresolvedItemsForSource(sourceDir).length) return false;
    if (enabled) {
      var row = sourceWorkBindingRows(state.plan).find(function (candidate) {
        return pathKey(candidate.sourcePath) === pathKey(sourceDir) ||
          candidate.sourceName.toLocaleLowerCase() === sourceDir.toLocaleLowerCase();
      });
      if (row) {
        delete sourceWorkBindingEdits()[row.sourceName];
        delete sourceWorkDrafts()[row.sourceName];
        if (state.sourceWorkEditorSource === row.sourceName) {
          state.sourceWorkEditorSource = "";
          state.sourceWorkEditorMode = "";
          invalidateLandingRecognition("", "");
        }
      }
      state.landing = null;
      state.landingSourceWorkBindings = null;
      selectSourceWorkMode(sourceDir, MULTIPLE_WORKS_VALUE);
      return true;
    }
    clearFileWorkOverridesForSource(sourceDir);
    delete state.sourceWorkOverrides[sourceDir];
    delete state.multipleWorkSources[sourceDir];
    invalidatePreview("已恢复为单作品目录；请使用上方一级目录作品绑定并重新预览。");
    previewPlan();
    return true;
  }

  function findResourceFolder(node, relpath) {
    if (!node || typeof node !== "object") return null;
    if (node.type === "folder" && String(node.relpath || "") === String(relpath || "")) {
      return node;
    }
    var children = resourceFolderChildren(node);
    for (var i = 0; i < children.length; i++) {
      var found = findResourceFolder(children[i], relpath);
      if (found) return found;
    }
    return null;
  }

  function closeResourcePicker() {
    resourcePickerSerial += 1;
    state.resourcePicker = createResourcePickerState(false);
    render();
  }

  async function openResourcePicker() {
    var serial = ++resourcePickerSerial;
    state.resourcePicker = createResourcePickerState(true);
    state.resourcePicker.loading = true;
    render();
    try {
      var out = await fetchJson("/api/collection-detail/resource-libraries/cache", { method: "GET" });
      if (serial !== resourcePickerSerial || !state.resourcePicker.open) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "资源库缓存读取失败"));
      }
      state.resourcePicker.cache = out.data;
      state.resourcePicker.current = resourcePickerTree(out.data);
      state.resourcePicker.loading = false;
      render();
      window.setTimeout(function () {
        var search = document.getElementById("organizer-resource-picker-search-input");
        if (search) search.focus();
      }, 0);
    } catch (error) {
      if (serial !== resourcePickerSerial || !state.resourcePicker.open) return;
      state.resourcePicker.loading = false;
      state.resourcePicker.error = error && error.message ? error.message : String(error);
      render();
    }
  }

  async function openResourcePickerFolder(relpath) {
    var picker = state.resourcePicker;
    relpath = String(relpath || "");
    if (!picker || !picker.open || !relpath || picker.loading || picker.searching) return;
    var fromSearch = !!picker.searchQuery;
    var candidate = fromSearch
      ? findResourceFolder(resourcePickerTree(picker.searchResults), relpath)
      : findResourceFolder(picker.current, relpath);
    var serial = ++resourcePickerSerial;
    picker.loading = true;
    picker.error = "";
    render();
    try {
      var node = candidate;
      if (fromSearch || !node || node.children_loaded !== true) {
        var params = new URLSearchParams();
        params.set("relpath", relpath);
        var out = await fetchJson(
          "/api/collection-detail/resource-libraries/node?" + params.toString(),
          { method: "GET" },
        );
        if (serial !== resourcePickerSerial || !state.resourcePicker.open) return;
        if (!out.res.ok || !out.data || out.data.ok === false || !out.data.node) {
          throw new Error(responseError(out, "资源库目录读取失败"));
        }
        node = out.data.node;
      }
      if (serial !== resourcePickerSerial || !state.resourcePicker.open) return;
      picker.history.push(picker.current || resourcePickerTree(picker.cache));
      picker.current = node;
      picker.loading = false;
      picker.searchDraft = "";
      picker.searchQuery = "";
      picker.searchResults = null;
      render();
    } catch (error) {
      if (serial !== resourcePickerSerial || !state.resourcePicker.open) return;
      picker.loading = false;
      picker.error = error && error.message ? error.message : String(error);
      render();
    }
  }

  function backResourcePickerFolder() {
    var picker = state.resourcePicker;
    if (!picker || !picker.open || !picker.history.length || picker.loading || picker.searching) return;
    picker.current = picker.history.pop();
    picker.error = "";
    picker.searchDraft = "";
    picker.searchQuery = "";
    picker.searchResults = null;
    render();
  }

  function clearResourcePickerSearch() {
    var picker = state.resourcePicker;
    if (!picker || !picker.open || picker.loading || picker.searching) return;
    picker.searchDraft = "";
    picker.searchQuery = "";
    picker.searchResults = null;
    picker.error = "";
    render();
  }

  async function searchResourcePicker() {
    var picker = state.resourcePicker;
    if (!picker || !picker.open || picker.loading || picker.searching) return;
    var query = String(picker.searchDraft || "").trim();
    if (!query) {
      clearResourcePickerSearch();
      return;
    }
    if (picker.cache && !picker.cache.cached) {
      picker.error = "暂无可搜索的资源库缓存，请先到“资源库目录”完成扫描。";
      render();
      return;
    }
    var serial = ++resourcePickerSerial;
    picker.searching = true;
    picker.error = "";
    render();
    try {
      var params = new URLSearchParams();
      params.set("q", query);
      var out = await fetchJson(
        "/api/collection-detail/resource-libraries/search?" + params.toString(),
        { method: "GET" },
      );
      if (serial !== resourcePickerSerial || !state.resourcePicker.open) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "资源库目录搜索失败"));
      }
      picker.searchResults = out.data;
      picker.searchQuery = query;
      picker.searching = false;
      render();
    } catch (error) {
      if (serial !== resourcePickerSerial || !state.resourcePicker.open) return;
      picker.searching = false;
      picker.error = error && error.message ? error.message : String(error);
      render();
    }
  }

  function selectResourcePickerPath(path) {
    path = String(path || "").trim();
    if (!path || !pathAllowedByOrganizer(path)) {
      state.resourcePicker.error = "该目录不在当前整理器允许范围内，不能作为作品根目录。";
      render();
      return;
    }
    resourcePickerSerial += 1;
    var rootChanged = resetPlanScopedStateForRootChange(path);
    state.resourcePicker = createResourcePickerState(false);
    if (rootChanged) {
      invalidatePreview("已从资源库选择新的作品根目录，请重新预览。当前未移动任何文件。");
    } else {
      state.notice = "仍使用当前作品根目录；现有预览和编辑保持不变。";
      state.noticeError = false;
    }
    render();
  }

  function goToResourceLibrary() {
    closeResourcePicker();
    var mainTab = document.getElementById("tab-collection-detail");
    if (mainTab) mainTab.click();
    window.setTimeout(function () {
      var resourceTab = document.querySelector('[data-collection-detail-subtab="resource"]');
      if (resourceTab) resourceTab.click();
    }, 0);
  }

  function guideUnresolvedCatalog(sourceDir) {
    var source = firstString(sourceDir);
    state.notice =
      "该来源目录存在尚未入库的作品候选。请在“作品数据”补录作品，path 填写当前作品根目录 " +
      state.root +
      (source ? "；来源目录：" + source : "") +
      "。保存后返回“目录整理”重新预览。";
    state.noticeError = false;
    render();
    setSharedStatus(state.notice, false);
    var mainTab = document.getElementById("tab-collection-detail");
    if (mainTab) mainTab.click();
    window.setTimeout(function () {
      var listTab = document.querySelector('[data-collection-detail-subtab="list"]');
      if (listTab) listTab.click();
    }, 0);
  }

  async function loadConfigAndPreview(useConfiguredRoot) {
    var serial = ++requestSerial;
    state.busy = "config";
    state.notice = "正在读取目录整理配置...";
    state.noticeError = false;
    render();
    setSharedStatus("目录整理配置读取中...", false);
    try {
      var out = await fetchJson("/api/media-directory-organizer/config", { method: "GET" });
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "目录整理配置读取失败"));
      }
      state.config = out.data;
      var configuredRoot = configDefaultRoot(out.data);
      if ((useConfiguredRoot || !state.root.trim()) && configuredRoot) {
        resetPlanScopedStateForRootChange(configuredRoot);
      }
      state.busy = "";
      state.notice = configuredRoot
        ? "配置已加载，正在生成安全预览..."
        : "配置已加载。请输入作品根目录后生成预览。";
      render();
      if (state.root.trim()) {
        await previewPlan();
      } else {
        setSharedStatus("目录整理配置已加载；请输入作品根目录。", false);
      }
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.notice = error && error.message ? error.message : String(error);
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  async function previewPlan() {
    var root = String(state.root || "").trim();
    if (!root) {
      setNotice("请先输入作品根目录。", true);
      return;
    }
    var plannedRoot = firstString(state.plan && state.plan.root).trim();
    if (plannedRoot && pathKey(plannedRoot) !== pathKey(root)) {
      resetPlanScopedStateForRootChange(root, true);
    }
    invalidateLandingRecognition(recognitionState().query, "");
    var serial = ++requestSerial;
    state.busy = "preview";
    state.notice = "正在扫描目录并重新计算计划...";
    state.noticeError = false;
    state.execution = null;
    var sourceBindings = sourceWorkBindingsPayload();
    render();
    setSharedStatus("目录整理计划预览中...", false);
    try {
      var out = await fetchJson("/api/media-directory-organizer/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({
          root: root,
          target_overrides: legacyTargetOverrides(),
          route_target_overrides: state.routeOverrides,
          file_work_overrides: state.fileWorkOverrides,
          source_work_overrides: sourceWorkOverridesPayload(sourceBindings),
          source_work_bindings: sourceBindings,
          source_press_overrides: sourcePressOverridesPayload(),
        }),
      });
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false || !out.data.plan) {
        throw new Error(responseError(out, "目录整理预览失败"));
      }
      state.plan = out.data.plan;
      state.landing = null;
      state.sharedTarget = createSharedTargetState();
      var sharedCandidateSnapshot = sharedTargetCandidateBundle(state.plan, false);
      if (sharedCandidateSnapshot.candidates.length) {
        state.sharedTarget.candidateSnapshot = sharedCandidateSnapshot;
      }
      if (
        state.plan.registration_required &&
        state.plan.registration &&
        Array.isArray(state.plan.registration.work_drafts) &&
        state.plan.registration.work_drafts.length > 1
      ) {
        state.draftWorks = cloneJson(state.plan.registration.work_drafts);
        state.draftWork = null;
        state.recognition = createRecognitionState("");
      } else if (
        state.plan.registration_required &&
        state.plan.registration &&
        state.plan.registration.draft
      ) {
        state.draftWork = cloneJson(state.plan.registration.draft);
        state.draftWorks = [];
        state.recognition = createRecognitionState(state.draftWork.name || "");
      } else {
        state.draftWork = null;
        state.draftWorks = [];
        state.recognition = createRecognitionState("");
      }
      state.root = firstString(state.plan.root, root);
      state.dirty = false;
      state.busy = "";
      var planIssues = visiblePlanIssues(state.plan);
      var issueCount = planIssues.length;
      var unresolvedCount = Array.isArray(state.plan.unresolved_files)
        ? state.plan.unresolved_files.length
        : 0;
      var shortcutDetails = shortcutPlanDetails(state.plan);
      var shortcutConflicts = Number(shortcutDetails.summary.conflict_count) || 0;
      var settledShortcutState = alreadyOrganizedShortcutState(state.plan);
      var shortcutBlocked = !!(
        !state.plan.registration_required &&
        !settledShortcutState &&
        (!shortcutDetails.planId || shortcutConflicts > 0)
      );
      state.notice = settledShortcutState
        ? settledShortcutState === "complete"
          ? "媒体归类和快捷方式均已完成。"
          : settledShortcutState === "pending"
          ? "媒体已整理，仅快捷方式待补建。请使用“检查并补建快捷方式”；不会再次执行媒体移动。"
          : "媒体已经整理，但快捷方式仍被数据库或目标目录问题阻止：" +
            (firstIssueMessage(planIssues) || "请核对下方具体问题。")
        : issueCount || unresolvedCount || shortcutBlocked
        ? state.plan.registration_required
          ? sharedCandidateSnapshot.supported && sharedCandidateSnapshot.candidates.length >= 2
            ? "找到多个已有播出记录候选。若它们确实共用一个实体目录，请在下方明确勾选、选择精确压制记录并分配来源；否则继续使用新增作品表单。"
            : state.draftWorks.length > 1
            ? "数据库中没有找到这些作品。已按来源识别为多个作品，请逐部分别确认正式日期和压制信息，再生成一次完整落地预览。"
            : "数据库中没有找到该作品。请填写新增作品信息，再生成完整落地预览；当前没有写入或移动任何内容。"
          : shortcutBlocked && !issueCount && !unresolvedCount
          ? "媒体预览完成，但快捷方式计划缺失或存在冲突；当前不能执行。可核对快捷方式路径后重新预览。"
          : "预览完成，但存在 " +
            issueCount +
            " 个问题、" +
            unresolvedCount +
            " 个未决文件；修正并重新预览后才能执行。"
        : "预览完成。请逐项核对来源、目标目录和快捷方式路径。";
      state.noticeError = settledShortcutState
        ? settledShortcutState === "blocked"
        : !state.plan.registration_required &&
          (issueCount > 0 || unresolvedCount > 0 || shortcutBlocked);
      render();
      setSharedStatus(state.notice, state.noticeError);
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.dirty = true;
      state.notice = error && error.message ? error.message : String(error);
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  async function runLandingRecognition() {
    var root = String(state.root || "").trim();
    var draft = landingRecognitionDraft();
    var query = String(recognitionState().query || "").trim();
    if (!root || !draft) {
      setNotice("请先输入作品根目录，并通过普通预览生成新增作品表单。", true);
      return;
    }
    if (!query) {
      state.recognition.error = "请先填写用于智能识别的搜索词。";
      render();
      return;
    }
    var payload = landingRecognitionPayload(query);
    var snapshot = landingRecognitionSnapshot(payload);
    var serial = ++recognitionSerial;
    state.recognition.loading = true;
    state.recognition.error = "";
    state.recognition.notice = "";
    state.recognition.results = null;
    state.recognition.inputFingerprint = "";
    state.recognition.requestSnapshot = snapshot;
    render();
    setSharedStatus(
      bangumiRecognitionEligible(payload.draft_work)
        ? "正在本地识别并搜索 Bangumi 候选…"
        : "正在本地识别作品与整理目标…",
      false,
    );
    try {
      var out = await fetchJson("/api/media-directory-organizer/landing/suggest", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify(payload),
      });
      if (serial !== recognitionSerial) return;
      if (landingRecognitionSnapshot(landingRecognitionPayload(recognitionState().query)) !== snapshot) {
        invalidateLandingRecognition(
          recognitionState().query,
          "作品信息或搜索词已变化，旧识别结果已作废；请重新识别。",
        );
        render();
        return;
      }
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "智能识别失败"));
      }
      state.recognition.loading = false;
      state.recognition.results = out.data;
      state.recognition.inputFingerprint = String(out.data.input_fingerprint || "");
      state.recognition.requestSnapshot = snapshot;
      state.recognition.query = query;
      var candidates = Array.isArray(out.data.candidates) ? out.data.candidates : [];
      state.notice = candidates.length
        ? "智能识别已返回 " + candidates.length + " 个候选。填入候选只会修改表单，仍需重新生成完整落地预览。"
        : "智能识别完成，但没有找到可用候选；当前未写入或移动任何内容。";
      state.noticeError = false;
      render();
      setSharedStatus(state.notice, false);
    } catch (error) {
      if (serial !== recognitionSerial) return;
      state.recognition.loading = false;
      state.recognition.error = error && error.message ? error.message : String(error);
      state.recognition.results = null;
      state.recognition.inputFingerprint = "";
      render();
      setSharedStatus(state.recognition.error, true);
    }
  }

  function applyLandingRecognitionCandidate(candidateId) {
    candidateId = String(candidateId || "");
    var recognition = recognitionState();
    var results = recognition.results && typeof recognition.results === "object"
      ? recognition.results
      : null;
    var candidates = results && Array.isArray(results.candidates) ? results.candidates : [];
    var candidate = candidates.find(function (item) {
      return String((item && item.candidate_id) || "") === candidateId;
    });
    var currentSnapshot = landingRecognitionSnapshot(
      landingRecognitionPayload(recognition.query),
    );
    var responseFingerprint = String(recognition.inputFingerprint || "");
    var candidateFingerprint = String((candidate && candidate.input_fingerprint) || "");
    if (
      !candidate ||
      !candidateId ||
      !recognition.requestSnapshot ||
      currentSnapshot !== recognition.requestSnapshot ||
      !responseFingerprint ||
      candidateFingerprint !== responseFingerprint
    ) {
      invalidateLandingRecognition(
        recognition.query,
        "作品信息、根目录或搜索词已经变化，拒绝采用旧候选；请重新识别。",
      );
      render();
      return;
    }
    var proposed = candidate.proposed_work;
    if (!proposed || typeof proposed !== "object" || !Array.isArray(proposed.presses) || !proposed.presses.length) {
      state.recognition.error = "候选没有包含完整的作品与压制草稿，不能填入。";
      render();
      return;
    }
    if (!proposed.path || pathKey(proposed.path) !== pathKey(state.root)) {
      invalidateLandingRecognition(
        recognition.query,
        "候选的作品目录与当前作品根目录不一致，已拒绝填入；请重新识别。",
      );
      render();
      return;
    }
    if (state.sourceWorkEditorSource) {
      var sourceName = state.sourceWorkEditorSource;
      var sourceDraft = cloneJson(candidate.proposed_work);
      sourceDraft.path = state.root;
      var sourceRow = sourceWorkBindingRow(sourceName);
      if (sourceRow) {
        sourceDraft = sourceBindingDraftTemplate(
          Object.assign({}, sourceRow, { registration: { draft: sourceDraft } }),
        );
      } else {
        sourceDraft.presses = sourceDraft.presses.slice(0, 1);
        sourceDraft.presses[0].source_names = [sourceName];
      }
      sourceWorkDrafts()[sourceName] = sourceDraft;
      sourceWorkBindingEdits()[sourceName] = {
        mode: "draft",
        draft_work: cloneJson(sourceDraft),
      };
      if (sourceRow && sourceRow.sourcePath) delete state.sourceWorkOverrides[sourceRow.sourcePath];
      state.sourceWorkEditorMode = "manual";
      state.landing = null;
      state.landingSourceWorkBindings = null;
      invalidateLandingRecognition(firstString(sourceDraft.name, recognition.query), "");
      invalidatePreview(
        "已把查找候选填入当前一级来源的完整作品表单；请核对后生成 DB + 归类 + 快捷方式预览。",
      );
      render();
      setSharedStatus(state.notice, false);
      return;
    }
    state.draftWork = cloneJson(candidate.proposed_work);
    state.draftWorks = [];
    state.sharedTarget = createSharedTargetState();
    state.landing = null;
    state.shortcutPending = null;
    invalidateLandingRecognition(firstString(state.draftWork.name, recognition.query), "");
    invalidatePreview(
      "已将智能识别候选填入表单；当前未写入数据库、未移动文件，也未创建快捷方式。请逐项核对并重新生成完整落地预览。",
    );
    render();
    setSharedStatus(state.notice, false);
  }

  async function previewLandingPlan() {
    var root = String(state.root || "").trim();
    var drafts = landingDrafts();
    var shared = sharedTargetState();
    var useSharedTargets = shared.confirmed === true;
    var useSourceBindingLanding = sourceBindingsNeedLanding();
    var sourceBindingPayload = sourceWorkBindingsPayload({
      forLanding: useSourceBindingLanding,
    });
    if (!root || (!drafts.length && !useSharedTargets && !useSourceBindingLanding)) {
      setNotice("请先输入作品根目录，并通过普通预览生成手填作品表单。", true);
      return;
    }
    var draft = state.draftWork ? cloneJson(state.draftWork) : null;
    var draftWorks = state.draftWorks.length ? cloneJson(state.draftWorks) : [];
    if (draft) draft.path = root;
    draftWorks.forEach(function (item) { item.path = root; });
    var requestPayload;
    if (useSharedTargets) {
      var sharedBundle = sharedTargetCandidateBundle(state.plan, true);
      if (!sharedBundle.candidates.length) {
        setNotice("共享实体目录候选已经失效，请重新扫描作品根目录。", true);
        return;
      }
      shared.candidateSnapshot = sharedBundle;
      var sharedResult = buildSharedTargetBindings(state.plan);
      if (sharedResult.error || !sharedResult.bindings.length) {
        setNotice(sharedResult.error || "共享实体目录绑定尚未填写完整。", true);
        return;
      }
      requestPayload = {
        root: root,
        shared_target_bindings: sharedResult.bindings,
        acknowledge_shared_targets: true,
      };
    } else if (useSourceBindingLanding) {
      var sourceBindingIssue = sourceWorkLandingPayloadIssue(sourceBindingPayload);
      if (sourceBindingIssue) {
        setNotice(sourceBindingIssue + "。请先完成每个一级目录的作品绑定。", true);
        return;
      }
      requestPayload = {
        root: root,
        source_work_bindings: sourceBindingPayload,
      };
    } else {
      requestPayload = draftWorks.length
        ? { root: root, draft_works: draftWorks }
        : { root: root, draft_work: draft };
    }
    var serial = ++requestSerial;
    state.busy = "landing-preview";
    state.notice = useSharedTargets
      ? "正在校验共享数据库记录、单一实体目录和多条快捷方式计划…"
      : useSourceBindingLanding
      ? "正在按每个一级来源重新校验数据库补录、媒体归类和快捷方式计划…"
      : "正在重新校验数据库写入、媒体归类和快捷方式计划…";
    state.noticeError = false;
    state.execution = null;
    render();
    setSharedStatus("完整落地计划预览中…", false);
    try {
      var out = await fetchJson("/api/media-directory-organizer/landing/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify(requestPayload),
      });
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false || !out.data.organizer_plan) {
        throw new Error(responseError(out, "完整落地预览失败"));
      }
      state.root = firstString(out.data.root, root);
      state.landing = out.data;
      state.plan = out.data.organizer_plan;
      state.draftWork = draft;
      state.draftWorks = draftWorks;
      state.landingSourceWorkBindings = useSourceBindingLanding
        ? cloneJson(
            nonEmptyObject(out.data.source_work_bindings) || sourceBindingPayload,
          )
        : null;
      state.routeOverrides = Object.create(null);
      state.fileWorkOverrides = Object.create(null);
      state.sourceWorkOverrides = Object.create(null);
      state.multipleWorkSources = Object.create(null);
      state.sourcePressOverrides = Object.create(null);
      state.dirty = false;
      state.busy = "";
      state.notice = out.data.ready
        ? useSharedTargets
          ? "共享实体目录预览已生成。请核对每条数据库记录、唯一实体目标和所有快捷方式，再确认执行。"
          : useSourceBindingLanding
          ? "分来源完整落地预览已生成。请核对已有 DB 绑定、新增记录、媒体路由和快捷方式诊断，再确认执行。"
          : "完整落地预览已生成。请核对数据库文件、每条媒体移动和每个快捷方式，再确认执行。"
        : "完整落地预览仍有问题或快捷方式冲突，当前不能执行。";
      state.noticeError = !out.data.ready;
      render();
      setSharedStatus(state.notice, state.noticeError);
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.landing = null;
      state.dirty = true;
      state.notice = error && error.message ? error.message : String(error);
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  function landingConfirmationSummary(landing) {
    var plan = landing.organizer_plan || {};
    var summary = plan.summary || {};
    var catalog = landing.catalog_change || {};
    var catalogs = Array.isArray(landing.catalog_changes) ? landing.catalog_changes : [];
    var catalogAction = catalogs.length
      ? catalogs.map(function (item) { return firstString(item && item.action, "-"); }).join(" / ")
      : String(catalog.action || "-");
    var catalogTarget = catalogs.length
      ? catalogs.map(function (item) { return firstString(item && item.target, "-"); }).join(" / ")
      : String(catalog.target || "-");
    var shortcuts = landing.shortcut_summary || {};
    var sharedSummary = firstObject(landing.shared_target_summary);
    var sourceWorkSummary = cloneJson(firstObject(
      landing.source_work_summary,
      landing.source_work_binding_summary,
    ));
    var landingSourceBindings = nonEmptyObject(landing.source_work_bindings);
    if (!Object.keys(sourceWorkSummary).length && landingSourceBindings) {
      var landingBindingValues = Object.keys(landingSourceBindings).map(function (sourceName) {
        return landingSourceBindings[sourceName] || {};
      });
      sourceWorkSummary = {
        source_count: landingBindingValues.length,
        catalog_bound_count: landingBindingValues.filter(function (binding) {
          return binding.mode === "catalog";
        }).length,
        registration_count: landingBindingValues.filter(function (binding) {
          return binding.mode === "draft";
        }).length,
        unresolved_count: 0,
      };
    }
    if (sourceWorkSummary.source_count == null && sourceWorkSummary.total_count != null) {
      sourceWorkSummary.source_count = Number(sourceWorkSummary.total_count) || 0;
    }
    if (
      sourceWorkSummary.catalog_bound_count == null &&
      (sourceWorkSummary.catalog_count != null || sourceWorkSummary.existing_work_count != null)
    ) {
      sourceWorkSummary.catalog_bound_count = Number(
        sourceWorkSummary.catalog_count == null
          ? sourceWorkSummary.existing_work_count
          : sourceWorkSummary.catalog_count,
      ) || 0;
    }
    if (
      sourceWorkSummary.registration_count == null &&
      (sourceWorkSummary.draft_count != null || sourceWorkSummary.new_work_count != null)
    ) {
      sourceWorkSummary.registration_count = Number(
        sourceWorkSummary.draft_count == null
          ? sourceWorkSummary.new_work_count
          : sourceWorkSummary.draft_count,
      ) || 0;
    }
    if (sourceWorkSummary.unresolved_count == null && sourceWorkSummary.source_count != null) {
      sourceWorkSummary.unresolved_count = Math.max(
        0,
        Number(sourceWorkSummary.source_count || 0) -
          Number(sourceWorkSummary.catalog_bound_count || 0) -
          Number(sourceWorkSummary.registration_count || 0),
      );
    }
    var sharedBindings = Array.isArray(landing.shared_target_bindings)
      ? landing.shared_target_bindings
      : [];
    var sharedWorkNames = [];
    sharedBindings.forEach(function (binding) {
      (Array.isArray(binding && binding.members) ? binding.members : []).forEach(function (member) {
        var name = firstString(member && member.work_name);
        if (name && sharedWorkNames.indexOf(name) < 0) sharedWorkNames.push(name);
      });
    });
    return (
      "请再次核对这次完整落地：\n\n" +
      "作品：" + (sharedWorkNames.length
        ? sharedWorkNames.join(" / ")
        : Array.isArray(landing.draft_works)
        ? landing.draft_works.map(function (work) { return firstString(work && work.name); }).join(" / ")
        : String((landing.draft_work && landing.draft_work.name) || "")) +
      "\n数据库动作：" + catalogAction +
      "\n数据库文件：" + catalogTarget +
      (sharedSummary.database_record_count == null
        ? ""
        : "\n共享关系：" +
          String(sharedSummary.database_record_count || 0) +
          " 条数据库记录 / " +
          String(sharedSummary.physical_target_count || 0) +
          " 个实体目录 / " +
          String(sharedSummary.shortcut_count || 0) +
          " 个快捷方式") +
      (sourceWorkSummary.source_count == null
        ? ""
        : "\n来源绑定：" +
          String(sourceWorkSummary.source_count || 0) +
          " 个一级目录（已有 " +
          String(sourceWorkSummary.catalog_bound_count || 0) +
          " / 新增 " +
          String(sourceWorkSummary.registration_count || 0) +
          " / 未决 " +
          String(sourceWorkSummary.unresolved_count || 0) +
          "）") +
      "\n移动文件：" + String(summary.file_count || 0) +
      " 个，共 " + formatBytes(summary.bytes || 0) +
      "\n快捷方式：" + String(shortcuts.planned_count || 0) +
      " 个（冲突 " + String(shortcuts.conflict_count || 0) + "）" +
      "\n完整落地 ID：" + String(landing.landing_plan_id || "") +
      "\n\n这是第一次确认。点击“确定”后仍不会立即写入或移动。"
    );
  }

  async function applyCurrentLanding() {
    var landing = state.landing;
    if (!landing || !planCanExecute()) {
      setNotice("当前完整落地计划不可执行，请重新生成并解决所有问题。", true);
      return;
    }
    if (!window.confirm(landingConfirmationSummary(landing))) return;
    var typed = window.prompt(
      "第二步：请输入完整落地 ID，确认你核对的是当前 DB、归类和快捷方式预览。\n\n" +
        landing.landing_plan_id,
      "",
    );
    if (typed === null) return;
    if (String(typed).trim() !== String(landing.landing_plan_id)) {
      setNotice("完整落地 ID 输入不一致，未写数据库、未移动文件、未创建快捷方式。", true);
      return;
    }
    if (
      !window.confirm(
        (Array.isArray(landing.shared_target_bindings) && landing.shared_target_bindings.length
          ? "最终确认：现在会把所选数据库播出记录更新为同一个实体 press_path，媒体只归类一次，再分别增量创建快捷方式。不会覆盖已有文件或冲突快捷方式。"
          : state.landingSourceWorkBindings
          ? "最终确认：现在会按每个一级来源的已预览绑定更新或新增数据库记录，再执行媒体归类并分别创建快捷方式。旧快捷方式诊断不会被自动覆盖。"
          : Array.isArray(landing.draft_works) && landing.draft_works.length > 1
          ? "最终确认：现在会一次写入这些作品的数据库记录，再执行预览中的媒体移动，最后按作品分别增量创建快捷方式。不会覆盖已有文件或清空其他快捷方式。"
          : "最终确认：现在会先新增作品数据库记录，再执行预览中的媒体移动，最后仅增量创建该作品的快捷方式。不会覆盖已有文件或清空其他快捷方式。") +
          "\n\n确定要继续吗？",
      )
    ) {
      return;
    }

    var originalDraft = state.draftWork ? cloneJson(state.draftWork) : null;
    var originalDrafts = state.draftWorks.length ? cloneJson(state.draftWorks) : [];
    var sharedBindings = Array.isArray(landing.shared_target_bindings)
      ? cloneJson(landing.shared_target_bindings)
      : [];
    var sourceLandingBindings = state.landingSourceWorkBindings
      ? cloneJson(state.landingSourceWorkBindings)
      : null;
    var serial = ++requestSerial;
    state.busy = "apply";
    state.dirty = true;
    state.notice = "服务端正在重建完整计划并依次执行，请勿重复操作…";
    state.noticeError = false;
    render();
    setSharedStatus("作品完整落地执行中…", false);
    try {
      var out = await fetchJson("/api/media-directory-organizer/landing/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify(
          Object.assign(
            {
              root: state.root,
              landing_plan_id: landing.landing_plan_id,
              confirmation: String(typed).trim(),
              acknowledge_catalog_write: true,
              acknowledge_move: true,
              acknowledge_shortcuts: true,
            },
            sharedBindings.length
              ? {
                  shared_target_bindings: sharedBindings,
                  acknowledge_shared_targets: true,
                }
              : sourceLandingBindings
              ? { source_work_bindings: sourceLandingBindings }
              : originalDrafts.length
              ? { draft_works: originalDrafts }
              : { draft_work: originalDraft },
          ),
        ),
      });
      if (serial !== requestSerial) return;
      if (handlePartialApply(out)) return;
      if (!out.res.ok || !out.data) {
        throw new Error(responseError(out, "作品完整落地失败"));
      }
      if (out.data.state === "shortcut_pending") {
        state.execution = out.data.media || null;
        state.shortcutPending = {
          kind: "landing",
          root: state.root,
          draft_work: originalDraft,
          draft_works: originalDrafts,
          source_work_bindings: sourceLandingBindings,
          error: out.data.shortcut_error || "快捷方式创建失败",
          retry: out.data.shortcut_retry || {},
          preview: null,
        };
        state.plan = null;
        state.landing = null;
        state.draftWork = null;
        state.draftWorks = [];
        state.sharedTarget = createSharedTargetState();
        state.landingSourceWorkBindings = null;
        invalidateLandingRecognition("", "");
        state.dirty = false;
        state.busy = "";
        state.notice = sharedBindings.length
          ? "共享数据库记录和一次媒体归类已经完成，但快捷方式尚未完成。请只重新检查快捷方式，不要再次移动媒体。"
          : "作品数据库和媒体归类已经完成，但快捷方式尚未完成。请使用下方“重新检查快捷方式”，不要再次执行完整落地。";
        state.noticeError = true;
        render();
        setSharedStatus(state.notice, true);
        return;
      }
      if (out.data.ok === false) {
        throw new Error(responseError(out, "作品完整落地失败"));
      }
      state.execution = out.data.media || out.data.execution || null;
      state.plan = null;
      state.landing = null;
      state.draftWork = null;
      state.draftWorks = [];
      state.sharedTarget = createSharedTargetState();
      state.sourceWorkBindingEdits = Object.create(null);
      state.sourceWorkDrafts = Object.create(null);
      state.sourceWorkEditorSource = "";
      state.sourceWorkEditorMode = "";
      state.landingSourceWorkBindings = null;
      state.shortcutPending = null;
      invalidateLandingRecognition("", "");
      state.routeOverrides = Object.create(null);
      state.fileWorkOverrides = Object.create(null);
      state.sourceWorkOverrides = Object.create(null);
      state.multipleWorkSources = Object.create(null);
      state.sourcePressOverrides = Object.create(null);
      state.dirty = false;
      state.busy = "";
      state.notice = sharedBindings.length
        ? "共享实体目录已完整落地：数据库记录已分别保留，媒体只归类一次，快捷方式均已完成。"
        : sourceLandingBindings
        ? "分来源完整落地已完成：已有作品保持精确绑定，新作品已写入数据库，媒体归类和快捷方式均已完成。"
        : originalDrafts.length
        ? "多个作品已完整落地：数据库、媒体归类和各作品快捷方式均已完成。"
        : "作品已完整落地：数据库、媒体归类和该作品快捷方式均已完成。";
      state.noticeError = false;
      render();
      setSharedStatus(state.notice, false);
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.dirty = true;
      state.notice =
        (error && error.message ? error.message : String(error)) +
        " 服务端会在写入或移动前重建计划；请重新预览当前状态。";
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  function shortcutRetryEndpoint(pending, field, fallback) {
    var retry = pending && pending.retry && typeof pending.retry === "object"
      ? pending.retry
      : {};
    return firstString(retry[field], pending && pending[field], fallback);
  }

  function shortcutRetryRequestScope(pending) {
    pending = pending && typeof pending === "object" ? pending : {};
    var retry = firstObject(pending.retry);
    var preview = firstObject(pending.preview);
    var previewScope = shortcutScopeValues(preview);
    var retryScope = shortcutScopeValues(retry);
    var root = firstString(
      previewScope.root,
      retryScope.root,
      pending.root,
      state.root,
    );
    var workRefs = previewScope.workRefs || retryScope.workRefs;
    if (!workRefs && Array.isArray(pending.work_refs)) workRefs = pending.work_refs;
    var payload = { root: root };
    if (Array.isArray(workRefs) && workRefs.length) {
      payload.work_refs = cloneJson(workRefs);
    } else if (pending.draft_work && typeof pending.draft_work === "object") {
      payload.draft_work = cloneJson(pending.draft_work);
    }
    return payload;
  }

  async function beginShortcutOnlyPreview() {
    var root = String(state.root || "").trim();
    if (!root) {
      setNotice("请先输入作品根目录。", true);
      return;
    }
    if (sharedTargetState().confirmed) {
      setNotice("共享播出记录必须先生成完整落地预览，不能绕过数据库与媒体绑定直接补快捷方式。", true);
      return;
    }
    if (state.plan && state.plan.registration_required) {
      if (state.draftWorks.length > 1) {
        setNotice("当前根目录已识别为多个新作品；请在下方逐部填写后生成一次完整落地预览。", true);
        return;
      }
      if (!state.draftWork) {
        setNotice("该作品尚未写入数据库，请先重新预览并填写作品信息。", true);
        return;
      }
    }
    var planRepair = shortcutRepairDetails(state.plan);
    var planShortcutDetails = shortcutPlanDetails(state.plan);
    var alreadyOrganized = isAlreadyOrganizedPlan(state.plan);
    var repairableMediaPlan = !!(
      planRepair.required && canRepairWithCurrentMediaPlan(state.plan)
    );
    var stagedMultiWorkRepair = !!(
      repairableMediaPlan &&
      (planRepair.candidates.length > 1 || mediaPlanHasMultipleWorks(state.plan))
    );
    var includeMediaMove = !!(
      repairableMediaPlan && !stagedMultiWorkRepair
    );
    state.shortcutPending = {
      kind: alreadyOrganized ? "organizer" : "shortcut-only",
      includeMediaMove: includeMediaMove,
      stagedMultiWorkRepair: stagedMultiWorkRepair,
      root: root,
      work_refs:
        alreadyOrganized && Array.isArray(planShortcutDetails.scope.work_refs)
          ? cloneJson(planShortcutDetails.scope.work_refs)
          : null,
      error: "",
      retry: {
        preview_endpoint: "/api/media-directory-organizer/landing/shortcuts/preview",
        apply_endpoint: "/api/media-directory-organizer/landing/shortcuts/apply",
      },
      preview: planRepair.required ? state.plan : null,
    };
    if (planRepair.required) {
      state.notice =
        (includeMediaMove
          ? "已读取数据库修复候选。选择后会把数据库修复、当前媒体归类计划和快捷方式串联预览；目前尚未写入或移动任何内容。"
          : stagedMultiWorkRepair
          ? "当前计划包含多个数据库作品。请逐个确认并修复 path/press_path；此阶段不会移动媒体，全部绑定后再重新预览统一归类。"
          : "已读取数据库修复候选。必须明确选择并核对每个 press_path，当前未写入或移动任何内容。");
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
      return;
    }
    await previewShortcutRetry();
  }

  function cancelShortcutRetry() {
    if (state.busy) return;
    state.shortcutPending = null;
    state.notice = state.plan || landingDrafts().length
      ? "已退出快捷方式补建；原媒体预览、手填作品信息和人工修正仍然保留。"
      : "已退出快捷方式补建。";
    state.noticeError = false;
    render();
    setSharedStatus(state.notice, false);
  }

  async function previewShortcutRetry() {
    var pending = state.shortcutPending;
    if (!pending) return;
    var serial = ++requestSerial;
    state.busy = "shortcut-retry-preview";
    state.notice = "正在只读检查该作品的快捷方式状态…";
    state.noticeError = false;
    render();
    try {
      var out = await fetchJson(
        shortcutRetryEndpoint(
          pending,
          "preview_endpoint",
          "/api/media-directory-organizer/landing/shortcuts/preview",
        ),
        {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify(shortcutRetryRequestScope(pending)),
        },
      );
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "快捷方式重试预览失败"));
      }
      pending.preview = out.data;
      var returnedScope = shortcutScopeValues(out.data);
      pending.root = firstString(returnedScope.root, pending.root);
      if (returnedScope.workRefs) pending.work_refs = cloneJson(returnedScope.workRefs);
      var checkedIssues = shortcutIssues(out.data);
      pending.error = out.data.ready ? "" : firstIssueMessage(checkedIssues);
      state.busy = "";
      var checkedSummary = shortcutPlanDetails(out.data).summary;
      var checkedRows = shortcutPlanDetails(out.data).rows;
      var checkedTotal = Number(checkedSummary.total_count) || checkedRows.length;
      var allAlreadyExist = !!(
        out.data.ready &&
        checkedTotal > 0 &&
        (Number(checkedSummary.planned_count) || 0) === 0 &&
        (Number(checkedSummary.conflict_count) || 0) === 0 &&
        (Number(checkedSummary.already_exists_count) || 0) >= checkedTotal
      );
      state.notice = allAlreadyExist
        ? "快捷方式检查完成；数据库中的链接均已存在。"
        : out.data.ready
        ? "快捷方式重试预览完成；核对后可单独补建。"
        : shortcutRepairDetails(out.data).required || issuesNeedCatalogRepair(checkedIssues)
        ? "快捷方式暂不能创建：数据库记录缺失或不完整。请核对下方具体原因，并使用安全修复入口；当前未移动或写入任何内容。"
        : checkedIssues.length
        ? "快捷方式暂不能创建：" + firstIssueMessage(checkedIssues) + "；当前未写入任何内容。"
        : "快捷方式计划当前不可执行；服务端没有提供具体问题，当前未写入任何内容。";
      state.noticeError = !out.data.ready;
      render();
      setSharedStatus(state.notice, state.noticeError);
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.notice = error && error.message ? error.message : String(error);
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  async function applyShortcutRetry() {
    var pending = state.shortcutPending;
    var retry = pending && pending.preview;
    var retryPlanId = firstString(
      retry && retry.retry_plan_id,
      shortcutPlanDetails(retry).planId,
    );
    if (!retry || !retry.ready || !retryPlanId) {
      setNotice("请先重新检查快捷方式并解决冲突。", true);
      return;
    }
    var typed = window.prompt(
      "请输入快捷方式重试确认 ID。此操作只会补建该作品的快捷方式：\n\n" +
        retryPlanId,
      "",
    );
    if (typed === null) return;
    if (String(typed).trim() !== String(retryPlanId)) {
      setNotice("快捷方式重试 ID 输入不一致，未创建任何内容。", true);
      return;
    }
    if (!window.confirm("确认只增量补建该作品缺少的快捷方式吗？")) return;
    var serial = ++requestSerial;
    state.busy = "shortcut-retry-apply";
    state.notice = "正在重建重试计划并补建快捷方式…";
    state.noticeError = false;
    render();
    try {
      var out = await fetchJson(
        shortcutRetryEndpoint(
          pending,
          "apply_endpoint",
          "/api/media-directory-organizer/landing/shortcuts/apply",
        ),
        {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify(Object.assign(shortcutRetryRequestScope(pending), {
            retry_plan_id: retryPlanId,
            confirmation: String(typed).trim(),
            acknowledge_shortcuts: true,
          })),
        },
      );
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "快捷方式补建失败"));
      }
      state.shortcutPending = null;
      state.busy = "";
      if (state.plan) state.dirty = true;
      state.notice = state.plan
        ? "快捷方式补建完成；原媒体计划仍保留但已标记为需要重新预览。"
        : "快捷方式补建完成；该作品现在已完整落地。";
      state.noticeError = false;
      render();
      setSharedStatus(state.notice, false);
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.notice = error && error.message ? error.message : String(error);
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  function shortcutRepairCandidatesForPending(pending) {
    var details = shortcutRepairDetails(pending && pending.preview);
    return { details: details, candidates: details.candidates.slice() };
  }

  function shortcutRepairRequest(candidateIndex, reuseCurrent) {
    var pending = state.shortcutPending;
    if (reuseCurrent && pending && pending.repairRequest) {
      return withRepairMediaPlan(cloneJson(pending.repairRequest), pending);
    }
    var root = String(state.root || "").trim();
    if (pending && pending.repairDraft) {
      var editedDraft = cloneJson(pending.repairDraft);
      editedDraft.path = root;
      var editedPresses = repairDraftPresses(editedDraft);
      if (!firstString(editedDraft.name)) {
        throw new Error("请先填写作品名。");
      }
      if (!firstString(firstObject(editedDraft.date).start)) {
        throw new Error("请先填写作品开始日期。");
      }
      if (!editedPresses.length) {
        throw new Error("至少需要一条压制记录。");
      }
      var configuredFormats = Object.keys(
        firstObject(state.config && state.config.detection && state.config.detection.format_markers),
      );
      var configuredGroups = groupRegistryOptions()
        .map(function (item) {
          return firstString(item && item.code);
        })
        .filter(function (value) {
          return !!value;
        });
      for (var editedIndex = 0; editedIndex < editedPresses.length; editedIndex++) {
        var editedPress = editedPresses[editedIndex] || {};
        if (!firstString(editedPress.press_format)) {
          throw new Error("压制记录 " + (editedIndex + 1) + " 缺少压制格式。");
        }
        if (!firstString(editedPress.press_group)) {
          throw new Error("压制记录 " + (editedIndex + 1) + " 缺少压制组。");
        }
        var canonicalFormat = configuredFormats.find(function (value) {
          return value.toLowerCase() === firstString(editedPress.press_format).toLowerCase();
        });
        if (configuredFormats.length && !canonicalFormat && !pending.repairCatalogRef) {
          throw new Error("压制记录 " + (editedIndex + 1) + " 的压制格式不在当前服务端配置中。");
        }
        if (canonicalFormat) editedPress.press_format = canonicalFormat;
        var canonicalGroup = configuredGroups.find(function (value) {
          return value.toLowerCase() === firstString(editedPress.press_group).toLowerCase();
        });
        if (configuredGroups.length && !canonicalGroup && !pending.repairCatalogRef) {
          throw new Error("压制记录 " + (editedIndex + 1) + " 的压制组不在当前数据库注册表中。");
        }
        if (canonicalGroup) editedPress.press_group = canonicalGroup;
        if (!firstString(editedPress.press_path)) {
          throw new Error(
            "压制记录 " +
              (editedIndex + 1) +
              (pending.includeMediaMove
                ? " 缺少 press_path；请填写本次计划将创建的目标目录名。"
                : " 缺少 press_path；请填写已整理且确实存在的目标目录名。"),
          );
        }
      }
      var editedPayload = { root: root, draft_work: editedDraft };
      if (pending.repairCatalogRef) {
        editedPayload.catalog_ref = cloneJson(pending.repairCatalogRef);
      }
      if (pending.repairCatalogIntent) {
        editedPayload.catalog_intent = pending.repairCatalogIntent;
      }
      return withRepairMediaPlan(editedPayload, pending);
    }
    var bundle = shortcutRepairCandidatesForPending(pending);
    var index = Number(candidateIndex);
    if (!Number.isInteger(index) || index < 0) index = 0;
    var candidate = bundle.candidates[index];
    if (!candidate || !candidate.draftWork) {
      throw new Error(
        bundle.candidates.length > 1
          ? "请选择一个明确的数据库修复候选。"
          : "服务端没有提供可核对的数据库修复草稿，请先返回填写作品信息。",
      );
    }
    var draft = cloneJson(candidate.draftWork);
    draft.path = root;
    var payload = { root: root, draft_work: draft };
    if (candidate.catalogRef) payload.catalog_ref = cloneJson(candidate.catalogRef);
    return withRepairMediaPlan(payload, pending);
  }

  function selectShortcutRepairCandidate(candidateIndex) {
    var pending = state.shortcutPending;
    if (!pending) return;
    var bundle = shortcutRepairCandidatesForPending(pending);
    var index = Number(candidateIndex);
    if (!Number.isInteger(index) || index < 0 || index >= bundle.candidates.length) {
      setNotice("请选择一个明确的数据库修复候选。", true);
      return;
    }
    var candidate = bundle.candidates[index];
    pending.repairCandidateIndex = index;
    pending.repairDraft = cloneJson(candidate.draftWork);
    pending.repairDraft.path = firstString(pending.root, state.root);
    repairDraftPresses(pending.repairDraft);
    pending.repairCatalogRef = candidate.catalogRef ? cloneJson(candidate.catalogRef) : null;
    pending.repairCatalogIntent = "";
    pending.repairPreview = null;
    pending.repairRequest = null;
    pending.error = "";
    state.notice =
      (pending.includeMediaMove
        ? "已选择修复候选。请逐项确认作品 path、每个 press_path 与当前媒体整理目标，再生成串联预览。"
        : "已选择修复候选。请逐项确认作品 path 和每个 press_path，再生成只读修复预览。");
    state.noticeError = false;
    render();
    setSharedStatus(state.notice, false);
  }

  function selectShortcutRepairIndependent() {
    var pending = state.shortcutPending;
    if (!pending) return;
    var repair = shortcutRepairDetails(pending.preview);
    if (!repair.independentAppendAllowed || !repair.directDraft) {
      setNotice("当前候选不允许新增为独立作品；请选择已有数据库记录。", true);
      return;
    }
    pending.repairCandidateIndex = null;
    pending.repairDraft = cloneJson(repair.directDraft);
    pending.repairDraft.path = firstString(pending.root, state.root);
    repairDraftPresses(pending.repairDraft);
    pending.repairCatalogRef = null;
    pending.repairCatalogIntent = "append_independent";
    pending.repairPreview = null;
    pending.repairRequest = null;
    pending.error = "";
    state.notice =
      "已明确选择新增为独立作品。请再次核对作品名与每个 press_path；服务端仍会阻止规范化同名重复。";
    state.noticeError = false;
    render();
    setSharedStatus(state.notice, false);
  }

  function backFromShortcutRepairEditor() {
    var pending = state.shortcutPending;
    if (!pending || state.busy) return;
    if (pending.repairPreview && pending.repairDraft) {
      pending.repairPreview = null;
      pending.repairRequest = null;
      state.notice = "已返回补录信息编辑；修改后必须重新生成修复确认 ID。";
      state.noticeError = false;
      render();
      setSharedStatus(state.notice, false);
      return;
    }
    if (!pending.preview) {
      cancelShortcutRetry();
      return;
    }
    pending.repairDraft = null;
    pending.repairCatalogRef = null;
    pending.repairCatalogIntent = "";
    pending.repairCandidateIndex = null;
    pending.repairPreview = null;
    pending.repairRequest = null;
    state.notice = "已返回数据库修复候选；原媒体预览和人工修正仍然保留。";
    state.noticeError = false;
    render();
    setSharedStatus(state.notice, false);
  }

  async function previewShortcutRepair(candidateIndex, reuseCurrent) {
    var root = String(state.root || "").trim();
    if (!root) {
      setNotice("请先输入作品根目录。", true);
      return;
    }
    var pending = state.shortcutPending;
    if (!pending) {
      pending = state.shortcutPending = {
        kind: "repair",
        root: root,
        work_refs: null,
        error: "",
        retry: {},
        preview: null,
        repairPreview: null,
        repairRequest: null,
      };
    }
    var request;
    try {
      request = shortcutRepairRequest(candidateIndex, !!reuseCurrent);
    } catch (error) {
      setNotice(error && error.message ? error.message : String(error), true);
      return;
    }
    var sourceRepair = shortcutRepairDetails(pending.preview);
    var endpoint = sourceRepair.previewEndpoint;
    var serial = ++requestSerial;
    state.busy = "shortcut-repair-preview";
    state.notice = pending.includeMediaMove
      ? "正在只读校验数据库修复、媒体归类和快捷方式串联计划…"
      : "正在只读校验数据库补录和快捷方式修复计划…";
    state.noticeError = false;
    render();
    try {
      var out = await fetchJson(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify(request),
      });
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "数据库与快捷方式修复预览失败"));
      }
      var returnedIssues = shortcutIssues(out.data);
      var returnedRepair = shortcutRepairDetails(out.data);
      var rejectedMultiWorkMediaPlan = returnedIssues.some(function (issue) {
        return String((issue && issue.code) || "") === "repair-media-plan-multiple-works";
      });
      if (rejectedMultiWorkMediaPlan) {
        pending.includeMediaMove = false;
        pending.stagedMultiWorkRepair = true;
      }
      if (!out.data.ready && returnedRepair.candidates.length) {
        pending.kind = "repair";
        pending.preview = out.data;
        pending.repairDraft = null;
        pending.repairCatalogRef = null;
        pending.repairCatalogIntent = "";
        pending.repairCandidateIndex = null;
        pending.repairPreview = null;
        pending.repairRequest = null;
        pending.error = firstIssueMessage(returnedIssues);
        state.busy = "";
        state.notice = rejectedMultiWorkMediaPlan
          ? "当前媒体计划包含多个作品，已切换为逐作品修复数据库和快捷方式；请重新选择当前作品，本阶段不会移动媒体。"
          : "发现多个可能的数据库作品；必须明确选择一条并逐项核对 press_path，未写入任何内容。";
        state.noticeError = true;
        render();
        setSharedStatus(state.notice, true);
        return;
      }
      pending.kind = "repair";
      pending.repairRequest = request;
      pending.repairPreview = out.data;
      pending.error = out.data.ready ? "" : firstIssueMessage(returnedIssues);
      state.busy = "";
      var mediaMovePlanned = out.data.media_move_planned === true;
      state.notice = out.data.ready
        ? mediaMovePlanned
          ? "串联预览已生成。请核对数据库动作、媒体归类目标和快捷方式，再确认一次性执行。"
          : "修复预览已生成。请核对数据库动作、每个 press_path 和快捷方式，再确认执行；不会移动媒体文件。"
        : returnedIssues.length
        ? "修复计划不可执行：" + firstIssueMessage(returnedIssues)
        : "修复计划不可执行；服务端没有提供具体原因。";
      state.noticeError = !out.data.ready;
      render();
      setSharedStatus(state.notice, state.noticeError);
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.notice = error && error.message ? error.message : String(error);
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  async function applyShortcutRepair() {
    var pending = state.shortcutPending;
    var preview = pending && pending.repairPreview;
    var repair = shortcutRepairDetails(preview);
    var issues = shortcutIssues(preview);
    if (!preview || !preview.ready || !repair.planId || issues.length) {
      setNotice("请先生成可执行的数据库与快捷方式修复预览，并解决所有具体问题。", true);
      return;
    }
    var mediaMovePlanned = preview.media_move_planned === true;
    var typed = window.prompt(
      (mediaMovePlanned
        ? "请输入修复确认 ID。此操作将修复数据库、执行当前媒体归类计划并创建快捷方式：\n\n"
        : "请输入修复确认 ID。此操作只补录/修正数据库并创建快捷方式，不会移动媒体文件：\n\n") +
        repair.planId,
      "",
    );
    if (typed === null) return;
    if (String(typed).trim() !== String(repair.planId)) {
      setNotice(
        mediaMovePlanned
          ? "修复确认 ID 输入不一致，未写数据库、未移动媒体、未创建快捷方式。"
          : "修复确认 ID 输入不一致，未写数据库、未创建快捷方式。",
        true,
      );
      return;
    }
    if (
      !window.confirm(
        mediaMovePlanned
          ? "最终确认：先写入预览中的数据库变更，再按当前计划归类媒体，最后增量创建缺少的快捷方式。不会覆盖已有媒体或快捷方式。\n\n确定要继续吗？"
          : "最终确认：写入预览中的数据库变更，并增量创建缺少的快捷方式。不会移动媒体文件，也不会覆盖已有快捷方式。\n\n确定要继续吗？",
      )
    ) {
      return;
    }
    var request = Object.assign(cloneJson(pending.repairRequest || {}), {
      repair_plan_id: repair.planId,
      confirmation: String(typed).trim(),
      acknowledge_catalog_write: true,
      acknowledge_shortcuts: true,
    });
    if (mediaMovePlanned) request.acknowledge_move = true;
    var serial = ++requestSerial;
    state.busy = "shortcut-repair-apply";
    state.notice = mediaMovePlanned
      ? "正在重建串联计划并修复数据库、归类媒体、创建快捷方式…"
      : "正在重建修复计划并补录数据库、创建快捷方式…";
    state.noticeError = false;
    render();
    try {
      var out = await fetchJson(repair.applyEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify(request),
      });
      if (serial !== requestSerial) return;
      if (handlePartialApply(out)) return;
      if (out.data && out.data.state === "shortcut_pending") {
        var retry = firstObject(out.data.shortcut_retry);
        var responseScope = shortcutScopeValues(retry);
        state.execution = out.data.media || out.data.execution || null;
        state.plan = null;
        state.landing = null;
        state.draftWork = null;
        state.draftWorks = [];
        state.sharedTarget = createSharedTargetState();
        state.routeOverrides = Object.create(null);
        state.fileWorkOverrides = Object.create(null);
        state.sourceWorkOverrides = Object.create(null);
        clearSourceWorkBindingState();
        state.multipleWorkSources = Object.create(null);
        state.sourcePressOverrides = Object.create(null);
        state.shortcutPending = {
          kind: "landing",
          root: firstString(responseScope.root, retry.root, state.root),
          work_refs: responseScope.workRefs,
          error: out.data.shortcut_error || "快捷方式创建失败",
          retry: retry,
          preview: null,
        };
        state.dirty = false;
        state.busy = "";
        state.notice =
          "数据库修复和媒体归类已经完成，但快捷方式尚未完成。请只使用下方快捷方式重试，不要再次移动媒体或重复写数据库。";
        state.noticeError = true;
        render();
        setSharedStatus(state.notice, true);
        return;
      }
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "数据库与快捷方式修复失败"));
      }
      state.execution = mediaMovePlanned ? out.data.media || null : state.execution;
      state.shortcutPending = null;
      state.plan = null;
      state.landing = null;
      state.draftWork = null;
      state.draftWorks = [];
      state.sharedTarget = createSharedTargetState();
      if (mediaMovePlanned) {
        state.routeOverrides = Object.create(null);
        state.fileWorkOverrides = Object.create(null);
        state.sourceWorkOverrides = Object.create(null);
        state.multipleWorkSources = Object.create(null);
        state.sourcePressOverrides = Object.create(null);
      }
      state.busy = "";
      state.dirty = false;
      state.notice = mediaMovePlanned
        ? "数据库修复、媒体归类和快捷方式创建完成。"
        : pending.stagedMultiWorkRepair
        ? "当前数据库作品及快捷方式已修复，媒体文件没有移动。请重新预览并继续处理其余作品；全部绑定后再统一归类。"
        : "数据库补录/修正和快捷方式创建完成；媒体文件没有移动。";
      state.noticeError = false;
      render();
      setSharedStatus(state.notice, false);
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.notice = error && error.message ? error.message : String(error);
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  function guideShortcutRegistration() {
    if (state.busy) return;
    state.shortcutPending = null;
    if (state.plan && state.plan.registration_required && landingDrafts().length) {
      state.notice = "请核对下方作品名、日期和每个 press_path；可选择完整落地，或使用“不移动”补录入口。";
      state.noticeError = false;
      render();
      setSharedStatus(state.notice, false);
      return;
    }
    previewPlan();
  }

  function confirmationSummary(plan) {
    var summary = (plan && plan.summary) || {};
    var shortcutDetails = shortcutPlanDetails(plan);
    var shortcutSummary = shortcutDetails.summary;
    var assignmentCount = summary.assignment_count;
    if (assignmentCount == null) {
      assignmentCount = plan && Array.isArray(plan.assignments) ? plan.assignments.length : 0;
    }
    var unresolvedCount = summary.unresolved_file_count;
    if (unresolvedCount == null) {
      unresolvedCount = plan && Array.isArray(plan.unresolved_files)
        ? plan.unresolved_files.length
        : 0;
    }
    return (
      "请再次核对本次移动摘要：\n\n" +
      "来源目录：" +
      (summary.source_directory_count || 0) +
      " 个\n目标目录：" +
      (summary.target_directory_count || 0) +
      " 个\n路由任务：" +
      (assignmentCount || 0) +
      " 个\n文件：" +
      (summary.file_count || 0) +
      " 个\n回退分类文件：" +
      (summary.fallback_file_count || 0) +
      " 个\n未决文件：" +
      (unresolvedCount || 0) +
      " 个\n总大小：" +
      formatBytes(summary.bytes || 0) +
      "\n快捷方式：" +
      String(shortcutSummary.planned_count || 0) +
      " 个（已存在 " +
      String(shortcutSummary.already_exists_count || 0) +
      "，冲突 " +
      String(shortcutSummary.conflict_count || 0) +
      "）\n快捷方式确认 ID：" +
      String(shortcutDetails.planId || "") +
      "\n计划 ID：" +
      String(plan.plan_id || "") +
      "\n\n这是第一次确认。点击“确定”后仍不会立即移动文件或创建快捷方式。"
    );
  }

  async function applyCurrentPlan() {
    if (state.landing) {
      await applyCurrentLanding();
      return;
    }
    if (!planCanExecute()) {
      setNotice("当前计划不可执行：请先重新预览，并解决所有问题。", true);
      return;
    }
    var plan = state.plan;
    var shortcutDetails = shortcutPlanDetails(plan);
    if (!window.confirm(confirmationSummary(plan))) return;

    var typed = window.prompt(
      "第二步：请输入完整计划 ID 以确认你核对的是当前预览。\n\n" + plan.plan_id,
      "",
    );
    if (typed === null) return;
    if (String(typed).trim() !== String(plan.plan_id)) {
      setNotice("计划 ID 输入不一致，未执行任何移动。", true);
      return;
    }

    if (
      !window.confirm(
        "最终确认：现在将按计划移动文件，并增量创建预览中的快捷方式。执行过程中不会覆盖已有文件或已有快捷方式。\n\n确定要继续吗？",
      )
    ) {
      return;
    }

    var serial = ++requestSerial;
    state.busy = "apply";
    state.dirty = true;
    state.notice = "服务端正在重新校验计划并执行移动，请勿重复操作...";
    state.noticeError = false;
    render();
    setSharedStatus("目录整理计划执行中...", false);
    try {
      var sourceBindings = sourceWorkBindingsPayload();
      var out = await fetchJson("/api/media-directory-organizer/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({
          root: state.root,
          target_overrides: legacyTargetOverrides(),
          route_target_overrides: state.routeOverrides,
          file_work_overrides: state.fileWorkOverrides,
          source_work_overrides: sourceWorkOverridesPayload(sourceBindings),
          source_work_bindings: sourceBindings,
          source_press_overrides: sourcePressOverridesPayload(),
          plan_id: plan.plan_id,
          confirmation: String(typed).trim(),
          acknowledge_move: true,
          shortcut_plan_id: shortcutDetails.planId,
          shortcut_confirmation: shortcutDetails.planId,
          acknowledge_shortcuts: true,
        }),
      });
      if (serial !== requestSerial) return;
      if (handlePartialApply(out)) return;
      if (!out.res.ok || !out.data) {
        throw new Error(responseError(out, "目录整理执行失败"));
      }
      if (out.data.state === "shortcut_pending") {
        var retry = firstObject(out.data.shortcut_retry);
        var planScope = shortcutDetails.scope;
        var responseScope = shortcutScopeValues(retry);
        state.execution = out.data.media || out.data.execution || null;
        state.plan = null;
        state.landing = null;
        state.draftWork = null;
        state.draftWorks = [];
        state.sharedTarget = createSharedTargetState();
        state.routeOverrides = Object.create(null);
        state.fileWorkOverrides = Object.create(null);
        state.sourceWorkOverrides = Object.create(null);
        state.multipleWorkSources = Object.create(null);
        state.sourcePressOverrides = Object.create(null);
        state.shortcutPending = {
          kind: "organizer",
          root: firstString(responseScope.root, planScope.root, state.root),
          work_refs: responseScope.workRefs ||
            (Array.isArray(planScope.work_refs) ? cloneJson(planScope.work_refs) : null),
          error: out.data.shortcut_error || "快捷方式创建失败",
          retry: retry,
          preview: null,
        };
        state.dirty = false;
        state.busy = "";
        state.notice =
          "媒体移动已经完成，但快捷方式尚未完成。请使用下方“重新检查快捷方式”，不要再次执行媒体移动。";
        state.noticeError = true;
        render();
        setSharedStatus(state.notice, true);
        return;
      }
      if (out.data.ok === false) {
        throw new Error(responseError(out, "目录整理执行失败"));
      }
      state.execution = out.data.execution || out.data.result || out.data;
      state.plan = null;
      state.routeOverrides = Object.create(null);
      state.fileWorkOverrides = Object.create(null);
      state.sourceWorkOverrides = Object.create(null);
      clearSourceWorkBindingState();
      state.multipleWorkSources = Object.create(null);
      state.sourcePressOverrides = Object.create(null);
      state.dirty = false;
      state.busy = "";
      state.shortcutPending = null;
      state.notice = "执行完成：媒体归类和快捷方式均已完成。需要继续检查时，请重新生成预览。";
      state.noticeError = false;
      render();
      setSharedStatus("目录整理和快捷方式创建完成。", false);
    } catch (error) {
      if (serial !== requestSerial) return;
      state.busy = "";
      state.dirty = true;
      state.notice =
        (error && error.message ? error.message : String(error)) +
        " 请重新预览后再决定是否执行。";
      state.noticeError = true;
      render();
      setSharedStatus(state.notice, true);
    }
  }

  function bindViewOnce() {
    var target = view();
    if (!target || target.__NimdaOrganizerBound) return;
    target.__NimdaOrganizerBound = true;

    var onInput = function (event) {
      var input = event.target;
      if (!input || !input.matches) return;
      if (input.matches("[data-shared-target-confirm]")) {
        if (event.type !== "change") return;
        setSharedTargetConfirmed(!!input.checked);
        return;
      }
      if (input.matches("[data-shared-target-member]")) {
        if (event.type !== "change") return;
        setSharedTargetMemberSelected(
          input.getAttribute("data-shared-target-member") || "",
          !!input.checked,
        );
        return;
      }
      if (input.matches("[data-shared-target-path]")) {
        var sharedPathKey = String(input.getAttribute("data-shared-target-path") || "");
        if (sharedTargetState().pressPaths[sharedPathKey] === input.value) return;
        setSharedTargetPressPath(sharedPathKey, input.value);
        return;
      }
      if (input.matches("[data-shared-target-source]")) {
        if (event.type !== "change") return;
        setSharedTargetSourceOwner(
          input.getAttribute("data-shared-target-source") || "",
          input.value,
        );
        return;
      }
      if (input.matches("[data-repair-field]") && state.shortcutPending && state.shortcutPending.repairDraft) {
        var repairField = String(input.getAttribute("data-repair-field") || "");
        if (!repairField) return;
        state.shortcutPending.repairDraft[repairField] = input.value;
        state.shortcutPending.repairPreview = null;
        state.shortcutPending.repairRequest = null;
        return;
      }
      if (input.matches("[data-repair-date]") && state.shortcutPending && state.shortcutPending.repairDraft) {
        var repairDateField = String(input.getAttribute("data-repair-date") || "");
        if (!repairDateField) return;
        if (
          !state.shortcutPending.repairDraft.date ||
          typeof state.shortcutPending.repairDraft.date !== "object"
        ) {
          state.shortcutPending.repairDraft.date = {};
        }
        state.shortcutPending.repairDraft.date[repairDateField] = input.value;
        state.shortcutPending.repairPreview = null;
        state.shortcutPending.repairRequest = null;
        return;
      }
      if (input.matches("[data-repair-press]") && state.shortcutPending && state.shortcutPending.repairDraft) {
        var repairPressField = String(input.getAttribute("data-repair-press") || "");
        var repairPressIndex = Number(input.getAttribute("data-repair-press-index"));
        var repairPresses = repairDraftPresses(state.shortcutPending.repairDraft);
        if (
          !repairPressField ||
          !Number.isInteger(repairPressIndex) ||
          !repairPresses[repairPressIndex]
        ) {
          return;
        }
        repairPresses[repairPressIndex][repairPressField] =
          repairPressField === "press_group"
            ? String(input.value || "").trim()
            : input.value;
        state.shortcutPending.repairPreview = null;
        state.shortcutPending.repairRequest = null;
        return;
      }
      if (input.matches("[data-resource-picker-search-input]") && state.resourcePicker.open) {
        state.resourcePicker.searchDraft = input.value;
        return;
      }
      if (input.matches("[data-landing-recognition-query]")) {
        var currentRecognition = recognitionState();
        var previousQuery = currentRecognition.query;
        if (String(previousQuery) !== String(input.value)) {
          if (recognitionHasTransientState(currentRecognition)) {
            invalidateLandingRecognition(
              input.value,
              "搜索词已变化，旧识别候选已作废；请重新识别。",
            );
          } else {
            currentRecognition.query = input.value;
          }
        }
        return;
      }
      if (input.matches("[data-source-work-catalog]")) {
        if (event.type !== "change") return;
        selectSourceCatalogBinding(
          input.getAttribute("data-source-work-catalog") || "",
          input.value,
        );
        return;
      }
      if (input.matches("[data-source-work-manual]")) {
        if (event.type !== "change") return;
        setSourceWorkBinding(
          input.getAttribute("data-source-work-manual") || "",
          input.value,
        );
        return;
      }
      if (input.matches("[data-source-catalog-press]")) {
        updateSourceCatalogPress(
          input.getAttribute("data-source-name") || "",
          input.getAttribute("data-source-catalog-press") || "",
          input.value,
          Number(input.getAttribute("data-press-index")),
        );
        return;
      }
      if (input.matches("[data-source-draft-field]")) {
        updateSourceWorkDraft(
          input.getAttribute("data-source-name") || "",
          input.getAttribute("data-source-draft-field") || "",
          input.value,
          "field",
          -1,
        );
        return;
      }
      if (input.matches("[data-source-draft-date]")) {
        updateSourceWorkDraft(
          input.getAttribute("data-source-name") || "",
          input.getAttribute("data-source-draft-date") || "",
          input.value,
          "date",
          -1,
        );
        return;
      }
      if (input.matches("[data-source-draft-press]")) {
        updateSourceWorkDraft(
          input.getAttribute("data-source-name") || "",
          input.getAttribute("data-source-draft-press") || "",
          input.value,
          "press",
          Number(input.getAttribute("data-press-index")),
        );
        return;
      }
      if (input.id === "organizer-root-input") {
        if (resetPlanScopedStateForRootChange(input.value)) {
          invalidatePreview("作品根目录已编辑，请重新预览。");
        }
        return;
      }
      if (input.matches("[data-landing-field]") && landingDrafts().length) {
        var field = String(input.getAttribute("data-landing-field") || "");
        var activeDraft = landingDraftForInput(input);
        if (!field || !activeDraft) return;
        var previousValue = activeDraft[field];
        var recognitionBeforeFieldEdit = recognitionState();
        var hadFieldCandidates = recognitionHasTransientState(recognitionBeforeFieldEdit);
        var nextRecognitionQuery = recognitionBeforeFieldEdit.query;
        if (
          field === "name" &&
          (!String(nextRecognitionQuery || "").trim() ||
            String(nextRecognitionQuery).trim() === String(previousValue || "").trim())
        ) {
          nextRecognitionQuery = input.value;
        }
        activeDraft[field] = input.value;
        state.landing = null;
        if (state.draftWork) {
          invalidateLandingRecognition(
            nextRecognitionQuery,
            hadFieldCandidates ? "作品信息已变化，旧识别候选已作废；请重新识别。" : "",
          );
        }
        invalidatePreview("新增作品信息已编辑，请重新生成完整落地预览。");
        return;
      }
      if (input.matches("[data-landing-date]") && landingDrafts().length) {
        var dateField = String(input.getAttribute("data-landing-date") || "");
        var dateDraft = landingDraftForInput(input);
        if (!dateField || !dateDraft) return;
        if (!dateDraft.date || typeof dateDraft.date !== "object") {
          dateDraft.date = {};
        }
        var dateRecognition = recognitionState();
        var hadDateCandidates = recognitionHasTransientState(dateRecognition);
        dateDraft.date[dateField] = input.value;
        state.landing = null;
        if (state.draftWork) {
          invalidateLandingRecognition(
            dateRecognition.query,
            hadDateCandidates ? "作品日期已变化，旧识别候选已作废；请重新识别。" : "",
          );
        }
        invalidatePreview("作品日期已编辑，请重新生成完整落地预览。");
        return;
      }
      if (input.matches("[data-landing-press]") && landingDrafts().length) {
        var pressField = String(input.getAttribute("data-landing-press") || "");
        var pressIndex = Number(input.getAttribute("data-press-index"));
        var pressDraft = landingDraftForInput(input);
        var draftPresses = pressDraft && Array.isArray(pressDraft.presses)
          ? pressDraft.presses
          : [];
        if (!pressField || !Number.isInteger(pressIndex) || !draftPresses[pressIndex]) return;
        var pressRecognition = recognitionState();
        var hadPressCandidates = recognitionHasTransientState(pressRecognition);
        draftPresses[pressIndex][pressField] =
          pressField === "press_group" ? String(input.value || "").toUpperCase() : input.value;
        state.landing = null;
        if (state.draftWork) {
          invalidateLandingRecognition(
            pressRecognition.query,
            hadPressCandidates ? "压制信息已变化，旧识别候选已作废；请重新识别。" : "",
          );
        }
        invalidatePreview("压制信息已编辑，请重新生成完整落地预览。");
        return;
      }
      if (input.matches("[data-organizer-target]")) {
        var routeId = String(input.getAttribute("data-route-id") || "");
        if (!routeId) return;
        state.routeOverrides[routeId] = input.value;
        invalidatePreview("目标目录已编辑，请重新预览并生成新的计划 ID。");
        return;
      }
      if (input.matches("[data-source-press-field]")) {
        if (event.type !== "change") return;
        var sourcePressPath = String(input.getAttribute("data-source-press-path") || "");
        var sourcePressField = String(input.getAttribute("data-source-press-field") || "");
        if (
          !sourcePressPath ||
          (sourcePressField !== "press_format" && sourcePressField !== "press_group")
        ) {
          return;
        }
        var sourcePressOverride = state.sourcePressOverrides[sourcePressPath];
        if (!sourcePressOverride || typeof sourcePressOverride !== "object") {
          sourcePressOverride = Object.create(null);
        } else {
          sourcePressOverride = Object.assign(Object.create(null), sourcePressOverride);
        }
        var sourcePressValue = String(input.value || "").trim();
        if (sourcePressValue) {
          sourcePressOverride[sourcePressField] = sourcePressValue;
        } else {
          delete sourcePressOverride[sourcePressField];
        }
        if (firstString(sourcePressOverride.press_format, sourcePressOverride.press_group)) {
          state.sourcePressOverrides[sourcePressPath] = sourcePressOverride;
        } else {
          delete state.sourcePressOverrides[sourcePressPath];
        }
        invalidatePreview(
          sourcePressField === "press_format"
            ? "来源目录的压制格式已人工指定，正在重新预览。"
            : "来源目录的压制组已人工指定，正在重新预览。",
        );
        previewPlan();
        return;
      }
      if (input.matches("[data-source-work-dir]")) {
        if (event.type !== "change") return;
        var sourceWorkDir = String(input.getAttribute("data-source-work-dir") || "");
        var sourceWorkMode = selectSourceWorkMode(sourceWorkDir, input.value);
        if (sourceWorkMode === "single") previewPlan();
        return;
      }
      if (input.matches("[data-file-source]")) {
        var fileSource = String(input.getAttribute("data-file-source") || "");
        if (!fileSource) return;
        if (input.value) {
          state.fileWorkOverrides[fileSource] = input.value;
        } else {
          delete state.fileWorkOverrides[fileSource];
        }
        invalidatePreview("未决文件的作品归属已编辑，请重新预览并生成新的计划 ID。");
      }
    };

    var onClick = function (event) {
      var clicked = event.target;
      if (!clicked || !clicked.closest) return;
      var backdrop = clicked.closest("[data-resource-picker-backdrop]");
      if (backdrop && clicked === backdrop) {
        closeResourcePicker();
        return;
      }
      var button = clicked.closest("[data-organizer-action]");
      if (!button || !target.contains(button)) return;
      var action = button.getAttribute("data-organizer-action");
      if (action === "move-files-page") {
        changeMoveFilePage(button);
      } else if (action === "reload-config") {
        loadConfigAndPreview(false);
      } else if (action === "preview") {
        previewPlan();
      } else if (action === "resource-picker-open") {
        openResourcePicker();
      } else if (action === "resource-picker-close") {
        closeResourcePicker();
      } else if (action === "resource-picker-search") {
        searchResourcePicker();
      } else if (action === "resource-picker-clear-search") {
        clearResourcePickerSearch();
      } else if (action === "resource-picker-back") {
        backResourcePickerFolder();
      } else if (action === "resource-picker-open-folder") {
        openResourcePickerFolder(button.getAttribute("data-resource-relpath") || "");
      } else if (action === "resource-picker-select" || action === "resource-picker-select-current") {
        selectResourcePickerPath(button.getAttribute("data-resource-path") || "");
      } else if (action === "resource-picker-go-library") {
        goToResourceLibrary();
      } else if (action === "unresolved-catalog-guide") {
        guideUnresolvedCatalog(button.getAttribute("data-source-work-dir") || "");
      } else if (action === "source-work-multiple-enable") {
        setSourceMultipleWorkMode(
          button.getAttribute("data-source-work-dir") || "",
          true,
        );
      } else if (action === "source-work-multiple-disable") {
        setSourceMultipleWorkMode(
          button.getAttribute("data-source-work-dir") || "",
          false,
        );
      } else if (action === "source-work-search") {
        openSourceWorkEditor(button.getAttribute("data-source-name") || "", "search");
      } else if (action === "source-work-manual") {
        openSourceWorkEditor(button.getAttribute("data-source-name") || "", "manual");
      } else if (action === "source-work-editor-close") {
        closeSourceWorkEditor();
      } else if (action === "landing-recognition-run") {
        runLandingRecognition();
      } else if (action === "landing-recognition-apply") {
        applyLandingRecognitionCandidate(button.getAttribute("data-recognition-candidate-id") || "");
      } else if (action === "landing-preview") {
        previewLandingPlan();
      } else if (action === "shortcut-only-preview") {
        beginShortcutOnlyPreview();
      } else if (action === "shortcut-repair-registration-preview") {
        beginShortcutOnlyPreview();
      } else if (action === "shortcut-retry-preview") {
        previewShortcutRetry();
      } else if (action === "shortcut-retry-apply") {
        applyShortcutRetry();
      } else if (action === "shortcut-repair-select") {
        selectShortcutRepairCandidate(button.getAttribute("data-repair-candidate-index") || "0");
      } else if (action === "shortcut-repair-independent") {
        selectShortcutRepairIndependent();
      } else if (action === "shortcut-repair-preview") {
        previewShortcutRepair(-1);
      } else if (action === "shortcut-repair-repreview") {
        previewShortcutRepair(0, true);
      } else if (action === "shortcut-repair-apply") {
        applyShortcutRepair();
      } else if (action === "shortcut-repair-back") {
        backFromShortcutRepairEditor();
      } else if (action === "shortcut-registration-guide") {
        guideShortcutRegistration();
      } else if (action === "shortcut-retry-cancel") {
        cancelShortcutRetry();
      } else if (action === "apply") {
        applyCurrentPlan();
      }
    };

    var onKeyDown = function (event) {
      var input = event.target;
      if (
        event.key === "Enter" &&
        input &&
        input.matches &&
        input.matches("[data-source-work-manual]")
      ) {
        setSourceWorkBinding(
          input.getAttribute("data-source-work-manual") || "",
          input.value,
        );
        event.preventDefault();
        return;
      }
      if (
        event.key === "Enter" &&
        input &&
        input.matches &&
        input.matches("[data-landing-recognition-query]")
      ) {
        recognitionState().query = input.value;
        runLandingRecognition();
        event.preventDefault();
        return;
      }
      if (!state.resourcePicker.open) return;
      if (event.key === "Escape") {
        closeResourcePicker();
        event.preventDefault();
        return;
      }
      if (
        event.key === "Enter" &&
        input &&
        input.matches &&
        input.matches("[data-resource-picker-search-input]")
      ) {
        state.resourcePicker.searchDraft = input.value;
        searchResourcePicker();
        event.preventDefault();
      }
    };

    target.addEventListener("input", onInput);
    target.addEventListener("change", onInput);
    target.addEventListener("click", onClick);
    target.addEventListener("keydown", onKeyDown);
    target.addEventListener("toggle", onMoveDetailsToggle, true);
    addCleanup(function () {
      target.removeEventListener("input", onInput);
      target.removeEventListener("change", onInput);
      target.removeEventListener("click", onClick);
      target.removeEventListener("keydown", onKeyDown);
      target.removeEventListener("toggle", onMoveDetailsToggle, true);
      delete target.__NimdaOrganizerBound;
    });
  }

  function startOnce() {
    if (started) return;
    started = true;
    loadConfigAndPreview(true);
  }

  if (typeof window.__NIMDA_ORGANIZER_TEST_HOOK__ === "function") {
    window.__NIMDA_ORGANIZER_TEST_HOOK__({
      state: state,
      render: render,
      planCanExecute: planCanExecute,
      previewPlan: previewPlan,
      previewLandingPlan: previewLandingPlan,
      applyCurrentPlan: applyCurrentPlan,
      applyCurrentLanding: applyCurrentLanding,
      sharedTargetCandidateBundle: sharedTargetCandidateBundle,
      buildSharedTargetBindings: buildSharedTargetBindings,
      setSharedTargetConfirmed: setSharedTargetConfirmed,
      setSharedTargetMemberSelected: setSharedTargetMemberSelected,
      setSharedTargetPressPath: setSharedTargetPressPath,
      setSharedTargetSourceOwner: setSharedTargetSourceOwner,
      beginShortcutOnlyPreview: beginShortcutOnlyPreview,
      previewShortcutRetry: previewShortcutRetry,
      applyShortcutRetry: applyShortcutRetry,
      selectShortcutRepairCandidate: selectShortcutRepairCandidate,
      selectShortcutRepairIndependent: selectShortcutRepairIndependent,
      previewShortcutRepair: previewShortcutRepair,
      applyShortcutRepair: applyShortcutRepair,
      cancelShortcutRetry: cancelShortcutRetry,
      selectSourceWorkMode: selectSourceWorkMode,
      guideUnresolvedCatalog: guideUnresolvedCatalog,
      setSourceMultipleWorkMode: setSourceMultipleWorkMode,
      resetPlanScopedStateForRootChange: resetPlanScopedStateForRootChange,
      selectResourcePickerPath: selectResourcePickerPath,
      sourceWorkBindingRows: sourceWorkBindingRows,
      sourceWorkBindingsPayload: sourceWorkBindingsPayload,
      sourceWorkLandingPayloadIssue: sourceWorkLandingPayloadIssue,
      sourceWorkOverridesPayload: sourceWorkOverridesPayload,
      sourceWorkCandidateKey: sourceWorkCandidateKey,
      setSourceWorkBinding: setSourceWorkBinding,
      selectSourceCatalogBinding: selectSourceCatalogBinding,
      updateSourceCatalogPress: updateSourceCatalogPress,
      openSourceWorkEditor: openSourceWorkEditor,
      updateSourceWorkDraft: updateSourceWorkDraft,
    });
  }

  registry.register({
    id: "media-directory-organizer",
    label: "目录整理",
    tabId: "tab-media-directory-organizer",
    viewId: "media-directory-organizer-view",
    order: 30,
    init: function (nextCtx) {
      featureCtx = nextCtx;
      bindViewOnce();
      render();
    },
    activate: function (nextCtx) {
      featureCtx = nextCtx || featureCtx;
      featureActive = true;
      bindViewOnce();
      render();
      startOnce();
    },
    deactivate: function (nextCtx) {
      featureCtx = nextCtx || featureCtx;
      featureActive = false;
    },
    refreshAfterConfig: function (nextCtx) {
      featureCtx = nextCtx || featureCtx;
      if (featureActive) loadConfigAndPreview(false);
    },
    dispose: function () {
      disposeFeature();
    },
  });
})();
