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

  var state = {
    config: null,
    root: "",
    plan: null,
    routeOverrides: Object.create(null),
    fileWorkOverrides: Object.create(null),
    dirty: false,
    busy: "",
    notice: "",
    noticeError: false,
    execution: null,
    landing: null,
    draftWork: null,
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
    var cards = [
      ["来源目录", summary.source_directory_count || 0],
      ["目标目录", summary.target_directory_count || 0],
      ["路由任务", assignmentCount || 0],
      ["移动文件", summary.file_count || 0],
      ["回退分类", summary.fallback_file_count || 0],
      ["未决文件", unresolvedCount || 0],
      ["总大小", formatBytes(summary.bytes || 0)],
      ["问题", summary.issue_count || 0],
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

  function moveTargetDirectory(move, assignment) {
    var categoryDirectory = firstString(
      move && move.category_relpath,
      relativePathUnder(move && move.category_dir, state.root),
    );
    if (categoryDirectory) return categoryDirectory;
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
    return fallback === "."
      ? firstString(assignment && assignment.target_relpath, assignment && assignment.target_dir, ".")
      : fallback;
  }

  function renderAssignmentMoves(plan, assignment) {
    var moves = plan && Array.isArray(plan.moves) ? plan.moves : [];
    var matched = moves.filter(function (move) {
      return moveBelongsToAssignment(move, assignment);
    });
    if (!matched.length) {
      return '<p class="organizer-move-empty">当前路由没有可显示的文件明细。</p>';
    }
    var groupMap = Object.create(null);
    matched.forEach(function (move) {
      var directory = moveTargetDirectory(move, assignment);
      var key = pathKey(directory) || directory;
      if (!groupMap[key]) {
        groupMap[key] = { directory: directory, moves: [], bytes: 0 };
      }
      groupMap[key].moves.push(move);
      groupMap[key].bytes += Number(move && move.size) || 0;
    });
    var groups = Object.keys(groupMap)
      .map(function (key) {
        return groupMap[key];
      })
      .sort(function (left, right) {
        return String(left.directory).localeCompare(String(right.directory), "zh-CN", {
          numeric: true,
          sensitivity: "base",
        });
      });
    var renderedGroups = groups
      .map(function (group) {
        var visible = group.moves.slice(0, 200);
        var rows = visible.map(function (move) {
          var sourceRelpath = firstString(
            move.source_relpath,
            relativePathUnder(move.source, assignment.source_dir),
          );
          var targetWithinDirectory = firstString(
            move.layout_inner_path,
            relativePathUnder(move.target, move.category_dir),
            move.target_relative_path,
            move.target_relpath,
            relativePathUnder(move.target, assignment.target_dir),
          );
          var rule = structuredText(move.classification_rule);
          var reason = structuredText(
            firstString(
              move.classification_reason,
              move.layout_reason,
              move.reason,
            ),
          );
          var basis = [rule, reason]
            .filter(function (value, index, values) {
              return value && values.indexOf(value) === index;
            })
            .join("；");
          return (
            "<tr><td><code>" +
            esc(sourceRelpath || move.source || "-") +
            "</code></td><td><code>" +
            esc(targetWithinDirectory || move.target || "-") +
            "</code></td><td>" +
            esc(basis || "继承当前路由") +
            "</td></tr>"
          );
        }).join("");
        return (
          '<details class="organizer-target-group"><summary>' +
          '<code class="organizer-target-group-path">' +
          esc(group.directory || ".") +
          "</code>" +
          '<span class="organizer-target-group-meta">' +
          esc(group.moves.length) +
          " 个文件 · " +
          esc(formatBytes(group.bytes)) +
          "</span></summary>" +
          (group.moves.length > visible.length
            ? '<p class="organizer-truncated">该目标目录文件较多，仅显示前 200 条；执行计划仍包含全部 ' +
              esc(group.moves.length) +
              " 条。</p>"
            : "") +
          '<div class="organizer-move-table-wrap"><table class="organizer-move-table">' +
          "<thead><tr><th>来源相对路径</th><th>目标目录内路径</th><th>分类规则 / 依据</th></tr></thead>" +
          "<tbody>" +
          rows +
          "</tbody></table></div></details>"
        );
      })
      .join("");
    return (
      '<details class="organizer-move-details"><summary>文件明细（' +
      esc(matched.length) +
      " 个文件 / " +
      esc(groups.length) +
      " 个目标目录）</summary>" +
      '<div class="organizer-move-groups">' +
      renderedGroups +
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

  function renderUnresolvedFiles(plan) {
    var unresolved = plan && Array.isArray(plan.unresolved_files)
      ? plan.unresolved_files
      : [];
    if (!unresolved.length) {
      return '<p class="organizer-ok">没有待人工选择作品的文件。</p>';
    }
    var disabled = state.busy ? " disabled" : "";
    return (
      '<div class="organizer-unresolved-list">' +
      unresolved
        .map(function (item, index) {
          var source = firstString(item.source);
          var selected = Object.prototype.hasOwnProperty.call(state.fileWorkOverrides, source)
            ? String(state.fileWorkOverrides[source])
            : "";
          var candidates = Array.isArray(item.candidates) ? item.candidates : [];
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
            "</p></div>" +
            '<label><span>人工选择作品</span><select data-file-source="' +
            esc(source) +
            '"' +
            disabled +
            (candidates.length ? "" : " disabled") +
            ">" +
            options +
            "</select></label></article>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function renderIssues(plan) {
    var issues = plan && Array.isArray(plan.issues) ? plan.issues : [];
    if (!issues.length) {
      return '<p class="organizer-ok">没有发现阻止执行的问题。</p>';
    }
    return (
      '<ul class="organizer-issue-list">' +
      issues
        .map(function (issue) {
          return (
            "<li><strong>[" +
            esc(issue.code || "issue") +
            "]</strong> " +
            esc(issue.message || "未知问题") +
            (issue.path ? "<code>" + esc(issue.path) + "</code>" : "") +
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

  function landingRecognitionPayload(query) {
    var root = String(state.root || "").trim();
    var draft = cloneJson(state.draftWork);
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
    groupRegistryOptions().forEach(function (item) {
      var code = String((item && item.code) || "");
      if (!code) return;
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
    return options.join("");
  }

  function renderWorkRegistration(plan) {
    var required = !!(plan && plan.registration_required);
    var draft = state.draftWork;
    if (!required && !draft) return "";
    if (!draft) {
      return '<p class="organizer-empty">正在准备新增作品表单…</p>';
    }
    var date = draft.date && typeof draft.date === "object" ? draft.date : {};
    var presses = Array.isArray(draft.presses) ? draft.presses : [];
    var disabled = state.busy ? " disabled" : "";
    var pressRows = presses
      .map(function (press, index) {
        var sourceNames = Array.isArray(press.source_names) ? press.source_names : [];
        return (
          '<fieldset class="organizer-registration-press"><legend>来源压制 ' +
          esc(index + 1) +
          "</legend>" +
          '<p><strong>来源目录：</strong><code>' +
          esc(sourceNames.join(" / ") || "-") +
          "</code></p>" +
          '<div class="organizer-registration-grid">' +
          '<label><span>压制格式</span><input type="text" data-landing-press="press_format" data-press-index="' +
          esc(index) +
          '" value="' +
          esc(press.press_format || "") +
          '" autocomplete="off"' +
          disabled +
          " /></label>" +
          '<label><span>压制组 / 组合简称</span><select data-landing-press="press_group" data-press-index="' +
          esc(index) +
          '"' +
          disabled +
          ">" +
          renderGroupOptions(press.press_group) +
          "</select></label>" +
          '<label class="organizer-registration-wide"><span>数据库 press_path / 整理目标</span><input type="text" data-landing-press="press_path" data-press-index="' +
          esc(index) +
          '" value="' +
          esc(press.press_path || "") +
          '" autocomplete="off" spellcheck="false"' +
          disabled +
          " /></label>" +
          "</div></fieldset>"
        );
      })
      .join("");
    return (
      '<section class="organizer-section organizer-registration">' +
      "<h2>数据库中没有该作品：手填新增</h2>" +
      "<p>这里只生成完整预览，不会立即写数据库、移动文件或创建快捷方式。</p>" +
      renderLandingRecognition(draft) +
      '<div class="organizer-registration-grid">' +
      '<label><span>作品名</span><input type="text" data-landing-field="name" value="' +
      esc(draft.name || "") +
      '" autocomplete="off"' +
      disabled +
      " /></label>" +
      '<label><span>开始日期</span><input type="date" data-landing-date="start" value="' +
      esc(date.start || "") +
      '"' +
      disabled +
      " /></label>" +
      '<label><span>结束日期（可空）</span><input type="date" data-landing-date="end" value="' +
      esc(date.end || "") +
      '"' +
      disabled +
      " /></label>" +
      '<label><span>作品类型</span><select data-landing-field="domain"' +
      disabled +
      '><option value="animation"' +
      (draft.domain === "animation" ? " selected" : "") +
      '>动画</option><option value="television"' +
      (draft.domain === "television" ? " selected" : "") +
      ">电视剧</option></select></label>" +
      '<label><span>国家</span><select data-landing-field="country"' +
      disabled +
      '><option value="japan"' +
      (draft.country === "japan" ? " selected" : "") +
      '>日本</option><option value="korea"' +
      (draft.country === "korea" ? " selected" : "") +
      '>韩国</option><option value="china"' +
      (draft.country === "china" ? " selected" : "") +
      ">中国</option></select></label>" +
      '<label><span>发行类型</span><select data-landing-field="release_type"' +
      disabled +
      '><option value="tv"' +
      (draft.release_type === "tv" ? " selected" : "") +
      '>TV</option><option value="ova"' +
      (draft.release_type === "ova" ? " selected" : "") +
      '>OVA</option><option value="movie"' +
      (draft.release_type === "movie" ? " selected" : "") +
      ">Movie</option></select></label>" +
      '<label class="organizer-registration-wide"><span>作品目录（自动写入 DB）</span><input type="text" value="' +
      esc(draft.path || state.root) +
      '" readonly /></label></div>' +
      pressRows +
      '<button type="button" class="btn" data-organizer-action="landing-preview"' +
      disabled +
      ">" +
      (state.busy === "landing-preview" ? "生成中…" : "生成“DB → 归类 → 快捷方式”完整预览") +
      "</button></section>"
    );
  }

  function renderLandingSummary() {
    var landing = state.landing;
    if (!landing) return "";
    var catalog = landing.catalog_change || {};
    var shortcutSummary = landing.shortcut_summary || {};
    var shortcuts = Array.isArray(landing.shortcuts) ? landing.shortcuts : [];
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
      esc(catalog.action || "-") +
      "</dd></div><div><dt>数据库文件</dt><dd><code>" +
      esc(catalog.target || "-") +
      "</code></dd></div><div><dt>快捷方式</dt><dd>计划 " +
      esc(shortcutSummary.planned_count || 0) +
      "，冲突 " +
      esc(shortcutSummary.conflict_count || 0) +
      "</dd></div></dl>" +
      '<ul class="organizer-shortcut-plan">' +
      shortcuts
        .map(function (item) {
          return (
            "<li><code>" +
            esc(item.shortcut_path || "-") +
            "</code><span>→</span><code>" +
            esc(item.target_path || "-") +
            "</code><strong>" +
            esc(item.status || "planned") +
            "</strong></li>"
          );
        })
        .join("") +
      "</ul></section>"
    );
  }

  function renderShortcutPending() {
    var pending = state.shortcutPending;
    if (!pending) return "";
    var retry = pending.preview;
    var summary = retry && retry.shortcut_summary ? retry.shortcut_summary : {};
    var rows = retry && Array.isArray(retry.shortcuts) ? retry.shortcuts : [];
    var disabled = state.busy ? " disabled" : "";
    var canApply = !!(
      retry && retry.ready && retry.retry_plan_id && !state.busy
    );
    return (
      '<section class="organizer-section organizer-shortcut-pending">' +
      '<div class="organizer-plan-head"><div><h2>数据库和媒体归类已完成，快捷方式待补建</h2>' +
      '<p>不要重新执行完整落地；这里只会增量检查并创建该作品缺少的快捷方式。</p></div>' +
      '<span class="organizer-ready is-blocked">待处理</span></div>' +
      (pending.error ? '<p class="organizer-pending-error">' + esc(pending.error) + "</p>" : "") +
      (retry
        ? '<p>重试确认 ID：<code>' +
          esc(retry.retry_plan_id || "-") +
          "</code>；计划 " +
          esc(summary.planned_count || 0) +
          "，已存在 " +
          esc(summary.already_exists_count || 0) +
          "，冲突 " +
          esc(summary.conflict_count || 0) +
          "。</p>"
        : "") +
      (rows.length
        ? '<ul class="organizer-shortcut-plan">' +
          rows
            .map(function (item) {
              return (
                "<li><code>" +
                esc(item.shortcut_path || "-") +
                "</code><span>→</span><code>" +
                esc(item.target_path || "-") +
                "</code><strong>" +
                esc(item.status || "planned") +
                "</strong></li>"
              );
            })
            .join("") +
          "</ul>"
        : "") +
      '<div class="organizer-pending-actions">' +
      '<button type="button" class="btn secondary" data-organizer-action="shortcut-retry-preview"' +
      disabled +
      ">" +
      (state.busy === "shortcut-retry-preview" ? "检查中…" : "重新检查快捷方式") +
      "</button>" +
      '<button type="button" class="btn danger" data-organizer-action="shortcut-retry-apply"' +
      (canApply ? "" : " disabled") +
      ">" +
      (state.busy === "shortcut-retry-apply" ? "补建中…" : "确认补建快捷方式") +
      "</button></div></section>"
    );
  }

  function renderExecution(execution) {
    if (!execution) return "";
    var warnings = Array.isArray(execution.cleanup_warnings) ? execution.cleanup_warnings : [];
    return (
      '<section class="organizer-execution-result" aria-live="polite">' +
      "<h2>执行完成</h2>" +
      "<p>已移动 <strong>" +
      esc(execution.moved_file_count || 0) +
      "</strong> 个文件，共 " +
      esc(formatBytes(execution.moved_bytes || 0)) +
      "。</p>" +
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
    var issues = plan && Array.isArray(plan.issues) ? plan.issues : [];
    var unresolved = plan && Array.isArray(plan.unresolved_files)
      ? plan.unresolved_files
      : [];
    return !!(
      plan &&
      plan.ready &&
      plan.plan_id &&
      !issues.length &&
      !unresolved.length &&
      !state.dirty &&
      !state.busy
    );
  }

  function renderPlan(plan) {
    if (!plan) {
      return '<p class="organizer-empty">输入作品根目录后点击“重新预览”。预览只读取磁盘，不会移动文件。</p>';
    }
    var familyWorks = Array.isArray(plan.family_works) ? plan.family_works : [];
    return (
      '<section class="organizer-plan" aria-label="目录整理计划">' +
      '<div class="organizer-plan-head">' +
      "<div><h2>计划摘要</h2><p>计划 ID：<code>" +
      esc(plan.plan_id || "-") +
      "</code></p></div>" +
      '<span class="organizer-ready ' +
      (plan.ready ? "is-ready" : "is-blocked") +
      '">' +
      (plan.ready ? "可执行" : "不可执行") +
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
      '<section class="organizer-section"><h2>来源与目标</h2>' +
      renderAssignments(plan) +
      "</section>" +
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
    var config = state.config || {};
    var paths = config.paths && typeof config.paths === "object" ? config.paths : {};
    var roots = allowedRoots(config);
    var executeDisabled = planCanExecute() ? "" : " disabled";
    var previewDisabled = state.busy || state.shortcutPending ? " disabled" : "";
    var inputDisabled = state.busy || state.shortcutPending ? " disabled" : "";
    var noticeClass = state.noticeError ? " is-error" : "";
    target.innerHTML =
      '<section class="organizer-panel">' +
      '<header class="organizer-header">' +
      "<div><h1>媒体目录整理</h1>" +
      "<p>先预览并逐项核对目标目录；执行时不会改文件名，也不会覆盖已有文件。</p></div>" +
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
      "</button>" +
      "</div>" +
      '<p class="organizer-dirty"' +
      (state.dirty ? "" : " hidden") +
      ">目标目录或作品根目录已经编辑；必须重新预览，当前计划不能执行。</p>" +
      '<p class="organizer-notice' +
      noticeClass +
      '" aria-live="polite">' +
      esc(state.notice) +
      "</p>" +
      renderExecution(state.execution) +
      renderShortcutPending() +
      renderWorkRegistration(state.plan) +
      renderLandingSummary() +
      renderPlan(state.plan) +
      '<section class="organizer-execute-panel">' +
      "<div><h2>执行计划</h2><p>执行前依次核对摘要、输入完整计划 ID，并进行最终确认。</p></div>" +
      '<button type="button" class="btn danger" data-organizer-action="apply"' +
      executeDisabled +
      ">" +
      (state.busy === "apply"
        ? "执行中..."
        : state.landing
        ? "确认完整落地"
        : "执行移动") +
      "</button>" +
      "</section>" +
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
    state.root = path;
    state.routeOverrides = Object.create(null);
    state.fileWorkOverrides = Object.create(null);
    state.landing = null;
    state.draftWork = null;
    state.shortcutPending = null;
    invalidateLandingRecognition("", "");
    state.resourcePicker = createResourcePickerState(false);
    invalidatePreview("已从资源库选择作品根目录，请重新预览。当前未移动任何文件。");
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
        state.root = configuredRoot;
        state.routeOverrides = Object.create(null);
        state.fileWorkOverrides = Object.create(null);
        state.landing = null;
        state.draftWork = null;
        state.shortcutPending = null;
        invalidateLandingRecognition("", "");
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
    invalidateLandingRecognition(recognitionState().query, "");
    var serial = ++requestSerial;
    state.busy = "preview";
    state.notice = "正在扫描目录并重新计算计划...";
    state.noticeError = false;
    state.execution = null;
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
        }),
      });
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false || !out.data.plan) {
        throw new Error(responseError(out, "目录整理预览失败"));
      }
      state.plan = out.data.plan;
      state.landing = null;
      if (
        state.plan.registration_required &&
        state.plan.registration &&
        state.plan.registration.draft
      ) {
        state.draftWork = cloneJson(state.plan.registration.draft);
        state.recognition = createRecognitionState(state.draftWork.name || "");
      } else {
        state.draftWork = null;
        state.recognition = createRecognitionState("");
      }
      state.root = firstString(state.plan.root, root);
      state.dirty = false;
      state.busy = "";
      var issueCount = Array.isArray(state.plan.issues) ? state.plan.issues.length : 0;
      var unresolvedCount = Array.isArray(state.plan.unresolved_files)
        ? state.plan.unresolved_files.length
        : 0;
      state.notice = issueCount || unresolvedCount
        ? state.plan.registration_required
          ? "数据库中没有找到该作品。请填写新增作品信息，再生成完整落地预览；当前没有写入或移动任何内容。"
          : "预览完成，但存在 " +
            issueCount +
            " 个问题、" +
            unresolvedCount +
            " 个未决文件；修正并重新预览后才能执行。"
        : "预览完成。请逐项核对来源和目标目录。";
      state.noticeError = !state.plan.registration_required && (issueCount > 0 || unresolvedCount > 0);
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
    var draft = state.draftWork;
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
    state.draftWork = cloneJson(candidate.proposed_work);
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
    if (!root || !state.draftWork) {
      setNotice("请先输入作品根目录，并通过普通预览生成手填作品表单。", true);
      return;
    }
    var draft = cloneJson(state.draftWork);
    draft.path = root;
    var serial = ++requestSerial;
    state.busy = "landing-preview";
    state.notice = "正在重新校验数据库写入、媒体归类和快捷方式计划…";
    state.noticeError = false;
    state.execution = null;
    render();
    setSharedStatus("完整落地计划预览中…", false);
    try {
      var out = await fetchJson("/api/media-directory-organizer/landing/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ root: root, draft_work: draft }),
      });
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false || !out.data.organizer_plan) {
        throw new Error(responseError(out, "完整落地预览失败"));
      }
      state.root = firstString(out.data.root, root);
      state.landing = out.data;
      state.plan = out.data.organizer_plan;
      state.draftWork = draft;
      state.routeOverrides = Object.create(null);
      state.fileWorkOverrides = Object.create(null);
      state.dirty = false;
      state.busy = "";
      state.notice = out.data.ready
        ? "完整落地预览已生成。请核对数据库文件、每条媒体移动和每个快捷方式，再确认执行。"
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
    var shortcuts = landing.shortcut_summary || {};
    return (
      "请再次核对这次完整落地：\n\n" +
      "作品：" + String((landing.draft_work && landing.draft_work.name) || "") +
      "\n数据库动作：" + String(catalog.action || "-") +
      "\n数据库文件：" + String(catalog.target || "-") +
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
        "最终确认：现在会先新增作品数据库记录，再执行预览中的媒体移动，最后仅增量创建该作品的快捷方式。不会覆盖已有文件或清空其他快捷方式。\n\n确定要继续吗？",
      )
    ) {
      return;
    }

    var originalDraft = cloneJson(state.draftWork);
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
        body: JSON.stringify({
          root: state.root,
          draft_work: originalDraft,
          landing_plan_id: landing.landing_plan_id,
          confirmation: String(typed).trim(),
          acknowledge_catalog_write: true,
          acknowledge_move: true,
          acknowledge_shortcuts: true,
        }),
      });
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data) {
        throw new Error(responseError(out, "作品完整落地失败"));
      }
      if (out.data.state === "shortcut_pending") {
        state.execution = out.data.media || null;
        state.shortcutPending = {
          root: state.root,
          draft_work: originalDraft,
          error: out.data.shortcut_error || "快捷方式创建失败",
          retry: out.data.shortcut_retry || {},
          preview: null,
        };
        state.plan = null;
        state.landing = null;
        state.draftWork = null;
        invalidateLandingRecognition("", "");
        state.dirty = false;
        state.busy = "";
        state.notice =
          "作品数据库和媒体归类已经完成，但快捷方式尚未完成。请使用下方“重新检查快捷方式”，不要再次执行完整落地。";
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
      state.shortcutPending = null;
      invalidateLandingRecognition("", "");
      state.routeOverrides = Object.create(null);
      state.fileWorkOverrides = Object.create(null);
      state.dirty = false;
      state.busy = "";
      state.notice = "作品已完整落地：数据库、媒体归类和该作品快捷方式均已完成。";
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
        "/api/media-directory-organizer/landing/shortcuts/preview",
        {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify({
            root: pending.root,
            draft_work: pending.draft_work,
          }),
        },
      );
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "快捷方式重试预览失败"));
      }
      pending.preview = out.data;
      pending.error = "";
      state.busy = "";
      state.notice = out.data.ready
        ? "快捷方式重试预览完成；核对后可单独补建。"
        : "快捷方式仍有冲突，未创建任何内容。";
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
    if (!retry || !retry.ready || !retry.retry_plan_id) {
      setNotice("请先重新检查快捷方式并解决冲突。", true);
      return;
    }
    var typed = window.prompt(
      "请输入快捷方式重试确认 ID。此操作只会补建该作品的快捷方式：\n\n" +
        retry.retry_plan_id,
      "",
    );
    if (typed === null) return;
    if (String(typed).trim() !== String(retry.retry_plan_id)) {
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
        "/api/media-directory-organizer/landing/shortcuts/apply",
        {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify({
            root: pending.root,
            draft_work: pending.draft_work,
            retry_plan_id: retry.retry_plan_id,
            confirmation: String(typed).trim(),
            acknowledge_shortcuts: true,
          }),
        },
      );
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "快捷方式补建失败"));
      }
      state.shortcutPending = null;
      state.busy = "";
      state.notice = "快捷方式补建完成；该作品现在已完整落地。";
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

  function confirmationSummary(plan) {
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
      "\n计划 ID：" +
      String(plan.plan_id || "") +
      "\n\n这是第一次确认。点击“确定”后仍不会立即移动文件。"
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
        "最终确认：现在将按计划移动文件。执行过程中不会覆盖已有文件。\n\n确定要继续吗？",
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
      var out = await fetchJson("/api/media-directory-organizer/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({
          root: state.root,
          target_overrides: legacyTargetOverrides(),
          route_target_overrides: state.routeOverrides,
          file_work_overrides: state.fileWorkOverrides,
          plan_id: plan.plan_id,
          confirmation: String(typed).trim(),
          acknowledge_move: true,
        }),
      });
      if (serial !== requestSerial) return;
      if (!out.res.ok || !out.data || out.data.ok === false) {
        throw new Error(responseError(out, "目录整理执行失败"));
      }
      state.execution = out.data.execution || out.data.result || out.data;
      state.plan = null;
      state.routeOverrides = Object.create(null);
      state.fileWorkOverrides = Object.create(null);
      state.dirty = false;
      state.busy = "";
      state.notice = "执行完成。需要继续检查时，请重新生成预览。";
      state.noticeError = false;
      render();
      setSharedStatus("目录整理执行完成。", false);
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
      if (input.id === "organizer-root-input") {
        var previousRoot = state.root;
        state.root = input.value;
        if (String(previousRoot).trim() !== String(state.root).trim()) {
          state.routeOverrides = Object.create(null);
          state.fileWorkOverrides = Object.create(null);
          state.landing = null;
          state.draftWork = null;
          state.shortcutPending = null;
          invalidateLandingRecognition("", "作品根目录已变化，旧识别候选已作废。");
          invalidatePreview("作品根目录已编辑，请重新预览。");
        }
        return;
      }
      if (input.matches("[data-landing-field]") && state.draftWork) {
        var field = String(input.getAttribute("data-landing-field") || "");
        if (!field) return;
        var previousValue = state.draftWork[field];
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
        state.draftWork[field] = input.value;
        state.landing = null;
        invalidateLandingRecognition(
          nextRecognitionQuery,
          hadFieldCandidates ? "作品信息已变化，旧识别候选已作废；请重新识别。" : "",
        );
        invalidatePreview("新增作品信息已编辑，请重新生成完整落地预览。");
        return;
      }
      if (input.matches("[data-landing-date]") && state.draftWork) {
        var dateField = String(input.getAttribute("data-landing-date") || "");
        if (!dateField) return;
        if (!state.draftWork.date || typeof state.draftWork.date !== "object") {
          state.draftWork.date = {};
        }
        var dateRecognition = recognitionState();
        var hadDateCandidates = recognitionHasTransientState(dateRecognition);
        state.draftWork.date[dateField] = input.value;
        state.landing = null;
        invalidateLandingRecognition(
          dateRecognition.query,
          hadDateCandidates ? "作品日期已变化，旧识别候选已作废；请重新识别。" : "",
        );
        invalidatePreview("作品日期已编辑，请重新生成完整落地预览。");
        return;
      }
      if (input.matches("[data-landing-press]") && state.draftWork) {
        var pressField = String(input.getAttribute("data-landing-press") || "");
        var pressIndex = Number(input.getAttribute("data-press-index"));
        var draftPresses = Array.isArray(state.draftWork.presses)
          ? state.draftWork.presses
          : [];
        if (!pressField || !Number.isInteger(pressIndex) || !draftPresses[pressIndex]) return;
        var pressRecognition = recognitionState();
        var hadPressCandidates = recognitionHasTransientState(pressRecognition);
        draftPresses[pressIndex][pressField] =
          pressField === "press_group" ? String(input.value || "").toUpperCase() : input.value;
        state.landing = null;
        invalidateLandingRecognition(
          pressRecognition.query,
          hadPressCandidates ? "压制信息已变化，旧识别候选已作废；请重新识别。" : "",
        );
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
      if (action === "reload-config") {
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
      } else if (action === "landing-recognition-run") {
        runLandingRecognition();
      } else if (action === "landing-recognition-apply") {
        applyLandingRecognitionCandidate(button.getAttribute("data-recognition-candidate-id") || "");
      } else if (action === "landing-preview") {
        previewLandingPlan();
      } else if (action === "shortcut-retry-preview") {
        previewShortcutRetry();
      } else if (action === "shortcut-retry-apply") {
        applyShortcutRetry();
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
    addCleanup(function () {
      target.removeEventListener("input", onInput);
      target.removeEventListener("change", onInput);
      target.removeEventListener("click", onClick);
      target.removeEventListener("keydown", onKeyDown);
      delete target.__NimdaOrganizerBound;
    });
  }

  function startOnce() {
    if (started) return;
    started = true;
    loadConfigAndPreview(true);
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
