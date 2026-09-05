(function () {
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

  var root = ensureFeatureRegistry();

  var featureCtx = null;
  var featureActive = true;
  var cleanupCallbacks = [];
  var linkIndexBound = false;
  var subtabBound = false;
  var linkIndexState = null;
  var linkIndexLoading = false;
  var linkIndexOperationNotice = null;
  var linkIndexSelectedPath = "";
  var linkIndexCollapsedPaths = {};
  var linkIndexTooltipEl = null;
  var linkIndexTooltipTarget = null;
  var linkIndexTooltipPosition = null;
  var resourceRootDrafts = null;
  var resourceExcludeDrafts = null;
  var resourceScanState = null;
  var resourceScanLoading = false;
  var resourceSearchState = null;
  var resourceSearchLoading = false;
  var resourceNodeLoadingPaths = {};
  var resourceConfigSaving = false;
  var resourceTreeCollapsedPaths = {};
  var resourceSearchCollapsedPaths = {};
  var resourceSelectedPath = "";
  var resourceTreeSearchDraft = "";
  var resourceTreeSearchKeyword = "";
  var resourceRootConfigCollapsed = true;
  var resourceTreeWidth = Number(readPreference("nimda.resourceTreeWidth") || "360");
  var resourceTreeResizing = false;
  var linkIndexTreeWidth = Number(readPreference("nimda.linkIndexTreeWidth") || "360");
  var linkIndexTreeResizing = false;
  var linkIndexGroupByEmptyPath =
    readPreference("nimda.linkIndexGroupByEmptyPath") === "1" ||
    readPreference("nimda.linkIndexEmptyPathOnly") === "1";
  var activeSubtab = "list";
  var linkIndexRequestSerial = 0;
  var resourceRequestSerial = 0;
  var resourceSearchSerial = 0;

  function readPreference(key) {
    try {
      return localStorage.getItem(key);
    } catch (_e) {
      return null;
    }
  }

  function writePreference(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (_e) {}
  }

  function resourceRequestCurrent(serial) {
    return serial === resourceRequestSerial && resourcePanelActive();
  }

  function beginResourceRead() {
    resourceRequestSerial += 1;
    resourceSearchSerial += 1;
    resourceSearchLoading = false;
    resourceNodeLoadingPaths = {};
    resourceScanLoading = true;
    return resourceRequestSerial;
  }

  function ctx() {
    return featureCtx || {};
  }

  function esc(s) {
    if (ctx().esc) return ctx().esc(s);
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setStatus(msg, isErr) {
    if (ctx().setStatus) ctx().setStatus(msg, !!isErr);
  }

  function addDocumentListener(type, handler) {
    document.addEventListener(type, handler);
    cleanupCallbacks.push(function () {
      document.removeEventListener(type, handler);
    });
  }

  function releaseResourcePayload() {
    resourceRequestSerial += 1;
    resourceSearchSerial += 1;
    resourceScanState = null;
    resourceScanLoading = false;
    resourceSearchState = null;
    resourceSearchLoading = false;
    resourceNodeLoadingPaths = {};
    resourceSelectedPath = "";
    resourceTreeSearchKeyword = "";
    resourceSearchCollapsedPaths = {};
  }

  function releaseLinkIndexPayload() {
    linkIndexRequestSerial += 1;
    linkIndexState = null;
    linkIndexLoading = false;
    linkIndexOperationNotice = null;
    linkIndexSelectedPath = "";
    hideLinkTooltip();
  }

  function releaseLargePayloads() {
    releaseResourcePayload();
    releaseLinkIndexPayload();
  }

  function linkIndexPanelActive() {
    return featureActive && activeSubtab === "index";
  }

  function resourcePanelActive() {
    return featureActive && activeSubtab === "resource";
  }

  function disposeCollectionDetailFeature() {
    featureActive = false;
    releaseLargePayloads();
    hideLinkTooltip();
    while (cleanupCallbacks.length) {
      try {
        cleanupCallbacks.pop()();
      } catch (_e) {}
    }
    linkIndexBound = false;
    subtabBound = false;
  }

  function setLinkIndexNotice(msg, isErr) {
    linkIndexOperationNotice = msg
      ? {
          message: String(msg),
          is_error: !!isErr,
        }
      : null;
  }

  async function fetchJson(url, opts) {
    if (ctx().fetchJson) return ctx().fetchJson(url, opts);
    var res = await fetch(url, opts || {});
    var data = await res.json().catch(function () {
      return {};
    });
    return { res: res, data: data };
  }

  function slot() {
    return document.getElementById("collection-detail-link-index-slot");
  }

  function resourceSlot() {
    return document.getElementById("collection-detail-resource-slot");
  }

  function panelEventRoot(target) {
    var linkRoot = slot();
    var resRoot = resourceSlot();
    if (linkRoot && linkRoot.contains(target)) return linkRoot;
    if (resRoot && resRoot.contains(target)) return resRoot;
    return null;
  }

  function setCollectionDetailSubtab(name) {
    var previousSubtab = activeSubtab;
    activeSubtab = name === "index" || name === "resource" ? name : "list";
    var releasedPayload = false;
    if (previousSubtab === "resource" && activeSubtab !== "resource") {
      releaseResourcePayload();
      releasedPayload = true;
    }
    if (previousSubtab === "index" && activeSubtab !== "index") {
      releaseLinkIndexPayload();
      releasedPayload = true;
    }
    var listPanel = document.getElementById("collection-detail-list-panel");
    var indexPanel = document.getElementById("collection-detail-index-panel");
    var resourcePanel = document.getElementById("collection-detail-resource-panel");
    Array.prototype.slice
      .call(document.querySelectorAll("[data-collection-detail-subtab]"))
      .forEach(function (btn) {
        var isActive = btn.getAttribute("data-collection-detail-subtab") === activeSubtab;
        btn.classList.toggle("is-active", isActive);
        btn.setAttribute("aria-selected", isActive ? "true" : "false");
      });
    if (listPanel) listPanel.hidden = activeSubtab !== "list";
    if (indexPanel) indexPanel.hidden = activeSubtab !== "index";
    if (resourcePanel) resourcePanel.hidden = activeSubtab !== "resource";
    if (releasedPayload) renderLinkIndexPanel();
    if (linkIndexPanelActive() && !linkIndexState && !linkIndexLoading) {
      loadLinkIndex().catch(function (e) {
        setStatus("索引目录读取失败：" + (e.message || String(e)), true);
      });
    }
    if (resourcePanelActive() && !resourceScanState && !resourceScanLoading) {
      loadResourceScanCache().catch(function (e) {
        setStatus("资源库缓存读取失败：" + (e.message || String(e)), true);
      });
    }
  }

  function bindSubtabsOnce() {
    if (subtabBound) return;
    subtabBound = true;
    addDocumentListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var btn = t.closest("[data-collection-detail-subtab]");
      if (!btn) return;
      setCollectionDetailSubtab(btn.getAttribute("data-collection-detail-subtab"));
    });
  }

  function summaryText(data) {
    var summary = (data && data.plan_summary) || {};
    return (
      "索引 " +
      (summary.total || 0) +
      " 项，空路径 " +
      (summary.empty_target_path || 0) +
      " 项，磁盘未关联 " +
      (summary.unmapped_on_disk || 0) +
      " 项"
    );
  }

  function configuredResourceRoots(data) {
    var cfg = (data && data.config) || {};
    var roots = Array.isArray(cfg.resource_roots) ? cfg.resource_roots : [];
    return roots
      .map(function (x) {
        return String(x || "").trim();
      })
      .filter(Boolean);
  }

  function configuredResourceExcludes(data) {
    var cfg = (data && data.config) || {};
    var raw = cfg.resource_excludes && typeof cfg.resource_excludes === "object" ? cfg.resource_excludes : {};
    var out = {};
    Object.keys(raw).forEach(function (key) {
      var values = Array.isArray(raw[key]) ? raw[key] : [];
      out[String(key || "").trim().toLowerCase()] = values
        .map(function (x) {
          return String(x || "").trim();
        })
        .filter(Boolean);
    });
    return out;
  }

  function resourceExcludesForRoot(data, root) {
    var map = configuredResourceExcludes(data);
    var key = String(root || "").trim().toLowerCase();
    return map[key] || [];
  }

  function ensureResourceRootDrafts(data) {
    if (resourceRootDrafts !== null) return;
    var roots = configuredResourceRoots(data);
    resourceRootDrafts = roots;
    resourceExcludeDrafts = roots.map(function (root) {
      return resourceExcludesForRoot(data, root).join(", ");
    });
    if (!resourceRootDrafts.length && !(data && data.config)) {
      resourceRootDrafts = null;
      resourceExcludeDrafts = null;
      return;
    }
    if (!resourceRootDrafts.length) {
      resourceRootDrafts = [""];
      resourceExcludeDrafts = [""];
    }
  }

  function syncResourceRootDraftsFromConfig(data) {
    var roots = configuredResourceRoots(data);
    if (!roots.length) return;
    var isEmptyDraft =
      resourceRootDrafts === null ||
      (resourceRootDrafts.length === 1 && !String(resourceRootDrafts[0] || "").trim());
    if (isEmptyDraft) {
      resourceRootDrafts = roots;
      resourceExcludeDrafts = roots.map(function (root) {
        return resourceExcludesForRoot(data, root).join(", ");
      });
    }
  }

  function resourceScanSummaryText(scan) {
    var summary = (scan && scan.summary) || {};
    if (!scan || !scan.ok) return resourceScanLoading ? "扫描中..." : "未扫描";
    return (
      (scan.cached ? "缓存：" : "") +
      "资源库 " +
      (summary.existing_root_count || 0) +
      " / " +
      (summary.root_count || 0) +
      "，系列 " +
      (summary.series_count || 0) +
      "，资源 " +
      (summary.item_count || 0) +
      (summary.truncated ? "，已达到上限 " + (summary.max_dirs || 0) : "")
    );
  }

  function resourceTreeRelpath(node) {
    return String((node && node.relpath) || "");
  }

  function folderChildren(node) {
    return (Array.isArray(node && node.children) ? node.children : []).filter(function (child) {
      return child && child.type !== "link" && child.type !== "file";
    });
  }

  function nodeFiles(node) {
    return Array.isArray(node && node.files) ? node.files : [];
  }

  function resourceNodeHasChildren(node) {
    return folderChildren(node).length > 0 || !!(node && node.has_children);
  }

  function resourceNodeLoaded(node) {
    return !!(node && node.children_loaded);
  }

  function resourceTreeSearchQuery() {
    return String(resourceTreeSearchKeyword || "").trim();
  }

  function resourceCollapseState() {
    return resourceTreeSearchQuery() ? resourceSearchCollapsedPaths : resourceTreeCollapsedPaths;
  }

  function replaceResourceCollapseState(paths) {
    if (resourceTreeSearchQuery()) resourceSearchCollapsedPaths = paths;
    else resourceTreeCollapsedPaths = paths;
  }

  function resourceNodeDirectChildCount(node) {
    var n = Number(node && node.child_count);
    return Number.isFinite(n) ? n : folderChildren(node).length;
  }

  function resourceNodeDirectFileCount(node) {
    var n = Number(node && node.direct_file_count);
    return Number.isFinite(n) ? n : nodeFiles(node).length;
  }

  function resourceNodeDirectTotalCount(node) {
    var n = Number(node && node.direct_child_count);
    if (Number.isFinite(n)) return n;
    return resourceNodeDirectChildCount(node) + resourceNodeDirectFileCount(node);
  }

  function resourceNodeTotalChildCount(node) {
    var n = Number(node && node.total_child_count);
    if (Number.isFinite(n)) return n;
    return (Number(node && node.dir_count) || 0) + (Number(node && node.file_count) || 0);
  }

  function resourceNodeSize(node) {
    var n = Number(node && node.size);
    return Number.isFinite(n) ? n : 0;
  }

  function formatFileSize(size) {
    var n = Number(size || 0);
    if (!n) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var idx = 0;
    while (n >= 1024 && idx < units.length - 1) {
      n = n / 1024;
      idx++;
    }
    return (idx === 0 ? String(Math.round(n)) : n.toFixed(n >= 10 ? 1 : 2)) + " " + units[idx];
  }

  function formatTimestamp(seconds) {
    var n = Number(seconds || 0);
    if (!n) return "";
    var d = new Date(n * 1000);
    if (Number.isNaN(d.getTime())) return "";
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function formatTimestampDetail(seconds) {
    var n = Number(seconds || 0);
    if (!n) return "";
    var d = new Date(n * 1000);
    if (Number.isNaN(d.getTime())) return "";
    return (
      d.getFullYear() +
      "-" +
      String(d.getMonth() + 1).padStart(2, "0") +
      "-" +
      String(d.getDate()).padStart(2, "0") +
      " " +
      String(d.getHours()).padStart(2, "0") +
      ":" +
      String(d.getMinutes()).padStart(2, "0") +
      ":" +
      String(d.getSeconds()).padStart(2, "0")
    );
  }

  function renderResourceTreeStats(node) {
    node = node || {};
    var directTotal = resourceNodeDirectTotalCount(node);
    var totalChildren = resourceNodeTotalChildCount(node);
    return (
      '<span class="link-tree-stat">' +
      esc(formatFileSize(resourceNodeSize(node))) +
      '</span><span class="link-tree-stat">' +
      esc(formatTimestamp(node.mtime)) +
      '</span><span class="link-tree-stat">' +
      esc(String(directTotal || 0)) +
      '</span><span class="link-tree-stat">' +
      esc(String(totalChildren || 0)) +
      '</span><span class="link-tree-meta" title="' +
      esc(node.path || node.error || "") +
      '">' +
      esc(node.error || node.path || "") +
      "</span>"
    );
  }

  function resourceTreeRoot(scan) {
    return (scan && scan.tree) || { type: "folder", name: "资源库", relpath: "", path: "", children: [] };
  }

  function resourceDisplayTree(scan) {
    var tree = resourceTreeRoot(scan);
    var query = resourceTreeSearchQuery();
    if (!query) return tree;
    if (query && resourceSearchState && resourceSearchState.ok && String(resourceSearchState.query || "") === query) {
      return resourceTreeRoot(resourceSearchState);
    }
    return tree;
  }

  function selectedResourceFolder(scan, tree) {
    tree = tree || resourceDisplayTree(scan);
    var node = findTreeNodeByRelpath(tree, resourceSelectedPath);
    if (!node || node.type === "resource") {
      resourceSelectedPath = "";
      node = tree;
    }
    return node || {};
  }

  function resourceChildStats(node) {
    var children = folderChildren(node);
    var files = nodeFiles(node);
    var folders = resourceNodeDirectChildCount(node);
    var fileCount = resourceNodeDirectFileCount(node);
    return {
      folders: folders,
      resources: 0,
      files: fileCount,
      total: resourceNodeDirectTotalCount(node),
      all: resourceNodeTotalChildCount(node),
      size: resourceNodeSize(node),
      loadedFolders: children.length,
      loadedFiles: files.length,
    };
  }

  function resourceFullTreePath(rootNode, node) {
    var rel = resourceTreeRelpath(node);
    if (!rel) return "资源库目录树";
    if (node && node.path) return String(node.path);
    if (node && node.name) return String(node.name);
    return rel;
  }

  function renderResourceTreeRootChildren(tree) {
    var children = folderChildren(tree);
    if (!children.length) {
      return resourceTreeSearchQuery()
        ? '<p class="link-index-empty">没有找到包含该关键字的目录或文件。</p>'
        : '<p class="link-index-empty">暂无资源库子项。</p>';
    }
    return children
      .map(function (child) {
        return renderResourceTreeNode(child, 0);
      })
      .join("");
  }

  function renderResourceTreeToolbar(scan) {
    var query = resourceTreeSearchQuery();
    return (
      '<div class="link-index-tree-toolbar">' +
      '<span class="link-index-tree-title">资源库目录树' +
      (scan && scan.scanned_at ? "（" + esc(scan.scanned_at) + "）" : "") +
      "</span>" +
      '<div class="link-index-tree-actions">' +
      '<label class="resource-tree-search"><span>搜索</span>' +
      '<input type="search" value="' +
      esc(resourceTreeSearchDraft) +
      '" placeholder="目录或文件名" data-resource-tree-search-input />' +
      "</label>" +
      '<button type="button" class="link-index-browser-tool-btn" data-resource-action="search-tree"' +
      (resourceSearchLoading ? " disabled" : "") +
      ">" +
      (resourceSearchLoading ? "搜索中..." : "搜索") +
      "</button>" +
      (query
        ? '<button type="button" class="link-index-browser-tool-btn" data-resource-action="clear-search">清除</button>'
        : "") +
      '<button type="button" class="link-index-browser-tool-btn" data-resource-action="expand-tree">全部展开</button>' +
      '<button type="button" class="link-index-browser-tool-btn" data-resource-action="collapse-tree">全部折叠</button>' +
      "</div></div>"
    );
  }

  function renderResourceTreeHeader() {
    return (
      '<div class="link-tree-header resource-tree-header" aria-hidden="true">' +
      "<span></span><span></span><span>名称</span><span>大小</span><span>修改时间</span><span>子文件</span><span>全部</span><span>路径</span>" +
      "</div>"
    );
  }

  function renderResourceTreeNode(node, depth) {
    if (!node || typeof node !== "object") return "";
    if (node.type === "file") return "";
    var children = folderChildren(node);
    var relpath = resourceTreeRelpath(node);
    var hasChildren = resourceNodeHasChildren(node);
    var loaded = resourceNodeLoaded(node);
    var loading = !!resourceNodeLoadingPaths[relpath];
    var collapsed = hasChildren && (resourceCollapseState()[relpath] === true || !loaded);
    var selectedClass = relpath === resourceSelectedPath ? " is-selected" : "";
    return (
      '<div class="link-tree-folder-wrap resource-tree-folder-wrap">' +
      '<div class="link-tree-folder-row resource-tree-folder-row dynatree-node dynatree-folder ' +
      (hasChildren ? (collapsed ? "dynatree-exp-c dynatree-ico-cf" : "dynatree-exp-e dynatree-ico-ef") : "dynatree-exp-n dynatree-ico-cf") +
      selectedClass +
      '" style="--tree-depth:' +
      esc(String(depth || 0)) +
      '" data-resource-tree-select="' +
      esc(relpath) +
      '">' +
      (hasChildren
        ?
      '<button type="button" class="link-tree-toggle ' +
      (collapsed ? "is-collapsed" : "is-expanded") +
          ' dynatree-expander' +
      '" data-resource-tree-toggle aria-label="展开或折叠" aria-expanded="' +
      (collapsed ? "false" : "true") +
      '">' +
      (loading ? "..." : collapsed ? "+" : "-") +
          "</button>"
        : '<span class="link-tree-toggle-spacer dynatree-connector" aria-hidden="true"></span>') +
      '<span class="link-tree-file-icon dynatree-icon is-folder' +
      (collapsed || !hasChildren ? "" : " is-open") +
      '" aria-hidden="true"></span>' +
      '<span class="link-tree-name" title="' +
      esc(node.path || node.error || "") +
      '">' +
      esc(node.name || "") +
      "</span>" +
      renderResourceTreeStats(node) +
      "</div>" +
      (hasChildren
        ? '<div class="link-tree-children"' +
          (collapsed ? " hidden" : "") +
          ">" +
          (collapsed ? "" : loaded
            ? children
                .map(function (child) {
                  return renderResourceTreeNode(child, (depth || 0) + 1);
                })
                .join("")
            : '<p class="link-index-empty">目录读取中...</p>') +
          "</div>"
        : "") +
      "</div>"
    );
  }

  function renderResourceFolderRows(rootNode, node) {
    var rows = "";
    var children = folderChildren(node);
    var files = nodeFiles(node);
    var relpath = resourceTreeRelpath(node);
    if (relpath) {
      var parentPath = parentRelpath(relpath);
      rows +=
        '<tr class="link-index-file-row is-parent" data-resource-tree-select="' +
        esc(parentPath) +
        '">' +
        '<td><button type="button" class="link-index-file-name" data-resource-tree-select="' +
        esc(parentPath) +
        '">' +
        '<span class="link-index-file-icon is-folder" aria-hidden="true"></span><span>..</span></button></td>' +
        "<td>上级目录</td><td></td><td></td><td></td><td></td><td></td></tr>";
    }
    if (!resourceNodeLoaded(node)) {
      rows += '<tr><td colspan="7" class="link-index-table-empty">目录读取中...</td></tr>';
      return rows;
    }
    for (var i = 0; i < children.length; i++) {
      var child = children[i] || {};
      rows +=
        '<tr class="link-index-file-row is-folder" data-resource-tree-select="' +
        esc(resourceTreeRelpath(child)) +
        '">' +
        '<td><button type="button" class="link-index-file-name" data-resource-tree-select="' +
        esc(resourceTreeRelpath(child)) +
        '">' +
        '<span class="link-index-file-icon is-folder" aria-hidden="true"></span><span title="' +
        esc(child.path || child.error || "") +
        '">' +
        esc(child.name || "") +
        "</span></button></td>" +
        "<td>文件夹</td><td>" +
        esc(formatFileSize(resourceNodeSize(child))) +
        "</td><td>" +
        esc(formatTimestampDetail(child.mtime)) +
        "</td><td>" +
        esc(String(resourceNodeDirectTotalCount(child) || 0)) +
        "</td><td>" +
        esc(String(resourceNodeTotalChildCount(child) || 0)) +
        '</td><td title="' +
        esc(child.path || child.error || "") +
        '">' +
        esc(child.error || child.path || resourceFullTreePath(rootNode, child)) +
        "</td></tr>";
    }
    for (var fi = 0; fi < files.length; fi++) {
      var file = files[fi] || {};
      rows +=
        '<tr class="link-index-file-row is-file resource-tree-file-row">' +
        '<td><span class="link-index-file-name">' +
        '<span class="link-index-file-icon is-file" aria-hidden="true"></span><span title="' +
        esc(file.path || "") +
        '">' +
        esc(file.name || "") +
        "</span></span></td>" +
        "<td>文件</td><td>" +
        esc(formatFileSize(file.size)) +
        '</td><td title="' +
        esc(formatTimestampDetail(file.mtime)) +
        '">' +
        esc(formatTimestampDetail(file.mtime)) +
        "</td><td></td><td></td>" +
        '<td title="' +
        esc(file.path || "") +
        '">' +
        esc(file.path || "") +
        "</td></tr>";
    }
    if (!rows) {
      rows = '<tr><td colspan="7" class="link-index-table-empty">暂无资源子项。</td></tr>';
    }
    return rows;
  }

  function renderResourceTree(scan) {
    if (resourceScanLoading) return '<p class="link-index-empty">扫描中...</p>';
    if (!scan || !scan.ok) return '<p class="link-index-empty">暂无缓存，请先扫描资源库。</p>';
    var roots = Array.isArray(scan.roots) ? scan.roots : [];
    var rootWarnings = roots
      .filter(function (root) {
        return root && root.error;
      })
      .map(function (root) {
        return '<p class="link-index-empty is-error">' + esc((root.root || "") + "：" + root.error) + "</p>";
      })
      .join("");
    var rawTree = resourceTreeRoot(scan);
    var rawChildren = folderChildren(rawTree);
    if (!rawChildren.length) return rootWarnings + '<p class="link-index-empty">暂无资源库目录缓存。</p>';
    var tree = resourceDisplayTree(scan);
    var selected = selectedResourceFolder(scan, tree);
    var stats = resourceChildStats(selected);
    var children = folderChildren(tree);
    return (
      rootWarnings +
      '<section class="link-index-browser resource-tree-browser" aria-label="资源库目录树">' +
      '<div class="link-index-browser-content" style="--link-tree-width:' +
      esc(String(Math.max(220, Math.min(720, resourceTreeWidth || 360)))) +
      'px">' +
      '<aside class="link-index-tree" aria-label="资源库目录树">' +
      renderResourceTreeToolbar(scan) +
      '<div class="link-index-tree-root resource-tree-root">' +
      renderResourceTreeHeader() +
      renderResourceTreeRootChildren(tree) +
      "</div></aside>" +
      '<div class="link-index-tree-resizer" data-resource-tree-resizer title="拖动调整资源库目录宽度"></div>' +
      '<section class="link-index-list-container" aria-label="当前资源库目录">' +
      '<div class="link-index-list-header">' +
      '<span class="link-index-list-location">' +
      esc(resourceFullTreePath(tree, selected)) +
      "</span>" +
      '<span class="link-index-list-note">' +
      esc(
        stats.total +
          " 子文件 / " +
          stats.all +
          " 全部 / " +
          formatFileSize(stats.size) +
          ((selected && selected.path) ? " · " + selected.path : "")
      ) +
      "</span></div>" +
      '<div class="link-index-list-files">' +
      '<table class="link-index-files-table"><thead><tr>' +
      "<th>名称</th><th>类型</th><th>大小</th><th>修改时间</th><th>子文件</th><th>全部</th><th>实际路径</th>" +
      "</tr></thead><tbody>" +
      renderResourceFolderRows(tree, selected) +
      "</tbody></table></div>" +
      "</section></div></section>"
    );
  }

  function resourceRootDisplayName(root, idx) {
    var raw = String(root || "").trim();
    if (!raw) return "资源库 " + (idx + 1);
    var normalized = raw.replace(/\\/g, "/").replace(/\/+$/g, "");
    if (/^[A-Za-z]:$/.test(normalized)) return raw;
    var slash = normalized.lastIndexOf("/");
    return slash >= 0 ? normalized.slice(slash + 1) || raw : raw;
  }

  function renderResourceRootRows() {
    var roots = resourceRootDrafts || [""];
    var excludes = resourceExcludeDrafts || [];
    var rows = roots
      .map(function (root, idx) {
        return (
          '<div class="resource-root-row">' +
          '<span class="resource-root-name" title="' +
          esc(root) +
          '">' +
          esc(resourceRootDisplayName(root, idx)) +
          "</span>" +
          '<input type="text" data-resource-root-index="' +
          esc(String(idx)) +
          '" value="' +
          esc(root) +
          '" placeholder="资源库根目录" />' +
          '<input type="text" data-resource-exclude-index="' +
          esc(String(idx)) +
          '" value="' +
          esc(excludes[idx] || "") +
          '" placeholder="$recycle, System Volume Information" />' +
          '<button type="button" class="btn secondary sm" data-resource-action="remove-root" data-resource-root-index="' +
          esc(String(idx)) +
          '"' +
          (roots.length <= 1 ? " disabled" : "") +
          ">删除</button>" +
          "</div>"
        );
      })
      .join("");
    return roots
      ? '<div class="resource-root-header"><span>名称</span><span>路径</span><span>排除目录</span><span></span></div>' +
          rows
      : rows;
  }

  function renderResourceLibraryPanel(data) {
    ensureResourceRootDrafts(data);
    return (
      '<div class="link-index-head resource-library-head">' +
      "<div><h2>资源库目录</h2>" +
      '<p class="link-index-summary">' +
      esc(resourceScanSummaryText(resourceScanState)) +
      "</p></div>" +
      '<div class="link-index-actions">' +
      '<button type="button" class="btn secondary sm" data-resource-action="toggle-root-config">' +
      (resourceRootConfigCollapsed ? "展开配置" : "收起配置") +
      "</button>" +
      '<button type="button" class="btn secondary sm" data-resource-action="add-root">新增目录</button>' +
      '<button type="button" class="btn secondary sm" data-resource-action="save-roots"' +
      (resourceConfigSaving ? " disabled" : "") +
      ">保存目录</button>" +
      '<button type="button" class="btn sm" data-resource-action="scan"' +
      (resourceScanLoading ? " disabled" : "") +
      ">扫描资源库</button>" +
      "</div></div>" +
      (resourceRootConfigCollapsed
        ? ""
        : '<div class="resource-root-list">' + renderResourceRootRows() + "</div>") +
      renderResourceTree(resourceScanState)
    );
  }

  function displayLinkName(name) {
    return String(name || "").replace(/\.lnk$/i, "");
  }

  function pathFileName(path) {
    var s = String(path || "").replace(/\\/g, "/");
    if (!s) return "";
    var idx = s.lastIndexOf("/");
    return idx >= 0 ? s.slice(idx + 1) : s;
  }

  function actualLinkName(node) {
    return displayLinkName(pathFileName((node && node.matched_shortcut_relpath) || "") || (node && node.name));
  }

  function pressGroupLabel(node) {
    var group = String((node && node.press_group) || "").trim();
    return group && group !== "----" ? group : "";
  }

  function displayLinkPath(node) {
    if (!node) return "";
    var target = node.shortcut_target_path || node.target_path || "";
    if (target) return String(target);
    if (node.shortcut_exists) return node.target_error ? "目标解析失败" : "目标未解析";
    return "";
  }

  function nodeRelpath(node) {
    return String((node && node.relpath) || "");
  }

  function nodeDisplayPath(node) {
    return String((node && node._display_path) || nodeRelpath(node));
  }

  function parentRelpath(relpath) {
    relpath = String(relpath || "");
    if (!relpath) return "";
    var idx = relpath.lastIndexOf("/");
    return idx >= 0 ? relpath.slice(0, idx) : "";
  }

  function visibleParentRelpath(relpath) {
    relpath = String(relpath || "");
    if (!relpath) return "";
    return parentRelpath(relpath);
  }

  function findTreeNodeByRelpath(node, relpath) {
    if (!node || typeof node !== "object") return null;
    if (nodeRelpath(node) === String(relpath || "")) return node;
    var children = Array.isArray(node.children) ? node.children : [];
    for (var i = 0; i < children.length; i++) {
      var found = findTreeNodeByRelpath(children[i], relpath);
      if (found) return found;
    }
    return null;
  }

  function selectedFolder(data) {
    var tree = displayTree(data);
    var node = findTreeNodeByRelpath(tree, linkIndexSelectedPath);
    if (!node || node.type === "link") {
      linkIndexSelectedPath = "";
      node = tree;
    }
    return node || {};
  }

  function statusLabel(status) {
    var labels = {
      ready: "已配置",
      missing_target: "目标丢失",
      duplicate_shortcut: "快捷方式冲突",
      invalid_path: "路径非法",
      failed: "生成失败",
      shortcut_exists: "已存在",
      unmapped_on_disk: "磁盘未关联",
    };
    return labels[status] || status || "";
  }

  function childStats(node) {
    var children = Array.isArray(node && node.children) ? node.children : [];
    var folders = 0;
    var links = 0;
    for (var i = 0; i < children.length; i++) {
      if (children[i] && children[i].type === "link") links++;
      else folders++;
    }
    return { folders: folders, links: links, total: children.length };
  }

  function fullTreePath(rootNode, node) {
    var rel = nodeDisplayPath(node);
    return rel || "索引目录";
  }

  function linkHasExistingTarget(node) {
    return !!(node && (node.target_exists === true || node.shortcut_target_exists === true));
  }

  function linkHasEmptyTargetPath(node) {
    return !!(node && (node.type || "folder") === "link" && !String(node.target_path || "").trim());
  }

  function emptyPathGroupPrefix(kind) {
    return kind === "empty" ? "__nimda_empty_path__" : "__nimda_non_empty_path__";
  }

  function emptyPathGroupName(kind) {
    return kind === "empty" ? "空路径" : "非空路径";
  }

  function cloneTreeForEmptyPathGroup(node, kind, prefix, displayPrefix) {
    if (!node || typeof node !== "object") return null;
    var isEmpty = kind === "empty";
    var type = node.type || "folder";
    var originalRel = nodeRelpath(node);
    var rel = originalRel ? prefix + "/" + originalRel : prefix;
    var displayPath = originalRel ? displayPrefix + "/" + originalRel : displayPrefix;
    if (type === "link") {
      if (linkHasEmptyTargetPath(node) !== isEmpty) return null;
      var linkCopy = Object.assign({}, node, {
        relpath: rel,
        _display_path: displayPath,
      });
      if (!linkCopy.shortcut_relpath) linkCopy.shortcut_relpath = originalRel;
      return linkCopy;
    }
    var children = Array.isArray(node.children) ? node.children : [];
    var filteredChildren = [];
    for (var i = 0; i < children.length; i++) {
      var child = cloneTreeForEmptyPathGroup(children[i], kind, prefix, displayPrefix);
      if (child) filteredChildren.push(child);
    }
    if (!filteredChildren.length) return null;
    return Object.assign({}, node, {
      relpath: rel,
      _display_path: displayPath,
      children: filteredChildren,
    });
  }

  function emptyPathTopGroupNode(rootNode, kind) {
    var name = emptyPathGroupName(kind);
    var prefix = emptyPathGroupPrefix(kind);
    var children = [];
    var rawChildren = Array.isArray(rootNode && rootNode.children) ? rootNode.children : [];
    for (var i = 0; i < rawChildren.length; i++) {
      var child = cloneTreeForEmptyPathGroup(rawChildren[i], kind, prefix, name);
      if (child) children.push(child);
    }
    return {
      type: "folder",
      name: name,
      relpath: prefix,
      path: "",
      _display_path: name,
      children: children,
      empty_path_group: kind,
    };
  }

  function groupTreeByEmptyTargetPath(node) {
    if (!node || typeof node !== "object") return null;
    return Object.assign({}, node, {
      children: [emptyPathTopGroupNode(node, "non_empty"), emptyPathTopGroupNode(node, "empty")],
    });
  }

  function annotateLinkFolderSummaries(node) {
    if (!node || typeof node !== "object") return { total: 0, emptyPath: 0 };
    if ((node.type || "folder") === "link") {
      var hasEmptyPath = linkHasEmptyTargetPath(node);
      var linkTotals = { total: 1, emptyPath: hasEmptyPath ? 1 : 0 };
      node._summary_counts = linkTotals;
      return linkTotals;
    }
    var totals = { total: 0, emptyPath: 0 };
    var children = Array.isArray(node.children) ? node.children : [];
    for (var i = 0; i < children.length; i++) {
      var childStats = annotateLinkFolderSummaries(children[i]);
      totals.total += childStats.total;
      totals.emptyPath += childStats.emptyPath;
    }
    node._summary_counts = totals;
    node._summary = "总数" + totals.total;
    return totals;
  }

  function nodeSummaryStats(node) {
    if (!node || typeof node !== "object") return { total: 0, emptyPath: 0 };
    var counts = node._summary_counts || null;
    if (counts && typeof counts === "object") {
      return {
        total: Number(counts.total) || 0,
        emptyPath: Number(counts.emptyPath) || 0,
      };
    }
    if ((node.type || "folder") === "link") {
      return {
        total: 1,
        emptyPath: linkHasEmptyTargetPath(node) ? 1 : 0,
      };
    }
    return { total: 0, emptyPath: 0 };
  }

  function renderTreeStats(node) {
    var stats = nodeSummaryStats(node);
    return (
      '<span class="link-tree-stat">' +
      esc(String(stats.total)) +
      '</span><span class="link-tree-stat">' +
      esc(String(stats.emptyPath)) +
      "</span>"
    );
  }

  function renderTableStatsCells(node) {
    var stats = nodeSummaryStats(node);
    return (
      '<td class="link-index-stat-cell">' +
      esc(String(stats.total)) +
      '</td><td class="link-index-stat-cell">' +
      esc(String(stats.emptyPath)) +
      "</td>"
    );
  }

  function displayTree(data) {
    var tree = (data && data.tree) || {};
    if (linkIndexGroupByEmptyPath) {
      tree = groupTreeByEmptyTargetPath(tree) || {
        type: "root",
        name: "索引目录",
        relpath: "",
        children: [],
      };
    }
    annotateLinkFolderSummaries(tree);
    return tree;
  }

  function renderTreeRootChildren(tree) {
    var children = folderChildren(tree);
    if (!children.length) return '<p class="link-index-empty">暂无索引子项。</p>';
    return children
      .map(function (child) {
        return renderTreeNode(child, 0);
      })
      .join("");
  }

  function renderTreeHeader() {
    return (
      '<div class="link-tree-header" aria-hidden="true">' +
      "<span></span><span></span><span>名称</span><span>总数</span><span>空路径</span>" +
      "</div>"
    );
  }

  function renderTreeToolbar() {
    return (
      '<div class="link-index-tree-toolbar">' +
      '<span class="link-index-tree-title">目录结构</span>' +
      '<div class="link-index-tree-actions">' +
      '<label class="link-index-filter-check"><input type="checkbox" data-link-index-classify-empty' +
      (linkIndexGroupByEmptyPath ? " checked" : "") +
      " />按空路径分类</label>" +
      '<button type="button" class="link-index-browser-tool-btn" data-link-index-browser-action="expand-all">全部展开</button>' +
      '<button type="button" class="link-index-browser-tool-btn" data-link-index-browser-action="collapse-all">全部折叠</button>' +
      "</div></div>"
    );
  }

  function linkOpenAttrs(node) {
    var shortcutPath = (node && (node.matched_shortcut_path || node.shortcut_path || node.path || node.open_path)) || "";
    var targetPath = (node && (node.shortcut_target_path || node.target_path)) || "";
    if (node && node.source === "index_db") shortcutPath = "";
    var hasShortcut = !!(node && shortcutPath);
    var openPath = hasShortcut ? shortcutPath : (node && (node.open_path || node.target_path || shortcutPath || node.path)) || "";
    var canOpen = !!(node && openPath && (hasShortcut || linkHasExistingTarget(node)));
    return (
      ' data-link-index-open="' +
      esc(openPath) +
      '" data-link-shortcut-path="' +
      esc(shortcutPath) +
      '" data-link-target-path="' +
      esc(targetPath) +
      '" data-target-exists="' +
      esc(node && node.target_exists === true ? "1" : "0") +
      '" data-can-open="' +
      esc(canOpen ? "1" : "0") +
      '" data-link-target-error="' +
      esc((node && node.target_error) || "") +
      '"'
    );
  }

  function linkTargetFix(node) {
    var fix = node && node.target_fix && typeof node.target_fix === "object" ? node.target_fix : null;
    return fix && fix.target_path ? fix : null;
  }

  function renderLinkTargetCell(node, pathTitle, pathText) {
    var fix = linkTargetFix(node);
    var html =
      '<td title="' +
      esc(pathTitle || "") +
      '">' +
      esc(pathText || "");
    if (fix) {
      html +=
        '<div class="link-index-target-fix">' +
        '<span class="link-index-target-fix-label">修正目录</span>' +
        '<span class="link-index-target-fix-path" title="' +
        esc(fix.target_path || "") +
        '">' +
        esc(fix.target_path || "") +
        "</span>" +
        '<button type="button" class="btn secondary sm" data-link-index-fix-target="' +
        esc(fix.target_path || "") +
        '" data-link-index-fix-source="' +
        esc(node.source || "") +
        '" data-link-index-fix-entry-key="' +
        esc(node.entry_key || "") +
        '" data-link-index-fix-shortcut="' +
        esc(node.matched_shortcut_path || node.shortcut_path || node.path || "") +
        '" data-link-index-fix-relpath="' +
        esc(node.shortcut_relpath || node.relpath || "") +
        '" data-link-index-fix-current-target="' +
        esc(node.shortcut_target_path || node.target_path || "") +
        '">' +
        esc(node.source === "index_db" ? "修复索引" : "修复lnk") +
        "</button>" +
        "</div>";
    }
    return html + "</td>";
  }

  function renderTreeNode(node, depth) {
    if (!node || typeof node !== "object") return "";
    var type = node.type || "folder";
    var children = folderChildren(node);
    if (type === "link") {
      return "";
    }
    var relpath = nodeRelpath(node);
    var selectedClass = relpath === linkIndexSelectedPath ? " is-selected" : "";
    var hasChildren = children.length > 0;
    var collapsed = hasChildren && !!linkIndexCollapsedPaths[relpath];
    return (
      '<div class="link-tree-folder-wrap">' +
      '<div class="link-tree-folder-row dynatree-node dynatree-folder ' +
      (hasChildren ? (collapsed ? "dynatree-exp-c dynatree-ico-cf" : "dynatree-exp-e dynatree-ico-ef") : "dynatree-exp-n dynatree-ico-cf") +
      selectedClass +
      '" style="--tree-depth:' +
      esc(String(depth || 0)) +
      '" data-link-tree-select="' +
      esc(relpath) +
      '">' +
      (hasChildren
        ?
      '<button type="button" class="link-tree-toggle ' +
      (collapsed ? "is-collapsed" : "is-expanded") +
          ' dynatree-expander' +
      '" data-link-tree-toggle aria-label="展开或折叠" aria-expanded="' +
      (collapsed ? "false" : "true") +
      '">' +
      (collapsed ? "+" : "-") +
          "</button>"
        : '<span class="link-tree-toggle-spacer dynatree-connector" aria-hidden="true"></span>') +
      '<span class="link-tree-file-icon dynatree-icon is-folder' +
      (collapsed || !hasChildren ? "" : " is-open") +
      '" aria-hidden="true"></span>' +
      '<span class="link-tree-name" title="' +
      esc(node.path || "") +
      '">' +
      esc(node.name || "") +
      "</span>" +
      renderTreeStats(node) +
      "</div>" +
      (hasChildren
        ? '<div class="link-tree-children"' +
          (collapsed ? " hidden" : "") +
          ">" +
          children
            .map(function (child) {
              return renderTreeNode(child, (depth || 0) + 1);
            })
            .join("") +
          "</div>"
        : "") +
      "</div>"
    );
  }

  function renderFolderRows(rootNode, node) {
    var rows = "";
    var children = Array.isArray(node && node.children) ? node.children : [];
    var relpath = nodeRelpath(node);
    if (relpath) {
      var parentPath = visibleParentRelpath(relpath);
      rows +=
        '<tr class="link-index-file-row is-parent" data-link-tree-select="' +
        esc(parentPath) +
        '">' +
        '<td><button type="button" class="link-index-file-name" data-link-tree-select="' +
        esc(parentPath) +
        '">' +
        '<span class="link-index-file-icon is-folder" aria-hidden="true"></span><span>..</span></button></td>' +
        "<td></td><td>上级目录</td><td></td><td></td><td></td></tr>";
    }
    for (var i = 0; i < children.length; i++) {
      var child = children[i] || {};
      if (child.type === "link") {
        var pathTitle = child.shortcut_target_path || child.target_path || child.target_error || "";
        var pathText = displayLinkPath(child);
        rows +=
          '<tr class="link-index-file-row is-link is-' +
          esc(child.status || "unknown") +
          '">' +
          '<td><button type="button" class="link-index-file-name"' +
          linkOpenAttrs(child) +
          ">" +
          '<span class="link-index-file-icon is-link" aria-hidden="true"></span><span title="' +
          esc(child.matched_shortcut_relpath || child.target_path || child.shortcut_path || child.path || "") +
          '">' +
          esc(actualLinkName(child)) +
          "</span></button></td>" +
          "<td title=\"" +
          esc(child.press_group || "") +
          "\">" +
          esc(pressGroupLabel(child)) +
          "</td>" +
          "<td>" +
          esc(statusLabel(child.status || "")) +
          "</td>" +
          renderTableStatsCells(child) +
          renderLinkTargetCell(child, pathTitle, pathText) +
          "</tr>";
      } else {
        rows +=
          '<tr class="link-index-file-row is-folder" data-link-tree-select="' +
          esc(nodeRelpath(child)) +
          '">' +
          '<td><button type="button" class="link-index-file-name" data-link-tree-select="' +
          esc(nodeRelpath(child)) +
          '">' +
          '<span class="link-index-file-icon is-folder" aria-hidden="true"></span><span title="' +
          esc(child.path || "") +
          '">' +
          esc(child.name || "") +
          "</span></button></td>" +
          "<td></td>" +
          "<td>文件夹</td>" +
          renderTableStatsCells(child) +
          '<td title="' +
          esc(fullTreePath(rootNode, child)) +
          '">' +
          esc(child.path || fullTreePath(rootNode, child)) +
          "</td></tr>";
      }
    }
    if (!rows) {
      rows = '<tr><td colspan="6" class="link-index-table-empty">暂无索引项。</td></tr>';
    }
    return rows;
  }

  function renderIndexBrowser(data) {
    var tree = displayTree(data);
    var selected = selectedFolder(data);
    var stats = childStats(selected);
    return (
      '<section class="link-index-browser" aria-label="索引目录浏览器">' +
      '<div class="link-index-browser-content" style="--link-tree-width:' +
      esc(String(Math.max(220, Math.min(720, linkIndexTreeWidth || 360)))) +
      'px">' +
      '<aside class="link-index-tree" aria-label="目录树">' +
      renderTreeToolbar() +
      '<div class="link-index-tree-root">' +
      renderTreeHeader() +
      renderTreeRootChildren(tree) +
      "</div></aside>" +
      '<div class="link-index-tree-resizer" data-link-index-resizer title="拖动调整目录宽度"></div>' +
      '<section class="link-index-list-container" aria-label="当前目录">' +
      '<div class="link-index-list-header">' +
      '<span class="link-index-list-location">' +
      esc(fullTreePath(tree, selected)) +
      "</span>" +
      '<span class="link-index-list-note">' +
      esc(stats.total + " 项 / " + stats.folders + " 文件夹 / " + stats.links + " 链接" + ((selected && selected.path) ? " · " + selected.path : "")) +
      "</span></div>" +
      '<div class="link-index-list-files">' +
      '<table class="link-index-files-table"><thead><tr>' +
      "<th>名称</th><th>压制组</th><th>状态</th><th>总数</th><th>空路径</th><th>目标 / 索引路径</th>" +
      "</tr></thead><tbody>" +
      renderFolderRows(tree, selected) +
      "</tbody></table></div>" +
      "</section></div></section>"
    );
  }

  function renderLinkIndexOperationNotice() {
    if (!linkIndexOperationNotice || !linkIndexOperationNotice.message) return "";
    return (
      '<div class="link-index-operation-notice' +
      (linkIndexOperationNotice.is_error ? " is-error" : "") +
      '">' +
      esc(linkIndexOperationNotice.message) +
      "</div>"
    );
  }

  function renderWarnings(data) {
    if (!data || !data.ok) return "";
    var summary = data.plan_summary || {};
    var items = [];
    if (summary.unmapped_on_disk) {
      items.push("实际索引目录中有 " + summary.unmapped_on_disk + " 个 .lnk 未在 DB 中找到关联。");
    }
    if (!items.length) return "";
    return (
      '<div class="link-index-warnings">' +
      items
        .map(function (item) {
          return "<p>" + esc(item) + "</p>";
        })
        .join("") +
      "</div>"
    );
  }

  function renderLinkIndexPanel() {
    var el = slot();
    if (el) {
      var data = linkIndexState || {};
      var loaded = !!data.ok;
      el.innerHTML =
        '<section class="link-index-panel">' +
        '<div class="link-index-head">' +
        "<div><h2>索引目录</h2>" +
        '<p class="link-index-summary">' +
        esc(linkIndexLoading ? "读取中..." : loaded ? summaryText(data) : "未读取") +
        "</p></div>" +
        '<div class="link-index-actions">' +
        '<button type="button" class="btn secondary sm" data-link-index-action="reload">重读</button>' +
        '<button type="button" class="btn secondary sm" data-link-index-action="validate"' +
        (loaded ? "" : " disabled") +
        ">重新校验索引</button>" +
        '<button type="button" class="btn sm" data-link-index-action="generate"' +
        (loaded ? "" : " disabled") +
        ">重新生成索引</button>" +
        '<button type="button" class="btn sm" data-link-index-action="generate-files"' +
        (loaded ? "" : " disabled") +
        ">生成目录文件</button>" +
        "</div></div>" +
        renderLinkIndexOperationNotice() +
        (loaded ? renderWarnings(data) + renderIndexBrowser(data) : "") +
        "</section>";
    }
    renderResourcePanel();
  }

  function renderResourcePanel() {
    var el = resourceSlot();
    if (!el) return;
    var data = resourceScanState || linkIndexState || {};
    el.innerHTML =
      '<section class="link-index-panel resource-library-page">' +
      renderResourceLibraryPanel(data) +
      "</section>";
  }

  async function loadLinkIndex(opts) {
    opts = opts || {};
    var serial = ++linkIndexRequestSerial;
    var refreshLinks = !!opts.refreshLinks;
    linkIndexLoading = true;
    setLinkIndexNotice("", false);
    renderLinkIndexPanel();
    setStatus(refreshLinks ? "链接重新确认中..." : "索引目录读取中...", false);
    var params = new URLSearchParams();
    params.set("lite", "1");
    if (refreshLinks) params.set("refresh_links", "1");
    var out;
    try {
      out = await fetchJson("/api/collection-detail/link-index?" + params.toString(), {
        method: "GET",
      });
    } catch (error) {
      if (serial !== linkIndexRequestSerial || !linkIndexPanelActive()) return;
      throw error;
    } finally {
      if (serial === linkIndexRequestSerial) {
        linkIndexLoading = false;
        renderLinkIndexPanel();
      }
    }
    if (serial !== linkIndexRequestSerial || !linkIndexPanelActive()) return;
    if (!out.res.ok || !out.data || !out.data.ok) {
      renderLinkIndexPanel();
      setStatus((out.data && out.data.error) || "索引目录读取失败。", true);
      return;
    }
    linkIndexState = out.data;
    if (resourceRootDrafts === null) ensureResourceRootDrafts(linkIndexState);
    renderLinkIndexPanel();
    setStatus(refreshLinks ? "链接确认已更新。" : "", false);
  }

  async function loadResourceScanCache() {
    var serial = beginResourceRead();
    renderLinkIndexPanel();
    try {
      var out = await fetchJson("/api/collection-detail/resource-libraries/cache", { method: "GET" });
      if (!resourceRequestCurrent(serial)) return;
      if (!out.res.ok || !out.data || !out.data.ok) {
        throw new Error((out.data && out.data.error) || "资源库缓存读取失败。");
      }
      resourceScanState = out.data;
      resourceSearchState = null;
      resourceSearchLoading = false;
      resourceNodeLoadingPaths = {};
      syncResourceRootDraftsFromConfig(resourceScanState);
    } catch (error) {
      if (resourceRequestCurrent(serial)) throw error;
    } finally {
      if (serial === resourceRequestSerial) {
        resourceScanLoading = false;
        renderLinkIndexPanel();
      }
    }
  }

  function mergeResourceTreeNode(target, source) {
    if (!target || !source) return source || target;
    Object.keys(target).forEach(function (key) {
      delete target[key];
    });
    Object.keys(source).forEach(function (key) {
      target[key] = source[key];
    });
    return target;
  }

  function applyResourceTreeNode(node) {
    if (!resourceScanState || !node) return node;
    var relpath = resourceTreeRelpath(node);
    if (!relpath) {
      resourceScanState.tree = node;
      return node;
    }
    var existing = findTreeNodeByRelpath(resourceTreeRoot(resourceScanState), relpath);
    if (existing) return mergeResourceTreeNode(existing, node);
    return node;
  }

  async function loadResourceTreeNode(relpath) {
    relpath = String(relpath || "");
    var serial = resourceRequestSerial;
    var existing = findTreeNodeByRelpath(resourceTreeRoot(resourceScanState || {}), relpath);
    if (existing && resourceNodeLoaded(existing)) return existing;
    if (resourceNodeLoadingPaths[relpath]) return existing || null;
    resourceNodeLoadingPaths[relpath] = true;
    renderLinkIndexPanel();
    try {
      var params = new URLSearchParams();
      params.set("relpath", relpath);
      var out = await fetchJson("/api/collection-detail/resource-libraries/node?" + params.toString(), { method: "GET" });
      if (!resourceRequestCurrent(serial)) return null;
      if (!out.res.ok || !out.data || !out.data.ok || !out.data.node) {
        throw new Error((out.data && out.data.error) || "资源库目录读取失败。");
      }
      return applyResourceTreeNode(out.data.node);
    } catch (error) {
      if (resourceRequestCurrent(serial)) throw error;
      return null;
    } finally {
      if (serial === resourceRequestSerial) delete resourceNodeLoadingPaths[relpath];
    }
  }

  async function searchResourceTree() {
    var query = String(resourceTreeSearchDraft || "").trim();
    var serial = ++resourceSearchSerial;
    var generation = resourceRequestSerial;
    function current() {
      return serial === resourceSearchSerial && resourceRequestCurrent(generation);
    }
    resourceTreeSearchKeyword = query;
    resourceSearchCollapsedPaths = {};
    resourceSelectedPath = "";
    if (!query) {
      resourceSearchState = null;
      resourceSearchLoading = false;
      renderLinkIndexPanel();
      return;
    }
    resourceSearchLoading = true;
    resourceSearchState = null;
    renderLinkIndexPanel();
    setStatus("资源库目录搜索中...", false);
    try {
      var params = new URLSearchParams();
      params.set("q", query);
      var out = await fetchJson("/api/collection-detail/resource-libraries/search?" + params.toString(), {
        method: "GET",
      });
      if (!current()) return;
      if (!out.res.ok || !out.data || !out.data.ok) {
        throw new Error((out.data && out.data.error) || "资源库目录搜索失败。");
      }
      resourceSearchState = out.data;
      setStatus("", false);
    } catch (error) {
      if (current()) throw error;
    } finally {
      if (current()) {
        resourceSearchLoading = false;
        renderLinkIndexPanel();
      }
    }
  }

  function cleanResourceRootEntries() {
    var roots = resourceRootDrafts || [];
    var excludes = resourceExcludeDrafts || [];
    var out = [];
    var seen = {};
    roots.forEach(function (root, idx) {
      var value = String(root || "").trim();
      if (!value) return;
      var key = value.toLowerCase();
      if (seen[key]) return;
      seen[key] = true;
      out.push({
        path: value,
        excludes: String(excludes[idx] || "")
          .split(/[,\n;，；]/)
          .map(function (x) {
            return x.trim();
          })
          .filter(Boolean),
      });
    });
    return out;
  }

  async function saveResourceRoots() {
    var roots = cleanResourceRootEntries();
    resourceConfigSaving = true;
    renderLinkIndexPanel();
    setStatus("资源库目录保存中...", false);
    try {
      var out = await fetchJson("/api/collection-detail/resource-libraries/config", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ roots: roots }),
      });
      if (!out.res.ok || !out.data || !out.data.ok) {
        throw new Error((out.data && out.data.error) || "资源库目录保存失败。");
      }
      resourceRootDrafts = configuredResourceRoots({ config: out.data.config });
      resourceExcludeDrafts = resourceRootDrafts.map(function (root) {
        return resourceExcludesForRoot({ config: out.data.config }, root).join(", ");
      });
      if (linkIndexState) linkIndexState.config = out.data.config || linkIndexState.config;
      releaseResourcePayload();
      setStatus("资源库目录已保存。", false);
    } finally {
      resourceConfigSaving = false;
      renderLinkIndexPanel();
    }
  }

  async function scanResourceLibraries() {
    var serial = beginResourceRead();
    renderLinkIndexPanel();
    setStatus("资源库扫描中...", false);
    try {
      var out = await fetchJson("/api/collection-detail/resource-libraries/scan", { method: "GET" });
      if (!resourceRequestCurrent(serial)) return;
      if (!out.res.ok || !out.data || !out.data.ok) {
        throw new Error((out.data && out.data.error) || "资源库扫描失败。");
      }
      resourceScanState = out.data;
      resourceSearchState = null;
      resourceSearchLoading = false;
      resourceNodeLoadingPaths = {};
      resourceTreeCollapsedPaths = {};
      if (linkIndexState && out.data.config) linkIndexState.config = out.data.config;
      setStatus("", false);
    } catch (error) {
      if (resourceRequestCurrent(serial)) throw error;
    } finally {
      if (serial === resourceRequestSerial) {
        resourceScanLoading = false;
        renderLinkIndexPanel();
      }
    }
  }

  async function generateLinkIndex() {
    var summary = linkIndexState ? summaryText(linkIndexState) : "";
    if (!window.confirm("将根据当前作品 DB 重新生成索引 DB。\n" + summary)) return;
    setLinkIndexNotice("索引 DB 生成中，请稍等...", false);
    renderLinkIndexPanel();
    setStatus("索引 DB 生成中...", false);
    var out = await fetchJson("/api/collection-detail/link-index/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({}),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      var genErr = (out.data && out.data.error) || "索引 DB 生成失败。";
      setLinkIndexNotice(genErr, true);
      renderLinkIndexPanel();
      setStatus(genErr, true);
      return;
    }
    if (!linkIndexPanelActive()) return;
    linkIndexState = Object.assign({}, linkIndexState || {}, out.data);
    var generatedCount = (out.data.index_db && Number(out.data.index_db.item_count || 0)) || Number((out.data.plan_summary || {}).total || 0);
    var emptyCount = Number((out.data.plan_summary || {}).empty_target_path || 0);
    var generatedMsg = "索引 DB 已重新生成：共 " + generatedCount + " 项，空路径 " + emptyCount + " 项。";
    setLinkIndexNotice(generatedMsg, false);
    renderLinkIndexPanel();
    setStatus(generatedMsg, false);
  }

  async function generateLinkIndexFiles() {
    setLinkIndexNotice("索引输出目录预检中...", false);
    renderLinkIndexPanel();
    setStatus("索引输出目录预检中...", false);
    var previewOut = await fetchJson("/api/collection-detail/link-index/generate-files", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ preview: true }),
    });
    if (!previewOut.res.ok || !previewOut.data || !previewOut.data.ok) {
      var previewErr = (previewOut.data && previewOut.data.error) || "索引输出目录预检失败。";
      setLinkIndexNotice(previewErr, true);
      renderLinkIndexPanel();
      setStatus(previewErr, true);
      return;
    }
    var info = previewOut.data.file_generation || {};
    var outputRoot = info.output_root || "";
    var body = {};
    if (info.root_non_empty) {
      var sample = Array.isArray(info.existing_sample) && info.existing_sample.length
        ? "\n\n现有项目示例：\n" + info.existing_sample.slice(0, 8).join("\n")
        : "";
      if (
        !window.confirm(
          "索引输出目录不是空目录，继续生成会先清空该目录。\n\n目录：" +
            outputRoot +
            "\n现有直接子项：" +
            (info.existing_count || 0) +
            sample +
            "\n\n第一次确认：是否继续？"
        )
      ) {
        setLinkIndexNotice("已取消生成目录文件。", false);
        renderLinkIndexPanel();
        setStatus("已取消生成目录文件。", false);
        return;
      }
      if (
        !window.confirm(
          "第二次确认：将清空索引输出目录后重新生成目录文件。\n\n目录：" +
            outputRoot +
            "\n\n确认清空并继续生成？"
        )
      ) {
        setLinkIndexNotice("已取消生成目录文件。", false);
        renderLinkIndexPanel();
        setStatus("已取消生成目录文件。", false);
        return;
      }
      body.confirm_clear = true;
      body.confirm_clear_twice = true;
      if (window.confirm("是否在清空前备份当前索引输出目录？")) {
        var backupDir = window.prompt("请输入备份保存目录。系统会在其中创建带时间戳的子目录；留空则不备份。", "");
        if (backupDir === null) {
          setLinkIndexNotice("已取消生成目录文件。", false);
          renderLinkIndexPanel();
          setStatus("已取消生成目录文件。", false);
          return;
        }
        backupDir = String(backupDir || "").trim();
        if (backupDir) body.backup_dir = backupDir;
      }
    }
    setLinkIndexNotice("目录文件生成中，请稍等...", false);
    renderLinkIndexPanel();
    setStatus("目录文件生成中...", false);
    var out = await fetchJson("/api/collection-detail/link-index/generate-files", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(body),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      var err = (out.data && out.data.error) || "目录文件生成失败。";
      setLinkIndexNotice(err, true);
      renderLinkIndexPanel();
      setStatus(err, true);
      return;
    }
    if (!linkIndexPanelActive()) return;
    linkIndexState = Object.assign({}, linkIndexState || {}, out.data);
    var result = out.data.file_generation || {};
    var msg =
      "目录文件生成完成：创建 " +
      (result.created || 0) +
      " 个，空路径跳过 " +
      (result.skipped_empty_target || 0) +
      " 个，目标缺失跳过 " +
      (result.skipped_missing_target || 0) +
      " 个" +
      (result.failed_count ? "，失败 " + result.failed_count + " 个" : "") +
      (result.backup_path ? "，已备份到 " + result.backup_path : "") +
      "。";
    setLinkIndexNotice(msg, !!result.failed_count);
    renderLinkIndexPanel();
    setStatus(msg, !!result.failed_count);
  }

  async function validateLinkIndex() {
    var summary = linkIndexState ? summaryText(linkIndexState) : "";
    if (!window.confirm("将更新资源库缓存并重新校验索引 DB，可能需要一些时间。\n" + summary)) return;
    setLinkIndexNotice("资源库更新与索引校验中，请稍等...", false);
    renderLinkIndexPanel();
    setStatus("资源库更新与索引校验中...", false);
    var out = await fetchJson("/api/collection-detail/link-index/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ refresh_resources: true }),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      var validateErr = (out.data && out.data.error) || "索引校验失败。";
      setLinkIndexNotice(validateErr, true);
      renderLinkIndexPanel();
      setStatus(validateErr, true);
      return;
    }
    if (!linkIndexPanelActive()) return;
    linkIndexState = Object.assign({}, linkIndexState || {}, out.data);
    if (out.data.validation && out.data.validation.resource_scan_summary) {
      resourceScanState = Object.assign({}, resourceScanState || {}, {
        ok: true,
        summary: out.data.validation.resource_scan_summary,
      });
    }
    renderLinkIndexPanel();
    var mismatch = out.data.validation ? Number(out.data.validation.mismatch_count || 0) : 0;
    var emptyCount = Number((out.data.plan_summary || {}).empty_target_path || 0);
    var validateMsg = mismatch
      ? "索引校验完成：有 " + mismatch + " 项可修复，空路径 " + emptyCount + " 项。"
      : "索引校验完成：未发现可修复项，空路径 " + emptyCount + " 项。";
    setLinkIndexNotice(validateMsg, false);
    renderLinkIndexPanel();
    setStatus(validateMsg, false);
  }

  async function postLinkTargetFixItems(items, opts) {
    opts = opts || {};
    var body = { items: items };
    if (opts.refreshPayload === false) body.refresh_payload = false;
    var out = await fetchJson("/api/collection-detail/link-index/fixes/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(body),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      throw new Error((out.data && out.data.error) || "修复索引目标失败。");
    }
    if (opts.refreshPayload !== false && linkIndexPanelActive()) {
      linkIndexState = out.data;
    }
    return out.data;
  }

  async function applyLinkTargetFix(btn) {
    var targetPath = btn.getAttribute("data-link-index-fix-target") || "";
    var source = btn.getAttribute("data-link-index-fix-source") || "";
    var entryKey = btn.getAttribute("data-link-index-fix-entry-key") || "";
    var shortcutPath = btn.getAttribute("data-link-index-fix-shortcut") || "";
    var shortcutRelpath = btn.getAttribute("data-link-index-fix-relpath") || "";
    var currentTargetPath = btn.getAttribute("data-link-index-fix-current-target") || "";
    if (!targetPath || (!shortcutPath && !shortcutRelpath)) {
      window.alert("这个修复候选缺少索引定位信息。");
      return;
    }
    if (!window.confirm("将索引目标修复为：\n" + targetPath + "\n\n继续吗？")) return;
    btn.disabled = true;
    btn.textContent = "修复中...";
    await postLinkTargetFixItems([
      {
        shortcut_path: shortcutPath,
        shortcut_relpath: shortcutRelpath,
        source: source,
        entry_key: entryKey,
        current_target_path: currentTargetPath,
        target_path: targetPath,
      },
    ]);
    renderLinkIndexPanel();
    setStatus("已修复 .lnk 指向，并刷新索引目录。", false);
  }

  async function openLinkIndexPath(btn) {
    var canOpen = btn.getAttribute("data-can-open") === "1";
    if (btn.getAttribute("data-target-exists") === "0" && !canOpen) {
      window.alert("目标目录不存在，链接可能已经丢失。");
      return;
    }
    var p = btn.getAttribute("data-link-index-open") || "";
    if (!p) {
      window.alert("没有可打开的路径。");
      return;
    }
    var out = await fetchJson("/api/collection-detail/link-index/open", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ path: p }),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      window.alert((out.data && out.data.error) || "打开失败。");
      return;
    }
    setStatus("已请求打开目录。", false);
  }

  function linkTooltipElement() {
    if (linkIndexTooltipEl && document.body.contains(linkIndexTooltipEl)) return linkIndexTooltipEl;
    linkIndexTooltipEl = document.createElement("div");
    linkIndexTooltipEl.className = "link-index-path-tooltip";
    linkIndexTooltipEl.hidden = true;
    document.body.appendChild(linkIndexTooltipEl);
    return linkIndexTooltipEl;
  }

  function linkTooltipText(btn) {
    var target = btn.getAttribute("data-link-target-path") || "";
    if (target) {
      if (btn.getAttribute("data-target-exists") === "0") return target + "（目标不存在）";
      return target;
    }
    return btn.getAttribute("data-link-target-error") || "目标路径读取中...";
  }

  function setLinkTooltipTitle(btn, text) {
    if (!btn || !text) return;
    btn.setAttribute("title", text);
    var named = btn.querySelector(".link-tree-name, span:last-child");
    if (named) named.setAttribute("title", text);
  }

  function placeLinkTooltip(ev) {
    linkIndexTooltipPosition = ev;
    var tip = linkTooltipElement();
    var pad = 14;
    var x = ev.clientX + pad;
    var y = ev.clientY + pad;
    var rect = tip.getBoundingClientRect();
    if (x + rect.width > window.innerWidth - 8) x = Math.max(8, ev.clientX - rect.width - pad);
    if (y + rect.height > window.innerHeight - 8) y = Math.max(8, ev.clientY - rect.height - pad);
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }

  async function resolveLinkTargetForTooltip(btn, ev) {
    if (!btn || btn.getAttribute("data-link-target-loading") === "1") return;
    if (btn.getAttribute("data-link-target-path")) return;
    var p = btn.getAttribute("data-link-shortcut-path") || btn.getAttribute("data-link-index-open") || "";
    if (!p) return;
    btn.setAttribute("data-link-target-loading", "1");
    try {
      var out = await fetchJson("/api/collection-detail/link-index/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ path: p }),
      });
      if (!out.res.ok || !out.data || !out.data.ok) {
        throw new Error((out.data && out.data.error) || "目标路径解析失败");
      }
      var target = out.data.target_path || out.data.open_path || "";
      btn.setAttribute("data-link-target-path", target);
      btn.setAttribute("data-target-exists", out.data.target_exists ? "1" : "0");
      btn.removeAttribute("data-link-target-error");
      setLinkTooltipTitle(btn, linkTooltipText(btn));
    } catch (e) {
      btn.setAttribute("data-link-target-error", "目标路径解析失败：" + (e.message || String(e)));
    } finally {
      btn.removeAttribute("data-link-target-loading");
      var currentSlot = slot();
      if (linkIndexTooltipTarget === btn && linkIndexPanelActive() && currentSlot && currentSlot.contains(btn)) {
        var tip = linkTooltipElement();
        tip.textContent = linkTooltipText(btn);
        tip.classList.toggle("is-error", !!btn.getAttribute("data-link-target-error"));
        if (linkIndexTooltipPosition || ev) placeLinkTooltip(linkIndexTooltipPosition || ev);
      }
    }
  }

  function showLinkTooltip(btn, ev) {
    linkIndexTooltipTarget = btn;
    var tip = linkTooltipElement();
    var text = linkTooltipText(btn);
    tip.textContent = text;
    tip.classList.toggle("is-error", !!btn.getAttribute("data-link-target-error"));
    tip.hidden = false;
    setLinkTooltipTitle(btn, text);
    placeLinkTooltip(ev);
    if (!btn.getAttribute("data-link-target-path")) {
      resolveLinkTargetForTooltip(btn, ev).catch(function () {});
    }
  }

  function hideLinkTooltip() {
    linkIndexTooltipTarget = null;
    linkIndexTooltipPosition = null;
    if (linkIndexTooltipEl) linkIndexTooltipEl.hidden = true;
  }

  function collectCollapsibleFolderPaths(node, out) {
    if (!node || typeof node !== "object" || node.type === "link") return;
    var children = folderChildren(node);
    if (children.length) out[nodeRelpath(node)] = true;
    for (var i = 0; i < children.length; i++) {
      collectCollapsibleFolderPaths(children[i], out);
    }
  }

  function collectResourceTreeFolderPaths(node, out) {
    if (!node || typeof node !== "object" || node.type === "resource" || node.type === "file") return;
    var children = folderChildren(node);
    if (children.length) out[resourceTreeRelpath(node)] = true;
    for (var i = 0; i < children.length; i++) {
      collectResourceTreeFolderPaths(children[i], out);
    }
  }

  function setAllResourceFoldersCollapsed(collapsed) {
    if (!collapsed) {
      replaceResourceCollapseState({});
      renderLinkIndexPanel();
      return;
    }
    var next = {};
    collectResourceTreeFolderPaths(resourceDisplayTree(resourceScanState || {}), next);
    replaceResourceCollapseState(next);
    renderLinkIndexPanel();
  }

  function setAllIndexFoldersCollapsed(collapsed) {
    if (!collapsed) {
      linkIndexCollapsedPaths = {};
      renderLinkIndexPanel();
      return;
    }
    var next = {};
    collectCollapsibleFolderPaths(displayTree(linkIndexState || {}), next);
    linkIndexCollapsedPaths = next;
    renderLinkIndexPanel();
  }

  function setLinkIndexTreeWidthFromClientX(clientX) {
    var el = slot();
    var browser = el ? el.querySelector(".link-index-browser-content") : null;
    if (!browser) return;
    var rect = browser.getBoundingClientRect();
    var next = Math.round(clientX - rect.left);
    next = Math.max(220, Math.min(720, next));
    linkIndexTreeWidth = next;
    writePreference("nimda.linkIndexTreeWidth", String(next));
    browser.style.setProperty("--link-tree-width", next + "px");
  }

  function setResourceTreeWidthFromClientX(clientX) {
    var el = resourceSlot() || slot();
    var browser = el ? el.querySelector(".resource-tree-browser .link-index-browser-content") : null;
    if (!browser) return;
    var rect = browser.getBoundingClientRect();
    var next = Math.round(clientX - rect.left);
    next = Math.max(220, Math.min(720, next));
    resourceTreeWidth = next;
    writePreference("nimda.resourceTreeWidth", String(next));
    browser.style.setProperty("--link-tree-width", next + "px");
  }

  function bindLinkIndexOnce() {
    if (linkIndexBound) return;
    if (!slot() && !resourceSlot()) return;
    linkIndexBound = true;
    addDocumentListener("mousedown", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var owner = panelEventRoot(t);
      if (!owner) return;
      var resizer = t.closest("[data-link-index-resizer]");
      var resourceResizer = t.closest("[data-resource-tree-resizer]");
      if (resourceResizer && owner.contains(resourceResizer)) {
        resourceTreeResizing = true;
        document.body.classList.add("is-link-index-resizing");
        setResourceTreeWidthFromClientX(ev.clientX);
        ev.preventDefault();
        return;
      }
      if (!resizer || !owner.contains(resizer) || owner !== slot()) return;
      linkIndexTreeResizing = true;
      document.body.classList.add("is-link-index-resizing");
      setLinkIndexTreeWidthFromClientX(ev.clientX);
      ev.preventDefault();
    });
    addDocumentListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var owner = panelEventRoot(t);
      if (!owner) return;
      var linkRoot = slot();
      var resourceAction = t.closest("[data-resource-action]");
      if (resourceAction && owner.contains(resourceAction)) {
        var resourceActionName = resourceAction.getAttribute("data-resource-action");
        if (resourceActionName === "toggle-root-config") {
          resourceRootConfigCollapsed = !resourceRootConfigCollapsed;
          renderLinkIndexPanel();
        } else if (resourceActionName === "add-root") {
          resourceRootConfigCollapsed = false;
          resourceRootDrafts = (resourceRootDrafts || []).concat([""]);
          resourceExcludeDrafts = (resourceExcludeDrafts || []).concat([""]);
          renderLinkIndexPanel();
        } else if (resourceActionName === "remove-root") {
          var removeIndex = Number(resourceAction.getAttribute("data-resource-root-index") || "-1");
          resourceRootDrafts = (resourceRootDrafts || []).filter(function (_root, idx) {
            return idx !== removeIndex;
          });
          resourceExcludeDrafts = (resourceExcludeDrafts || []).filter(function (_root, idx) {
            return idx !== removeIndex;
          });
          if (!resourceRootDrafts.length) resourceRootDrafts = [""];
          if (!resourceExcludeDrafts.length) resourceExcludeDrafts = [""];
          renderLinkIndexPanel();
        } else if (resourceActionName === "save-roots") {
          saveResourceRoots().catch(function (e) {
            resourceConfigSaving = false;
            renderLinkIndexPanel();
            setStatus("资源库目录保存失败：" + (e.message || String(e)), true);
          });
        } else if (resourceActionName === "scan") {
          scanResourceLibraries().catch(function (e) {
            setStatus("资源库扫描失败：" + (e.message || String(e)), true);
          });
        } else if (resourceActionName === "search-tree") {
          searchResourceTree().catch(function (e) {
            setStatus("资源库目录搜索失败：" + (e.message || String(e)), true);
          });
        } else if (resourceActionName === "clear-search") {
          resourceSearchSerial += 1;
          resourceTreeSearchDraft = "";
          resourceTreeSearchKeyword = "";
          resourceSearchState = null;
          resourceSearchLoading = false;
          resourceSelectedPath = "";
          renderLinkIndexPanel();
        } else if (resourceActionName === "expand-tree") {
          setAllResourceFoldersCollapsed(false);
        } else if (resourceActionName === "collapse-tree") {
          setAllResourceFoldersCollapsed(true);
        }
        return;
      }
      var browserAction = t.closest("[data-link-index-browser-action]");
      if (browserAction && linkRoot && linkRoot.contains(browserAction)) {
        var browserActionName = browserAction.getAttribute("data-link-index-browser-action");
        if (browserActionName === "expand-all") {
          setAllIndexFoldersCollapsed(false);
        } else if (browserActionName === "collapse-all") {
          setAllIndexFoldersCollapsed(true);
        }
        return;
      }
      var resourceToggle = t.closest("[data-resource-tree-toggle]");
      if (resourceToggle && owner.contains(resourceToggle)) {
        var resourceWrap = resourceToggle.closest(".resource-tree-folder-wrap");
        var resourceRow = resourceWrap ? resourceWrap.querySelector(":scope > .resource-tree-folder-row") : null;
        var resourceRelpath = resourceRow ? resourceRow.getAttribute("data-resource-tree-select") || "" : "";
        var resourceSearchActive = !!resourceTreeSearchQuery();
        var resourceNode =
          findTreeNodeByRelpath(resourceDisplayTree(resourceScanState || {}), resourceRelpath) ||
          findTreeNodeByRelpath(resourceTreeRoot(resourceScanState || {}), resourceRelpath);
        var isCollapsed = resourceToggle.classList.contains("is-collapsed");
        if (isCollapsed) {
          resourceCollapseState()[resourceRelpath] = false;
          if (!resourceSearchActive && resourceNode && !resourceNodeLoaded(resourceNode)) {
            loadResourceTreeNode(resourceRelpath)
              .then(function () {
                renderLinkIndexPanel();
              })
              .catch(function (e) {
                setStatus("资源库目录读取失败：" + (e.message || String(e)), true);
                renderLinkIndexPanel();
              });
          } else {
            renderLinkIndexPanel();
          }
        } else {
          resourceCollapseState()[resourceRelpath] = true;
          renderLinkIndexPanel();
        }
        return;
      }
      var resourceSelect = t.closest("[data-resource-tree-select]");
      if (resourceSelect && owner.contains(resourceSelect)) {
        var selectedResourceRelpath = resourceSelect.getAttribute("data-resource-tree-select") || "";
        var selectSearchActive = !!resourceTreeSearchQuery();
        var resourceNode =
          findTreeNodeByRelpath(resourceDisplayTree(resourceScanState || {}), selectedResourceRelpath) ||
          findTreeNodeByRelpath(resourceTreeRoot(resourceScanState || {}), selectedResourceRelpath);
        if (resourceNode && resourceNode.type !== "resource") {
          resourceSelectedPath = selectedResourceRelpath;
          if (!selectSearchActive && !resourceNodeLoaded(resourceNode)) {
            loadResourceTreeNode(selectedResourceRelpath)
              .then(function () {
                renderLinkIndexPanel();
              })
              .catch(function (e) {
                setStatus("资源库目录读取失败：" + (e.message || String(e)), true);
                renderLinkIndexPanel();
              });
          } else {
            renderLinkIndexPanel();
          }
        }
        return;
      }
      var toggle = t.closest("[data-link-tree-toggle]");
      if (toggle && linkRoot && linkRoot.contains(toggle)) {
        var wrap = toggle.closest(".link-tree-folder-wrap");
        var children = wrap ? wrap.querySelector(":scope > .link-tree-children") : null;
        if (children) {
          var nextHidden = !children.hidden;
          children.hidden = nextHidden;
          toggle.textContent = nextHidden ? "+" : "-";
          toggle.classList.toggle("is-collapsed", nextHidden);
          toggle.classList.toggle("is-expanded", !nextHidden);
          toggle.setAttribute("aria-expanded", nextHidden ? "false" : "true");
          var icon = wrap ? wrap.querySelector(":scope > .link-tree-folder-row .link-tree-file-icon.is-folder") : null;
          if (icon) icon.classList.toggle("is-open", !nextHidden);
          var row = wrap ? wrap.querySelector(":scope > .link-tree-folder-row") : null;
          var relpath = row ? row.getAttribute("data-link-tree-select") || "" : "";
          linkIndexCollapsedPaths[relpath] = nextHidden;
        }
        return;
      }
      var select = t.closest("[data-link-tree-select]");
      if (select && linkRoot && linkRoot.contains(select)) {
        var relpath = select.getAttribute("data-link-tree-select") || "";
        var node = findTreeNodeByRelpath(displayTree(linkIndexState || {}), relpath);
        if (node && node.type !== "link") {
          linkIndexSelectedPath = relpath;
          renderLinkIndexPanel();
        }
        return;
      }
      var fixBtn = t.closest("[data-link-index-fix-target]");
      if (fixBtn && linkRoot && linkRoot.contains(fixBtn)) {
        applyLinkTargetFix(fixBtn).catch(function (e) {
          renderLinkIndexPanel();
          setStatus("修复 .lnk 指向失败：" + (e.message || String(e)), true);
        });
        return;
      }
      var openBtn = t.closest("[data-link-index-open]");
      if (openBtn && linkRoot && linkRoot.contains(openBtn)) {
        openLinkIndexPath(openBtn).catch(function (e) {
          window.alert("打开失败：" + (e.message || String(e)));
        });
        return;
      }
      var btn = t.closest("[data-link-index-action]");
      if (!btn || !linkRoot || !linkRoot.contains(btn)) return;
      var action = btn.getAttribute("data-link-index-action");
      if (action === "reload") {
        loadLinkIndex().catch(function (e) {
          setStatus("索引目录读取失败：" + (e.message || String(e)), true);
        });
      } else if (action === "validate") {
        validateLinkIndex().catch(function (e) {
          var msg = "索引校验失败：" + (e.message || String(e));
          setLinkIndexNotice(msg, true);
          renderLinkIndexPanel();
          setStatus(msg, true);
        });
      } else if (action === "generate") {
        generateLinkIndex().catch(function (e) {
          var genMsg = "索引 DB 生成失败：" + (e.message || String(e));
          setLinkIndexNotice(genMsg, true);
          renderLinkIndexPanel();
          setStatus(genMsg, true);
        });
      } else if (action === "generate-files") {
        generateLinkIndexFiles().catch(function (e) {
          var filesMsg = "目录文件生成失败：" + (e.message || String(e));
          setLinkIndexNotice(filesMsg, true);
          renderLinkIndexPanel();
          setStatus(filesMsg, true);
        });
      }
    });
    addDocumentListener("input", function (ev) {
      var t = ev.target;
      var owner = panelEventRoot(t);
      if (!t || !t.matches || !owner) return;
      if (t.matches("[data-link-index-classify-empty]")) {
        linkIndexGroupByEmptyPath = !!t.checked;
        writePreference("nimda.linkIndexGroupByEmptyPath", linkIndexGroupByEmptyPath ? "1" : "0");
        writePreference("nimda.linkIndexEmptyPathOnly", null);
        linkIndexSelectedPath = "";
        renderLinkIndexPanel();
        setStatus(linkIndexGroupByEmptyPath ? "已按空路径分类展示。" : "已取消空路径分类。", false);
      } else if (t.matches("[data-resource-root-index]")) {
        var idx = Number(t.getAttribute("data-resource-root-index") || "-1");
        if (idx < 0) return;
        resourceRootDrafts = resourceRootDrafts || [""];
        resourceRootDrafts[idx] = t.value;
      } else if (t.matches("[data-resource-exclude-index]")) {
        var excludeIdx = Number(t.getAttribute("data-resource-exclude-index") || "-1");
        if (excludeIdx < 0) return;
        resourceExcludeDrafts = resourceExcludeDrafts || [""];
        resourceExcludeDrafts[excludeIdx] = t.value;
      } else if (t.matches("[data-resource-tree-search-input]")) {
        resourceTreeSearchDraft = t.value;
      }
    });
    addDocumentListener("keydown", function (ev) {
      var t = ev.target;
      var owner = panelEventRoot(t);
      if (!t || !t.matches || !owner) return;
      if (t.matches("[data-resource-tree-search-input]") && ev.key === "Enter") {
        resourceTreeSearchDraft = t.value;
        searchResourceTree().catch(function (e) {
          setStatus("资源库目录搜索失败：" + (e.message || String(e)), true);
        });
        ev.preventDefault();
      }
    });
    addDocumentListener("change", function (ev) {
      var t = ev.target;
      var owner = panelEventRoot(t);
      var linkRoot = slot();
      if (!t || !t.matches || !owner) return;
      if (t.matches("[data-resource-root-index]")) {
        var rootIndex = Number(t.getAttribute("data-resource-root-index") || "-1");
        if (rootIndex >= 0) {
          resourceRootDrafts = resourceRootDrafts || [""];
          resourceRootDrafts[rootIndex] = t.value;
        }
        return;
      }
      if (t.matches("[data-resource-exclude-index]")) {
        var excludeIndex = Number(t.getAttribute("data-resource-exclude-index") || "-1");
        if (excludeIndex >= 0) {
          resourceExcludeDrafts = resourceExcludeDrafts || [""];
          resourceExcludeDrafts[excludeIndex] = t.value;
        }
        return;
      }
    });
    addDocumentListener("mouseover", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var linkRoot = slot();
      var btn = t.closest("[data-link-index-open]");
      if (!btn || !linkRoot || !linkRoot.contains(btn)) return;
      showLinkTooltip(btn, ev);
    });
    addDocumentListener("mousemove", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var linkRoot = slot();
      var btn = t.closest("[data-link-index-open]");
      if (!btn || !linkRoot || !linkRoot.contains(btn)) return;
      placeLinkTooltip(ev);
    });
    addDocumentListener("mouseout", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var linkRoot = slot();
      var btn = t.closest("[data-link-index-open]");
      if (!btn || !linkRoot || !linkRoot.contains(btn)) return;
      var related = ev.relatedTarget;
      if (related && related.closest && related.closest("[data-link-index-open]") === btn) return;
      hideLinkTooltip();
    });
    addDocumentListener("mousemove", function (ev) {
      if (resourceTreeResizing) {
        setResourceTreeWidthFromClientX(ev.clientX);
        ev.preventDefault();
        return;
      }
      if (linkIndexTreeResizing) {
        setLinkIndexTreeWidthFromClientX(ev.clientX);
        ev.preventDefault();
      }
    });
    addDocumentListener("mouseup", function () {
      if (!linkIndexTreeResizing && !resourceTreeResizing) return;
      linkIndexTreeResizing = false;
      resourceTreeResizing = false;
      document.body.classList.remove("is-link-index-resizing");
    });
  }

  function mountLinkIndexPanel(nextCtx) {
    featureCtx = nextCtx || featureCtx;
    bindSubtabsOnce();
    bindLinkIndexOnce();
    renderLinkIndexPanel();
    setCollectionDetailSubtab(activeSubtab);
  }

  root.register({
    id: "collection-detail",
    label: "作品数据",
    tabId: "tab-collection-detail",
    viewId: "collection-detail-view",
    order: 10,
    init: function (nextCtx) {
      featureActive = true;
      mountLinkIndexPanel(nextCtx);
    },
    activate: function (nextCtx) {
      featureActive = true;
      featureCtx = nextCtx || featureCtx;
      if (ctx() && typeof ctx().syncSaveToolbar === "function") {
        ctx().syncSaveToolbar();
      }
      mountLinkIndexPanel(nextCtx);
    },
    deactivate: function (nextCtx) {
      featureCtx = nextCtx || featureCtx;
      featureActive = false;
      releaseLargePayloads();
      renderLinkIndexPanel();
    },
    refreshAfterConfig: function (nextCtx) {
      featureCtx = nextCtx || featureCtx;
      if (linkIndexState) {
        loadLinkIndex().catch(function (e) {
          setStatus("索引目录读取失败：" + (e.message || String(e)), true);
        });
      } else {
        renderLinkIndexPanel();
      }
    },
    dispose: function () {
      disposeCollectionDetailFeature();
    },
  });
})();
