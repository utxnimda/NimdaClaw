(function () {
  var root = (window.JpTvBrowseFeatureRegistry =
    window.JpTvBrowseFeatureRegistry || {
      features: [],
      register: function (feature) {
        this.features.push(feature);
      },
    });

  var featureCtx = null;
  var linkIndexBound = false;
  var subtabBound = false;
  var linkIndexState = null;
  var linkIndexLoading = false;
  var linkIndexSelectedPath = "";
  var linkIndexCollapsedPaths = {};
  var linkIndexGroupByLink = false;
  var linkIndexGroupByDb = false;
  var linkIndexGroupByDbLinked = false;
  var linkIndexShowFixableOnly = false;
  var linkIndexTooltipEl = null;
  var linkTargetFixProgress = null;
  var linkTargetFixRunning = false;
  var linkTargetFixCancelRequested = false;
  var linkTargetFixConfirmUntil = 0;
  var linkTargetFixConfirmSignature = "";
  var linkAssociationState = null;
  var linkAssociationLoading = false;
  var linkAssociationWriting = false;
  var linkAssociationCancelRequested = false;
  var resourceRootDrafts = null;
  var resourceExcludeDrafts = null;
  var resourceScanState = null;
  var resourceScanLoading = false;
  var resourceSearchState = null;
  var resourceSearchLoading = false;
  var resourceNodeLoadingPaths = {};
  var resourceConfigSaving = false;
  var resourceTreeCollapsedPaths = {};
  var resourceSelectedPath = "";
  var resourceTreeSearchDraft = "";
  var resourceTreeSearchKeyword = "";
  var resourceRootConfigCollapsed = true;
  var resourceTreeWidth = Number(localStorage.getItem("nimda.resourceTreeWidth") || "360");
  var resourceTreeResizing = false;
  var linkIndexTreeWidth = Number(localStorage.getItem("nimda.linkIndexTreeWidth") || "360");
  var linkIndexTreeResizing = false;
  var activeSubtab = "list";
  var LINK_INDEX_GROUP_PREFIX = "__group__";

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
    activeSubtab = name === "index" || name === "resource" ? name : "list";
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
    if (activeSubtab === "index" && !linkIndexState && !linkIndexLoading) {
      loadLinkIndex().catch(function (e) {
        setStatus("索引目录读取失败：" + (e.message || String(e)), true);
      });
    }
    if (activeSubtab === "resource" && !resourceScanState && !resourceScanLoading) {
      loadResourceScanCache().catch(function (e) {
        setStatus("资源库缓存读取失败：" + (e.message || String(e)), true);
      });
    }
  }

  function bindSubtabsOnce() {
    if (subtabBound) return;
    subtabBound = true;
    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var btn = t.closest("[data-collection-detail-subtab]");
      if (!btn) return;
      setCollectionDetailSubtab(btn.getAttribute("data-collection-detail-subtab"));
    });
  }

  function summaryText(data) {
    var summary = (data && data.plan_summary) || {};
    var mapping = (data && data.mapping_summary) || {};
    return (
      "索引 " +
      (summary.total || 0) +
      " 项，未配置 " +
      (mapping.unconfigured_press || 0) +
      " 项，目标丢失 " +
      (summary.missing_target || 0) +
      " 项，磁盘未关联 " +
      (summary.unmapped_on_disk || 0) +
      " 项"
    );
  }

  function configuredResourceRoots(data) {
    var cfg = (data && data.config) || {};
    var roots = Array.isArray(cfg.resource_roots) ? cfg.resource_roots : [];
    if (!roots.length && cfg.media_root) roots = [cfg.media_root];
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
    var searchActive = !!resourceTreeSearchQuery();
    var collapsed = hasChildren && (searchActive && loaded ? false : resourceTreeCollapsedPaths[relpath] === true || !loaded);
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
          (loaded
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

  function assocText(node) {
    var assoc = Array.isArray(node && node.associated) ? node.associated : [];
    if (!assoc.length) return "";
    var names = assoc
      .slice(0, 3)
      .map(function (x) {
        return x && x.name ? x.name : "";
      })
      .filter(Boolean);
    var suffix = assoc.length > names.length ? " +" + (assoc.length - names.length) : "";
    return (names.join(" / ") || assoc.length + " 项") + suffix;
  }

  function warningsText(node) {
    var warnings = Array.isArray(node && node.warnings) ? node.warnings : [];
    return warnings.filter(Boolean).join(" / ");
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

  function plannedLinkName(node) {
    return displayLinkName((node && node.name) || pathFileName((node && node.relpath) || ""));
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
    if (relpath.indexOf(LINK_INDEX_GROUP_PREFIX + "/") === 0) {
      var rest = relpath.slice(LINK_INDEX_GROUP_PREFIX.length + 1);
      if (rest.indexOf("/") < 0) return "";
    }
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
    return !!(node && linkHasLnk(node) && node.target_exists === true);
  }

  function linkHasLnk(node) {
    if (!node) return false;
    if (node.matched_shortcut_path || node.matched_shortcut_relpath) return true;
    if (node.link_exists === true) return true;
    if (Object.prototype.hasOwnProperty.call(node, "shortcut_exists")) return !!node.shortcut_exists;
    if (Object.prototype.hasOwnProperty.call(node, "link_exists")) return !!node.link_exists;
    return false;
  }

  function linkHasDb(node) {
    return !!(node && (node.db_associated || node.db_name_matched));
  }

  function linkHasDbLinked(node) {
    return !!(node && node.db_linked);
  }

  function linkHasFix(node) {
    return !!(node && node.target_fix && typeof node.target_fix === "object" && node.target_fix.target_path);
  }

  function linkStateClass(node) {
    if (!node || (node.type || "folder") !== "link") return "";
    var hasLink = linkHasLnk(node);
    var hasDb = linkHasDb(node);
    if (hasLink && hasDb) return "state-has-link-has-db";
    if (hasLink && !hasDb) return "state-has-link-no-db";
    if (!hasLink && hasDb) return "state-no-link-has-db";
    return "state-no-link-no-db";
  }

  function collectLinkLeaves(node, ancestors, out) {
    if (!node || typeof node !== "object") return;
    if ((node.type || "folder") === "link") {
      out.push({ node: node, ancestors: ancestors.slice() });
      return;
    }
    var nextAncestors = node.type === "root" ? ancestors : ancestors.concat([node]);
    var children = Array.isArray(node.children) ? node.children : [];
    for (var i = 0; i < children.length; i++) {
      collectLinkLeaves(children[i], nextAncestors, out);
    }
  }

  function categoryDefinitions() {
    if (!linkIndexGroupByLink && !linkIndexGroupByDb && !linkIndexGroupByDbLinked) return [];
    var defs = [];
    var linkVals = linkIndexGroupByLink ? [true, false] : [null];
    var dbVals = linkIndexGroupByDb ? [true, false] : [null];
    var linkedVals = linkIndexGroupByDbLinked ? [true, false] : [null];
    for (var li = 0; li < linkVals.length; li++) {
      for (var di = 0; di < dbVals.length; di++) {
        for (var bi = 0; bi < linkedVals.length; bi++) {
          var linkVal = linkVals[li];
          var dbVal = dbVals[di];
          var linkedVal = linkedVals[bi];
          var parts = [];
          var keyParts = [];
          if (linkVal !== null) {
            parts.push(linkVal ? "lnk存在" : "lnk缺失");
            keyParts.push("link-" + (linkVal ? "1" : "0"));
          }
          if (dbVal !== null) {
            parts.push(dbVal ? "DB存在" : "DB缺失");
            keyParts.push("db-" + (dbVal ? "1" : "0"));
          }
          if (linkedVal !== null) {
            parts.push(linkedVal ? "DB已关联" : "DB未关联");
            keyParts.push("linked-" + (linkedVal ? "1" : "0"));
          }
          defs.push({
            key: keyParts.join("_"),
            label: parts.join(" / "),
            link: linkVal,
            db: dbVal,
            dbLinked: linkedVal,
          });
        }
      }
    }
    return defs;
  }

  function linkMatchesCategory(node, def) {
    if (def.link !== null && linkHasLnk(node) !== def.link) return false;
    if (def.db !== null && linkHasDb(node) !== def.db) return false;
    if (def.dbLinked !== null && linkHasDbLinked(node) !== def.dbLinked) return false;
    return true;
  }

  function copyArray(raw) {
    return Array.isArray(raw) ? raw.slice() : [];
  }

  function makeVirtualFolder(name, relpath, path, displayPath) {
    return {
      type: "folder",
      name: name,
      relpath: relpath,
      path: path || "",
      _display_path: displayPath || "",
      associated: [],
      children: [],
      _children_by_relpath: Object.create(null),
    };
  }

  function virtualFolderChild(parent, sourceFolder, prefix, displayPrefix) {
    var sourceRel = nodeRelpath(sourceFolder);
    var relpath = prefix + "/" + sourceRel;
    var existing = parent._children_by_relpath && parent._children_by_relpath[relpath];
    if (existing) return existing;
    var child = makeVirtualFolder(
      sourceFolder.name || "",
      relpath,
      sourceFolder.path || "",
      displayPrefix + "/" + sourceRel
    );
    child.associated = copyArray(sourceFolder.associated);
    child.warnings = copyArray(sourceFolder.warnings);
    parent._children_by_relpath[relpath] = child;
    parent.children.push(child);
    return child;
  }

  function cloneLinkForVirtualTree(node, prefix, displayPrefix) {
    var out = {};
    Object.keys(node || {}).forEach(function (key) {
      if (key === "children") return;
      out[key] = node[key];
    });
    out.relpath = prefix + "/" + nodeRelpath(node);
    out._display_path = displayPrefix + "/" + nodeRelpath(node);
    out.associated = copyArray(node && node.associated);
    out.warnings = copyArray(node && node.warnings);
    out.children = [];
    return out;
  }

  function folderLeafCount(node) {
    if (!node || typeof node !== "object") return 0;
    if ((node.type || "folder") === "link") return 1;
    var total = 0;
    var children = Array.isArray(node.children) ? node.children : [];
    for (var i = 0; i < children.length; i++) {
      total += folderLeafCount(children[i]);
    }
    return total;
  }

  function annotateVirtualFolderSummaries(node) {
    if (!node || typeof node !== "object" || node.type === "link") return 0;
    var total = 0;
    var children = Array.isArray(node.children) ? node.children : [];
    for (var i = 0; i < children.length; i++) {
      total += annotateVirtualFolderSummaries(children[i]);
    }
    node._summary = total ? total + " 个链接" : "0 个链接";
    return total;
  }

  function annotateLinkFolderSummaries(node) {
    if (!node || typeof node !== "object") return { links: 0, db: 0, linked: 0, total: 0 };
    if ((node.type || "folder") === "link") {
      var linkTotals = {
        links: linkHasLnk(node) ? 1 : 0,
        db: linkHasDb(node) ? 1 : 0,
        linked: linkHasDbLinked(node) ? 1 : 0,
        total: 1,
      };
      node._summary_counts = linkTotals;
      return linkTotals;
    }
    var totals = { links: 0, db: 0, linked: 0, total: 0 };
    var children = Array.isArray(node.children) ? node.children : [];
    for (var i = 0; i < children.length; i++) {
      var childStats = annotateLinkFolderSummaries(children[i]);
      totals.links += childStats.links;
      totals.db += childStats.db;
      totals.linked += childStats.linked;
      totals.total += childStats.total;
    }
    node._summary_counts = totals;
    node._summary =
      "lnk存在" + totals.links + " DB存在" + totals.db + " DB关联" + totals.linked + " 总数" + totals.total;
    return totals;
  }

  function nodeSummaryStats(node) {
    if (!node || typeof node !== "object") return { links: 0, db: 0, linked: 0, total: 0 };
    var counts = node._summary_counts || null;
    if (counts && typeof counts === "object") {
      return {
        links: Number(counts.links) || 0,
        db: Number(counts.db) || 0,
        linked: Number(counts.linked) || 0,
        total: Number(counts.total) || 0,
      };
    }
    if ((node.type || "folder") === "link") {
      return {
        links: linkHasLnk(node) ? 1 : 0,
        db: linkHasDb(node) ? 1 : 0,
        linked: linkHasDbLinked(node) ? 1 : 0,
        total: 1,
      };
    }
    return { links: 0, db: 0, linked: 0, total: 0 };
  }

  function renderTreeStats(node) {
    var stats = nodeSummaryStats(node);
    return (
      '<span class="link-tree-stat">' +
      esc(String(stats.links)) +
      '</span><span class="link-tree-stat">' +
      esc(String(stats.db)) +
      '</span><span class="link-tree-stat">' +
      esc(String(stats.linked)) +
      '</span><span class="link-tree-stat">' +
      esc(String(stats.total)) +
      "</span>"
    );
  }

  function renderTableStatsCells(node) {
    var stats = nodeSummaryStats(node);
    return (
      '<td class="link-index-stat-cell">' +
      esc(String(stats.links)) +
      '</td><td class="link-index-stat-cell">' +
      esc(String(stats.db)) +
      '</td><td class="link-index-stat-cell">' +
      esc(String(stats.linked)) +
      '</td><td class="link-index-stat-cell">' +
      esc(String(stats.total)) +
      "</td>"
    );
  }

  function buildGroupedTree(rawTree) {
    var defs = categoryDefinitions();
    if (!defs.length) return rawTree || {};
    rawTree = rawTree || {};
    var rootNode = {
      type: "root",
      name: rawTree.name || "ROOT",
      relpath: "",
      path: rawTree.path || "",
      associated: copyArray(rawTree.associated),
      children: [],
      _children_by_relpath: Object.create(null),
    };
    var categories = {};
    for (var di = 0; di < defs.length; di++) {
      var def = defs[di];
      var relpath = LINK_INDEX_GROUP_PREFIX + "/" + def.key;
      var cat = makeVirtualFolder(def.label, relpath, rawTree.path || "", def.label);
      categories[def.key] = { def: def, node: cat };
      rootNode.children.push(cat);
    }
    var leaves = [];
    collectLinkLeaves(rawTree, [], leaves);
    for (var li = 0; li < leaves.length; li++) {
      var item = leaves[li];
      for (var ci = 0; ci < defs.length; ci++) {
        var catInfo = categories[defs[ci].key];
        if (!linkMatchesCategory(item.node, catInfo.def)) continue;
        var prefix = nodeRelpath(catInfo.node);
        var displayPrefix = catInfo.def.label;
        var cur = catInfo.node;
        for (var ai = 0; ai < item.ancestors.length; ai++) {
          cur = virtualFolderChild(cur, item.ancestors[ai], prefix, displayPrefix);
        }
        cur.children.push(cloneLinkForVirtualTree(item.node, prefix, displayPrefix));
        break;
      }
    }
    annotateLinkFolderSummaries(rootNode);
    return rootNode;
  }

  function pruneTreeForFixable(node) {
    if (!node || typeof node !== "object") return null;
    if ((node.type || "folder") === "link") {
      return linkHasFix(node) ? Object.assign({}, node, { children: [] }) : null;
    }
    var children = Array.isArray(node.children) ? node.children : [];
    var kept = [];
    for (var i = 0; i < children.length; i++) {
      var child = pruneTreeForFixable(children[i]);
      if (child) kept.push(child);
    }
    if (!kept.length && node.type !== "root") return null;
    var out = {};
    Object.keys(node).forEach(function (key) {
      if (key === "children" || key === "_children_by_relpath") return;
      out[key] = node[key];
    });
    out.children = kept;
    return out;
  }

  function displayTree(data) {
    var tree = buildGroupedTree((data && data.tree) || {});
    if (linkIndexShowFixableOnly) {
      tree = pruneTreeForFixable(tree) || { type: "root", name: "ROOT", relpath: "", path: "", children: [] };
    }
    annotateLinkFolderSummaries(tree);
    return tree;
  }

  function collectFixableLinks(node, out) {
    if (!node || typeof node !== "object") return out;
    if ((node.type || "folder") === "link") {
      if (linkHasFix(node)) out.push(node);
      return out;
    }
    var children = Array.isArray(node.children) ? node.children : [];
    for (var i = 0; i < children.length; i++) {
      collectFixableLinks(children[i], out);
    }
    return out;
  }

  function fixItemFromLinkNode(node) {
    var fix = linkTargetFix(node);
    if (!fix) return null;
    var shortcutPath = node.matched_shortcut_path || node.shortcut_path || node.path || "";
    if (!shortcutPath) return null;
    return {
      shortcut_path: shortcutPath,
      shortcut_relpath: node.shortcut_relpath || node.relpath || "",
      current_target_path: node.shortcut_target_path || node.target_path || "",
      target_path: fix.target_path || "",
    };
  }

  function fixableItemsFromTree(tree) {
    var nodes = collectFixableLinks(tree, []);
    var seen = {};
    var items = [];
    for (var i = 0; i < nodes.length; i++) {
      var item = fixItemFromLinkNode(nodes[i]);
      if (!item || !item.target_path) continue;
      var key = String(item.shortcut_path || item.shortcut_relpath || "").toLowerCase();
      if (!key || seen[key]) continue;
      seen[key] = true;
      items.push(item);
    }
    return items;
  }

  function linkFixKey(path) {
    return String(path || "").replace(/\\/g, "/").toLowerCase();
  }

  function fixItemsSignature(items) {
    return (Array.isArray(items) ? items : [])
      .map(function (item) {
        return linkFixKey(item && (item.shortcut_path || item.shortcut_relpath));
      })
      .filter(Boolean)
      .join("|");
  }

  function applyLocalLinkTargetFix(item, fixed) {
    if (!linkIndexState || !linkIndexState.tree) return false;
    var shortcutKey = linkFixKey((fixed && fixed.shortcut_path) || (item && item.shortcut_path));
    if (!shortcutKey) return false;
    var targetPath = String((fixed && fixed.target_path) || (item && item.target_path) || "");
    var changed = false;
    function walk(node) {
      if (!node || typeof node !== "object") return;
      if (
        (node.type || "folder") === "link" &&
        linkFixKey(node.matched_shortcut_path || node.shortcut_path || node.path) === shortcutKey
      ) {
        node.target_path = targetPath;
        node.shortcut_target_path = targetPath;
        node.target_exists = true;
        node.shortcut_target_exists = true;
        node.target_resolved = true;
        node.target_error = "";
        node.link_exists = true;
        delete node.target_fix;
        changed = true;
        return;
      }
      var children = Array.isArray(node.children) ? node.children : [];
      for (var i = 0; i < children.length; i++) walk(children[i]);
    }
    walk(linkIndexState.tree);
    if (changed && linkIndexState.plan_summary && Number(linkIndexState.plan_summary.target_fixable) > 0) {
      linkIndexState.plan_summary.target_fixable = Math.max(0, Number(linkIndexState.plan_summary.target_fixable) - 1);
    }
    return changed;
  }

  function renderLinkTargetFixProgress() {
    if (!linkTargetFixProgress) return "";
    var p = linkTargetFixProgress;
    var parts = [];
    if (p.message) parts.push(p.message);
    if (p.total) {
      parts.push("进度 " + (Number(p.done) || 0) + " / " + p.total);
      parts.push("成功 " + (Number(p.success) || 0));
      if (p.failed) parts.push("失败 " + p.failed);
    }
    return (
      '<div class="link-target-fix-progress' +
      (p.error ? " is-error" : "") +
      '">' +
      '<span class="link-target-fix-progress-text">' +
      esc(parts.join(" · ")) +
      "</span>" +
      (linkTargetFixRunning
        ? '<button type="button" class="btn secondary sm" data-link-index-browser-action="cancel-fixable"' +
          (linkTargetFixCancelRequested ? " disabled" : "") +
          ">" +
          (linkTargetFixCancelRequested ? "中断中..." : "中断") +
          "</button>"
        : "") +
      "</div>"
    );
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
      "<span></span><span></span><span>名称</span><span>lnk存在</span><span>DB存在</span><span>DB关联</span><span>总数</span>" +
      "</div>"
    );
  }

  function renderTreeToolbar() {
    return (
      '<div class="link-index-tree-toolbar">' +
      '<span class="link-index-tree-title">目录结构</span>' +
      '<div class="link-index-tree-actions">' +
      '<button type="button" class="link-index-browser-tool-btn" data-link-index-browser-action="expand-all">全部展开</button>' +
      '<button type="button" class="link-index-browser-tool-btn" data-link-index-browser-action="collapse-all">全部折叠</button>' +
      "</div></div>"
    );
  }

  function linkOpenAttrs(node) {
    var shortcutPath = (node && (node.matched_shortcut_path || node.shortcut_path || node.path || node.open_path)) || "";
    var targetPath = (node && (node.shortcut_target_path || node.target_path)) || "";
    var hasShortcut = !!(node && linkHasLnk(node) && shortcutPath);
    var openPath = hasShortcut ? shortcutPath : (node && (node.open_path || node.target_path || shortcutPath || node.path)) || "";
    var canOpen = !!(node && openPath && (hasShortcut || linkHasExistingTarget(node)));
    return (
      ' data-link-index-open="' +
      esc(openPath) +
      '" data-link-shortcut-path="' +
      esc(shortcutPath) +
      '" data-link-target-path="' +
      esc(targetPath) +
      '" data-db-associated="' +
      esc(node && node.db_associated ? "1" : "0") +
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
        '" data-link-index-fix-shortcut="' +
        esc(node.matched_shortcut_path || node.shortcut_path || node.path || "") +
        '" data-link-index-fix-relpath="' +
        esc(node.shortcut_relpath || node.relpath || "") +
        '" data-link-index-fix-current-target="' +
        esc(node.shortcut_target_path || node.target_path || "") +
        '">修复lnk</button>' +
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
        "<td></td><td>上级目录</td><td></td><td></td><td></td><td></td><td></td></tr>";
    }
    for (var i = 0; i < children.length; i++) {
      var child = children[i] || {};
      if (child.type === "link") {
        var pathTitle = child.shortcut_target_path || child.target_path || child.target_error || "";
        var pathText = displayLinkPath(child);
        rows +=
          '<tr class="link-index-file-row is-link is-' +
          esc(child.status || "unknown") +
          " " +
          esc(linkStateClass(child)) +
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
          esc(child.shortcut_relpath || child.relpath || "") +
          "\">" +
          esc(plannedLinkName(child)) +
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
      rows = '<tr><td colspan="8" class="link-index-table-empty">暂无索引项。</td></tr>';
    }
    return rows;
  }

  function renderIndexBrowser(data) {
    var tree = displayTree(data);
    var selected = selectedFolder(data);
    var stats = childStats(selected);
    var fixableItems = fixableItemsFromTree(tree);
    var fixableSignature = fixItemsSignature(fixableItems);
    var awaitingFixConfirm =
      !!fixableItems.length &&
      !linkTargetFixRunning &&
      linkTargetFixConfirmUntil > Date.now() &&
      linkTargetFixConfirmSignature === fixableSignature;
    return (
      '<section class="link-index-browser" aria-label="索引目录浏览器">' +
      '<div class="link-index-browser-tools">' +
      '<div class="link-index-browser-tool-group" aria-label="索引目录分类">' +
      '<label class="link-index-browser-check"><input type="checkbox" data-link-index-group="link"' +
      (linkIndexGroupByLink ? " checked" : "") +
      " />按 lnk 存在</label>" +
      '<label class="link-index-browser-check"><input type="checkbox" data-link-index-group="db"' +
      (linkIndexGroupByDb ? " checked" : "") +
      " />按 DB 存在</label>" +
      '<label class="link-index-browser-check"><input type="checkbox" data-link-index-group="db-linked"' +
      (linkIndexGroupByDbLinked ? " checked" : "") +
      " />按 DB 关联</label>" +
      '<label class="link-index-browser-check"><input type="checkbox" data-link-index-group="fixable"' +
      (linkIndexShowFixableOnly ? " checked" : "") +
      (linkTargetFixRunning ? " disabled" : "") +
      " />显示可修复</label>" +
      '<button type="button" class="link-index-browser-tool-btn" data-link-index-browser-action="repair-fixable"' +
      (fixableItems.length && !linkTargetFixRunning ? "" : " disabled") +
      ">" +
      (awaitingFixConfirm ? "确认修复" : "一键修复") +
      (fixableItems.length ? " (" + esc(String(fixableItems.length)) + ")" : "") +
      "</button>" +
      "</div></div>" +
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
      "<th>名称</th><th>规则命名</th><th>状态</th><th>lnk存在</th><th>DB存在</th><th>DB关联</th><th>总数</th><th>目标 / 索引路径</th>" +
      "</tr></thead><tbody>" +
      renderFolderRows(tree, selected) +
      "</tbody></table></div>" +
      "</section></div></section>"
    );
  }

  function renderWarnings(data) {
    if (!data || !data.ok) return "";
    var mapping = data.mapping_summary || {};
    var summary = data.plan_summary || {};
    var items = [];
    if (mapping.unconfigured_press) {
      items.push("收集列表中有 " + mapping.unconfigured_press + " 个压制项未配置连接路径。");
    }
    if (summary.missing_target) {
      items.push("有 " + summary.missing_target + " 个 DB 关联的目标目录不存在。");
    }
    if (summary.target_fixable) {
      items.push("有 " + summary.target_fixable + " 个 .lnk 可从资源库目录找到修复候选。");
    }
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

  function renderPlan(plan) {
    plan = Array.isArray(plan) ? plan : [];
    if (!plan.length) {
      return '<p class="link-index-empty">暂无 DB 已配置的索引项。</p>';
    }
    var rows = "";
    var max = Math.min(plan.length, 80);
    for (var i = 0; i < max; i++) {
      var item = plan[i] || {};
      rows +=
        '<tr class="link-index-plan-row is-' +
        esc(item.status || "unknown") +
        '">' +
        "<td>" +
        esc(item.status || "") +
        "</td><td>" +
        esc(item.name || "") +
        "</td><td>" +
        esc(item.press_label || "") +
        "</td><td>" +
        esc(item.shortcut_relpath || item.shortcut_path || item.error || "") +
        "</td><td>" +
        esc(item.target_path || "") +
        "</td></tr>";
    }
    return (
      '<div class="link-index-plan-wrap"><table class="link-index-plan"><thead><tr>' +
      "<th>状态</th><th>作品</th><th>压制</th><th>索引项</th><th>目标</th>" +
      "</tr></thead><tbody>" +
      rows +
      "</tbody></table></div>"
    );
  }

  function associationEntryCanApply(entry) {
    var row = entry && entry.row ? entry.row : {};
    var candidates = Array.isArray(row && row.candidates) ? row.candidates : [];
    var candIndex = entry && entry.autoIndex >= 0 ? entry.autoIndex : 0;
    var cand = candidates[candIndex] || candidates[0] || null;
    return associationCanApply(row, cand, associationDefaultLinkOption(row));
  }

  function associationDefaultCheckedCount(groups) {
    return (groups && Array.isArray(groups.confirmed) ? groups.confirmed : []).filter(associationEntryCanApply).length;
  }

  function associationSummaryText(data, groups) {
    var summary = (data && data.summary) || {};
    groups = groups || {};
    var visibleTotal = associationRows().length;
    var visibleConfirmed = Array.isArray(groups.confirmed) ? groups.confirmed.length : 0;
    var visibleCandidates = Array.isArray(groups.candidates) ? groups.candidates.length : 0;
    var visibleNoCandidates = Array.isArray(groups.noCandidates) ? groups.noCandidates.length : 0;
    var checkedVisible = associationDefaultCheckedCount(groups);
    var totalUnmapped = summary.total_unmapped || 0;
    var visibleLabel = totalUnmapped > visibleTotal ? "当前显示前 " + visibleTotal : "当前显示 " + visibleTotal;
    return (
      "全库：未关联 " +
      totalUnmapped +
      " 项，完全匹配 " +
      (summary.exact || 0) +
      " 项，可写入 " +
      (summary.exact_auto || 0) +
      " 项；" +
      visibleLabel +
      " 项：已确认 " +
      visibleConfirmed +
      "，候选 " +
      visibleCandidates +
      "，无候选 " +
      visibleNoCandidates +
      "，默认勾选 " +
      checkedVisible +
      " 项"
    );
  }

  function associationRows() {
    return linkAssociationState && Array.isArray(linkAssociationState.rows) ? linkAssociationState.rows : [];
  }

  function associationAutoCandidateIndex(row) {
    var candidates = Array.isArray(row && row.candidates) ? row.candidates : [];
    var auto = row && row.auto_candidate && typeof row.auto_candidate === "object" ? row.auto_candidate : null;
    var defaultCandidate = row && row.default_candidate && typeof row.default_candidate === "object" ? row.default_candidate : null;
    var preferred = auto || defaultCandidate;
    if (!preferred || !candidates.length) return -1;
    for (var i = 0; i < candidates.length; i++) {
      var cand = candidates[i] || {};
      if (
        String(cand.yaml_source_rel || "") === String(preferred.yaml_source_rel || "") &&
            String(cand.index_in_file) === String(preferred.index_in_file) &&
            String(cand.press_key || "") === String(preferred.press_key || "") &&
            String(cand.suggested_path || "") === String(preferred.suggested_path || "") &&
            String(cand.suggested_press_path || "") === String(preferred.suggested_press_path || "") &&
            String(cand.suggested_target_path || "") === String(preferred.suggested_target_path || "")
      ) {
        return i;
      }
    }
    return auto && candidates[0] && candidates[0].can_auto_apply ? 0 : -1;
  }

  function associationGroupedRows() {
    var confirmed = [];
    var candidates = [];
    var noCandidates = [];
    associationRows().forEach(function (row, rowIndex) {
      var autoIndex = associationAutoCandidateIndex(row);
      var item = { row: row, rowIndex: rowIndex, autoIndex: autoIndex };
      if (row && row.can_auto_apply && autoIndex >= 0) {
        confirmed.push(item);
      } else if (row && Array.isArray(row.candidates) && row.candidates.length) {
        candidates.push(item);
      } else {
        noCandidates.push(item);
      }
    });
    return { confirmed: confirmed, candidates: candidates, noCandidates: noCandidates };
  }

  function candidateLabel(cand) {
    if (!cand) return "";
    var parts = [cand.name || "未命名"];
    if (cand.year) parts.push("[" + cand.year + "]");
    if (cand.press_label) parts.push(cand.press_label);
    if (cand.match_type === "exact") parts.push("完全匹配");
    else if (cand.score) parts.push(Math.round(cand.score * 100) + "%");
    return parts.join(" / ");
  }

  function pressOptionLabel(opt) {
    if (!opt) return "";
    var label = opt.press_label || opt.press_key || "";
    if (opt.press_path) label += " -> " + opt.press_path;
    return label;
  }

  function pressDisplayLabel(raw) {
    var label = String(raw || "").trim();
    if (!label) return "";
    label = label.replace(/[-_/\s]*----$/i, "");
    label = label.replace(/\s*[-_]\s*/g, "/");
    label = label.replace(/\/{2,}/g, "/").replace(/\/$/g, "");
    return label;
  }

  function candidateWorkDisplayLabel(cand) {
    if (!cand) return "";
    var parts = [];
    if (cand.year) parts.push("[" + cand.year + "]");
    if (cand.date_range_label) {
      parts.push(cand.date_range_label);
    } else if (cand.begin_date || cand.end_date) {
      parts.push("[" + (cand.begin_date || cand.end_date) + "]" + (cand.begin_date && cand.end_date ? "[" + cand.end_date + "]" : ""));
    }
    if (cand.name) parts.push(cand.name);
    return parts.join(" ");
  }

  function candidateYearRangeLabel(cand) {
    if (!cand) return "";
    var begin = String(cand.begin_date || "").trim() || String(cand.year || "").trim();
    var end = String(cand.end_date || "").trim() || begin;
    if (!begin && !end) return "";
    return "[" + begin + "][" + end + "]";
  }

  function candidatePressParts(cand) {
    cand = cand || {};
    var format = String(cand.press_format || "").trim();
    var group = String(cand.press_group || "").trim();
    if (!format && cand.press_label) {
      var parts = pressDisplayLabel(cand.press_label).split("/");
      format = parts[0] || "";
      group = parts.slice(1).join("/") || "";
    }
    if (group === "----") group = "";
    return { format: format, group: group };
  }

  function candidateDbCompactLabel(cand) {
    if (!cand) return "";
    var year = candidateYearRangeLabel(cand);
    var work = (year ? year + " " : "") + String(cand.name || "未命名").trim();
    var press = candidatePressParts(cand);
    var parts = [work];
    if (press.format) parts.push(press.format);
    if (press.group) parts.push(press.group);
    return parts.filter(Boolean).join("/");
  }

  function confirmedDbOwnerLabel(cand, row) {
    var work = candidateWorkDisplayLabel(cand);
    var press = pressDisplayLabel(cand && cand.press_label);
    var link = String((row && row.link_name) || "").trim();
    var details = [];
    if (press) details.push("压制: " + press);
    if (link) details.push("索引: " + link);
    if (work && details.length) return work + " / " + details.join(" / ");
    return work || details.join(" / ") || "";
  }

  function compactCandidateLabel(cand, row) {
    return candidateDbCompactLabel(cand) || confirmedDbOwnerLabel(cand, row) || candidateLabel(cand);
  }

  function associationShortcutRelpath(row) {
    return String((row && (row.shortcut_relpath || row.id || row.shortcut_path)) || "")
      .replace(/\\/g, "/")
      .replace(/\.lnk$/i, "");
  }

  function associationShortcutParentName(row) {
    var rel = associationShortcutRelpath(row);
    var parts = rel.split("/").filter(Boolean);
    if (parts.length >= 2) return parts[parts.length - 2];
    return parts[0] || rel;
  }

  function associationShortcutDisplay(row, kind) {
    return kind === "candidate" ? associationShortcutParentName(row) : associationShortcutRelpath(row);
  }

  function associationLinkOptions(row) {
    var options = Array.isArray(row && row.link_options) ? row.link_options : [];
    if (!options.length && row) {
      options = [
        {
          shortcut_relpath: row.shortcut_relpath || row.id || "",
          shortcut_path: row.shortcut_path || "",
          link_name: row.link_name || "",
          display: associationShortcutRelpath(row),
          target_path: row.target_path || "",
          target_exists: row.target_exists,
          target_relpath: row.target_relpath || "",
          target_under_media_root: row.target_under_media_root,
          target_resolved: row.target_resolved,
          target_error: row.target_error || "",
        },
      ];
    }
    return options;
  }

  function associationDefaultLinkOption(row) {
    var options = associationLinkOptions(row);
    return options[0] || null;
  }

  function associationLinkOptionLabel(opt) {
    if (!opt) return "";
    return String(opt.display || opt.shortcut_relpath || opt.shortcut_path || opt.link_name || "").replace(/\.lnk$/i, "");
  }

  function associationLinkOptionForRow(rowEl) {
    var rows = associationRows();
    var rowIndex = Number(rowEl.getAttribute("data-link-assoc-row") || "-1");
    var row = rows[rowIndex] || {};
    var options = associationLinkOptions(row);
    var select = rowEl.querySelector("[data-link-assoc-shortcut]");
    var idx = Number((select && select.value) || "0");
    return options[idx] || options[0] || null;
  }

  function associationSuggestedTargetPath(row, cand, linkOpt) {
    return (
      (linkOpt && linkOpt.target_path) ||
      (cand && (cand.suggested_target_path || cand.target_path)) ||
      (row && row.target_path) ||
      ""
    );
  }

  function associationCanApply(row, cand, linkOpt) {
    var targetPath = associationSuggestedTargetPath(row, cand, linkOpt);
    var linkTargetOk = linkOpt ? linkOpt.target_exists === true : !!(cand && cand.can_apply);
    return !!(cand && cand.press_key && targetPath && linkTargetOk);
  }

  function renderAssociationShortcutControl(row, selectedLink) {
    var options = associationLinkOptions(row);
    selectedLink = selectedLink || options[0] || null;
    if (options.length <= 1) {
      return (
        '<span class="link-assoc-static" title="' +
        esc((selectedLink && (selectedLink.shortcut_relpath || selectedLink.shortcut_path)) || row.shortcut_relpath || row.shortcut_path || "") +
        '">' +
        esc(associationLinkOptionLabel(selectedLink) || associationShortcutRelpath(row)) +
        "</span>"
      );
    }
    return (
      '<select class="link-assoc-compact-select link-assoc-shortcut" data-link-assoc-shortcut title="' +
      esc(associationLinkOptionLabel(selectedLink)) +
      '">' +
      options
        .map(function (item, idx) {
          return (
            '<option value="' +
            esc(String(idx)) +
            '"' +
            (item === selectedLink ? " selected" : "") +
            ">" +
            esc(associationLinkOptionLabel(item)) +
            "</option>"
          );
        })
        .join("") +
      "</select>"
    );
  }

  function renderAssociationPressOptions(cand, selectedPressKey) {
    var options = Array.isArray(cand && cand.press_options) ? cand.press_options : [];
    if (!options.length && cand && cand.press_key) {
      options = [{ press_key: cand.press_key, press_label: cand.press_label || cand.press_key }];
    }
    return options
      .map(function (opt) {
        var key = opt.press_key || "";
        return (
          '<option value="' +
          esc(key) +
          '"' +
          (key === selectedPressKey ? " selected" : "") +
          ">" +
          esc(pressOptionLabel(opt)) +
          "</option>"
        );
      })
      .join("");
  }

  function renderAssociationHiddenControls(cand, candIndex, canApply, targetPath, opts) {
    opts = opts || {};
    return (
      (opts.omitCandidate
        ? ""
        : '<select class="link-assoc-hidden-control" data-link-assoc-candidate' +
          (canApply ? "" : " disabled") +
          ' aria-hidden="true" tabindex="-1">' +
          '<option value="' +
          esc(String(Math.max(0, candIndex || 0))) +
          '" selected></option></select>') +
      '<select class="link-assoc-hidden-control" data-link-assoc-press' +
      (canApply ? "" : " disabled") +
      ' aria-hidden="true" tabindex="-1">' +
      '<option value="' +
      esc((cand && cand.press_key) || "") +
      '" selected></option>' +
      "</select>" +
      '<input class="link-assoc-hidden-control link-assoc-target-path" data-link-assoc-target-path type="hidden" value="' +
      esc(targetPath || "") +
      '" />'
    );
  }

  function renderAssociationCandidateSelect(row, candidates, selectedCand, canApply) {
    if (!candidates.length) {
      return '<span class="link-assoc-static is-empty"></span>';
    }
    return (
      '<select class="link-assoc-compact-select link-assoc-candidate" data-link-assoc-candidate title="' +
      esc(compactCandidateLabel(selectedCand, row)) +
      '"' +
      (candidates.length ? "" : " disabled") +
      ">" +
      candidates
        .map(function (item, idx) {
          return (
            '<option value="' +
            esc(String(idx)) +
            '"' +
            (item === selectedCand ? " selected" : "") +
            ">" +
            esc(compactCandidateLabel(item, row)) +
            "</option>"
          );
        })
        .join("") +
      "</select>"
    );
  }

  function renderAssociationTableColgroup() {
    return (
      '<colgroup><col class="link-assoc-col-check" />' +
      '<col class="link-assoc-col-db" />' +
      '<col class="link-assoc-col-index" />' +
      '<col class="link-assoc-col-target" />' +
      '<col class="link-assoc-col-action" /></colgroup>'
    );
  }

  function parentPath(raw) {
    var s = String(raw || "").trim();
    if (!s) return "";
    s = s.replace(/[\\/]+$/g, "");
    var idx = Math.max(s.lastIndexOf("\\"), s.lastIndexOf("/"));
    return idx > 0 ? s.slice(0, idx) : "";
  }

  function renderAssociationRowActions(row, canApply, opts) {
    opts = opts || {};
    row = row || {};
    var shortcutPath = row.shortcut_path || "";
    var shortcutDir = parentPath(shortcutPath);
    var canOpen = !!shortcutPath;
    var canOpenShortcutDir = !!shortcutDir;
    var showApply = opts.showApply !== false;
    var actionCount = (showApply ? 1 : 0) + (opts.showReject ? 1 : 0) + 1 + (canOpenShortcutDir ? 1 : 0);
    return (
      '<div class="link-assoc-row-actions' +
      (opts.showReject ? " has-reject" : "") +
      (actionCount >= 4 ? " has-four" : actionCount >= 3 ? " has-three" : "") +
      '">' +
      (showApply
        ? '<button type="button" class="btn secondary sm" data-link-assoc-action="apply-row"' +
          (canApply ? "" : " disabled") +
          ">关联</button>"
        : "") +
      (opts.showReject
        ? '<button type="button" class="btn secondary sm" data-link-assoc-action="reject-row"' +
          (opts.canReject ? "" : " disabled") +
          ">不是关联</button>"
        : "") +
      '<button type="button" class="btn secondary sm" data-link-index-open="' +
      esc(shortcutPath) +
      '" data-link-shortcut-path="' +
      esc(shortcutPath) +
      '" data-link-assoc-open-target="1" data-db-associated="1" data-target-exists="' +
      esc(row.target_exists === false ? "0" : "1") +
      '" data-can-open="' +
      esc(canOpen ? "1" : "0") +
      '"' +
      (canOpen ? "" : " disabled") +
      ">打开目标</button>" +
      (canOpenShortcutDir
        ? '<button type="button" class="btn secondary sm" data-link-index-open="' +
          esc(shortcutDir) +
          '" data-link-assoc-open-shortcut-dir="1" data-db-associated="1" data-target-exists="1" data-can-open="1">打开lnk目录</button>'
        : "") +
      "</div>"
    );
  }

  function renderAssociationRows(rows) {
    if (!rows.length) {
      return '<p class="link-index-empty">暂无未关联索引项。</p>';
    }
    var html = "";
    rows.forEach(function (row, rowIndex) {
      var candidates = Array.isArray(row.candidates) ? row.candidates : [];
      var cand = candidates[0] || null;
      var canApply = !!(cand && cand.can_apply);
      var reason = (cand && cand.reason) || row.target_error || "";
      html +=
        '<tr class="link-assoc-row" data-link-assoc-row="' +
        esc(String(rowIndex)) +
        '">' +
        '<td><div class="link-assoc-main">' +
        '<strong title="' +
        esc(row.shortcut_path || "") +
        '">' +
        esc(row.work_name_hint || row.link_name || "") +
        "</strong>" +
        '<span title="' +
        esc(row.target_path || "") +
        '">' +
        esc(row.link_name || "") +
        "</span>" +
        (row.target_under_media_root
          ? '<em class="is-ok">目标在媒体根目录内</em>'
          : '<em>需确认相对路径</em>') +
        "</div></td>" +
        '<td><select class="link-assoc-candidate" data-link-assoc-candidate' +
        (candidates.length ? "" : " disabled") +
        ">" +
        (candidates.length
          ? candidates
              .map(function (item, candIndex) {
                return (
                  '<option value="' +
                  esc(String(candIndex)) +
                  '">' +
                  esc(candidateLabel(item)) +
                  "</option>"
                );
              })
              .join("")
          : '<option value="">无候选</option>') +
        "</select>" +
        (reason ? '<div class="link-assoc-note">' + esc(reason) + "</div>" : "") +
        "</td>" +
        '<td><select class="link-assoc-press" data-link-assoc-press' +
        (canApply ? "" : " disabled") +
        ">" +
        renderAssociationPressOptions(cand, cand && cand.press_key) +
        "</select></td>" +
        '<td><input class="link-assoc-target-path" data-link-assoc-target-path type="text" value="' +
        esc(associationSuggestedTargetPath(row, cand)) +
        '" /></td>' +
        '<td><button type="button" class="btn secondary sm" data-link-assoc-action="apply-row"' +
        (canApply ? "" : " disabled") +
        ">关联</button></td></tr>";
    });
    return (
      '<div class="link-assoc-table-wrap"><table class="link-assoc-table"><thead><tr>' +
      "<th>索引项</th><th>候选作品</th><th>压制项</th><th>实际资源目录</th><th></th>" +
      "</tr></thead><tbody>" +
      html +
      "</tbody></table></div>"
    );
  }

  function renderAssociationRowSection(items, opts) {
    opts = opts || {};
    if (!items.length) {
      return '<p class="link-index-empty">' + esc(opts.emptyText || "暂无匹配项。") + "</p>";
    }
    var html = "";
    items.forEach(function (entry) {
      var row = entry.row || {};
      var rowIndex = entry.rowIndex;
      var candidates = Array.isArray(row.candidates) ? row.candidates : [];
      var candIndex = entry.autoIndex >= 0 ? entry.autoIndex : 0;
      var cand = candidates[candIndex] || candidates[0] || null;
      var selectedLink = associationDefaultLinkOption(row);
      var canApply = associationCanApply(row, cand, selectedLink);
      var targetPath = associationSuggestedTargetPath(row, cand, selectedLink);
      var showCandidateCheck = opts.kind === "candidate";
      html +=
        '<tr class="link-assoc-row' +
        (opts.confirmed ? " is-confirmed" : " is-pending") +
        " is-confirmed-compact" +
        (opts.kind ? " is-" + esc(opts.kind) : "") +
        '" data-link-assoc-row="' +
        esc(String(rowIndex)) +
        '">' +
        '<td class="link-assoc-check-cell">' +
        (showCandidateCheck
          ? '<input type="checkbox" data-link-assoc-check' +
            (canApply ? "" : " disabled") +
            (opts.defaultChecked && canApply ? " checked" : "") +
            " />"
          : "") +
        "</td>" +
        "<td>" +
        renderAssociationCandidateSelect(row, candidates, cand, canApply) +
        renderAssociationHiddenControls(cand, candIndex, canApply, targetPath, { omitCandidate: true }) +
        "</td>" +
        "<td>" +
        renderAssociationShortcutControl(row, selectedLink) +
        "</td>" +
        '<td><span class="link-assoc-static" data-link-assoc-target-text title="' +
        esc(targetPath || "") +
        '">' +
        esc(targetPath || "") +
        "</span></td>" +
        "<td>" +
        renderAssociationRowActions(row, canApply, {
          showApply: opts.kind !== "no-candidate",
          showReject: opts.kind === "candidate",
          canReject: !!(cand && cand.reject_key),
        }) +
        "</td></tr>";
    });
    return (
      '<div class="link-assoc-section-head"><strong>' +
      esc(opts.title || "匹配项") +
      "</strong><span>" +
      esc(String(items.length)) +
      "</span></div>" +
      '<div class="link-assoc-table-wrap"><table class="link-assoc-table link-assoc-match-table link-assoc-compact-table">' +
      renderAssociationTableColgroup() +
      "<thead><tr>" +
      "<th></th><th>DB所属</th><th>索引目录相对路径</th><th>实际链接目录绝对路径</th><th>操作</th>" +
      "</tr></thead><tbody>" +
      html +
      "</tbody></table></div>"
    );
  }

  function renderConfirmedAssociationSection(items, opts) {
    opts = opts || {};
    if (!items.length) {
      return '<p class="link-index-empty">' + esc(opts.emptyText || "暂无已确认匹配。") + "</p>";
    }
    var html = "";
    items.forEach(function (entry) {
      var row = entry.row || {};
      var rowIndex = entry.rowIndex;
      var candidates = Array.isArray(row.candidates) ? row.candidates : [];
      var candIndex = entry.autoIndex >= 0 ? entry.autoIndex : 0;
      var cand = candidates[candIndex] || candidates[0] || null;
      var selectedLink = associationDefaultLinkOption(row);
      var canApply = associationCanApply(row, cand, selectedLink);
      var targetPath = associationSuggestedTargetPath(row, cand, selectedLink);
      html +=
        '<tr class="link-assoc-row is-confirmed is-confirmed-compact" data-link-assoc-row="' +
        esc(String(rowIndex)) +
        '">' +
        '<td class="link-assoc-check-cell"><input type="checkbox" data-link-assoc-check' +
        (canApply ? "" : " disabled") +
        (opts.defaultChecked && canApply ? " checked" : "") +
        ' /></td>' +
        '<td><span class="link-assoc-static is-db-owner" title="' +
        esc(confirmedDbOwnerLabel(cand, row)) +
        '">' +
        esc(confirmedDbOwnerLabel(cand, row)) +
        "</span>" +
        renderAssociationHiddenControls(cand, candIndex, canApply, targetPath) +
        "</td>" +
        "<td>" +
        renderAssociationShortcutControl(row, selectedLink) +
        "</td>" +
        '<td><span class="link-assoc-static" title="' +
        esc(targetPath) +
        '">' +
        esc(targetPath) +
        "</span></td>" +
        "<td>" +
        renderAssociationRowActions(row, canApply) +
        "</td></tr>";
    });
    return (
      '<div class="link-assoc-section-head"><strong>' +
      esc(opts.title || "已确认匹配") +
      "</strong><span>" +
      esc(String(items.length)) +
      "</span></div>" +
      '<div class="link-assoc-table-wrap"><table class="link-assoc-table link-assoc-match-table link-assoc-confirmed-table">' +
      renderAssociationTableColgroup() +
      "<thead><tr>" +
      "<th>选中</th><th>DB所属</th><th>索引目录相对路径</th><th>实际链接目录绝对路径</th><th>操作</th>" +
      "</tr></thead><tbody>" +
      html +
      "</tbody></table></div>"
    );
  }

  function renderAssociationGroupedRows(groups) {
    groups = groups || associationGroupedRows();
    return (
      renderConfirmedAssociationSection(groups.confirmed || [], {
        title: "已确认匹配（完全匹配，默认勾选）",
        emptyText: "暂无已确认匹配。",
        defaultChecked: true,
      }) +
      renderAssociationRowSection(groups.candidates || [], {
        title: "候选匹配（需手动确认）",
        emptyText: "暂无候选匹配。",
        confirmed: false,
        defaultChecked: false,
        kind: "candidate",
      }) +
      renderAssociationRowSection(groups.noCandidates || [], {
        title: "无候选作品",
        emptyText: "暂无无候选项目。",
        confirmed: false,
        defaultChecked: false,
        kind: "no-candidate",
      })
    );
  }

  function renderAssociationPanel() {
    var data = linkAssociationState || {};
    var loaded = !!data.ok;
    var groups = associationGroupedRows();
    var selectedCount = associationDefaultCheckedCount(groups);
    var selectedEnabled = selectedCount > 0;
    return (
      '<section class="link-assoc-panel">' +
      '<div class="link-assoc-head">' +
      '<div><h3>DB 关联匹配</h3><p class="link-index-summary">' +
      esc(linkAssociationLoading ? "匹配中..." : loaded ? associationSummaryText(data, groups) : "未匹配") +
      "</p></div>" +
      '<div class="link-index-actions">' +
      '<button type="button" class="btn secondary sm" data-link-assoc-action="load">' +
      (loaded ? "重新匹配" : "匹配 DB") +
      "</button>" +
      '<button type="button" class="btn sm primary" data-link-assoc-action="apply-selected"' +
      (selectedEnabled ? "" : " disabled") +
      ">写入勾选关联" +
      (loaded ? "（" + esc(String(selectedCount)) + "）" : "") +
      "</button>" +
      '<button type="button" class="btn secondary sm" data-link-assoc-action="cancel-write" disabled>中断写入</button>' +
      "</div></div>" +
      '<p class="link-assoc-progress" data-link-assoc-progress></p>' +
      (loaded ? renderAssociationGroupedRows(groups) : "") +
      "</section>"
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
        '<button type="button" class="btn secondary sm" data-link-index-action="refresh-links">重新确认链接</button>' +
        '<button type="button" class="btn sm" data-link-index-action="generate"' +
        (loaded ? "" : " disabled") +
        ">生成链接</button>" +
        "</div></div>" +
        (loaded ? renderLinkTargetFixProgress() + renderWarnings(data) + renderIndexBrowser(data) + renderAssociationPanel() : "") +
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
    var refreshLinks = !!opts.refreshLinks;
    linkIndexLoading = true;
    renderLinkIndexPanel();
    setStatus(refreshLinks ? "链接重新确认中..." : "索引目录读取中...", false);
    var params = new URLSearchParams();
    params.set("lite", "1");
    if (refreshLinks) params.set("refresh_links", "1");
    var out = await fetchJson("/api/collection-detail/link-index?" + params.toString(), {
      method: "GET",
    });
    linkIndexLoading = false;
    if (!out.res.ok || !out.data || !out.data.ok) {
      renderLinkIndexPanel();
      setStatus((out.data && out.data.error) || "索引目录读取失败。", true);
      return;
    }
    linkIndexState = out.data;
    if (resourceRootDrafts === null) ensureResourceRootDrafts(linkIndexState);
    linkAssociationState = null;
    renderLinkIndexPanel();
    setStatus(refreshLinks ? "链接确认已更新。" : "", false);
  }

  async function loadResourceScanCache() {
    var out = await fetchJson("/api/collection-detail/resource-libraries/cache", { method: "GET" });
    if (!out.res.ok || !out.data || !out.data.ok) {
      throw new Error((out.data && out.data.error) || "资源库缓存读取失败。");
    }
    resourceScanState = out.data;
    resourceSearchState = null;
    resourceSearchLoading = false;
    resourceNodeLoadingPaths = {};
    syncResourceRootDraftsFromConfig(resourceScanState);
    renderLinkIndexPanel();
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
    var existing = findTreeNodeByRelpath(resourceTreeRoot(resourceScanState || {}), relpath);
    if (existing && resourceNodeLoaded(existing)) return existing;
    if (resourceNodeLoadingPaths[relpath]) return existing || null;
    resourceNodeLoadingPaths[relpath] = true;
    renderLinkIndexPanel();
    try {
      var params = new URLSearchParams();
      params.set("relpath", relpath);
      var out = await fetchJson("/api/collection-detail/resource-libraries/node?" + params.toString(), { method: "GET" });
      if (!out.res.ok || !out.data || !out.data.ok || !out.data.node) {
        throw new Error((out.data && out.data.error) || "资源库目录读取失败。");
      }
      return applyResourceTreeNode(out.data.node);
    } finally {
      delete resourceNodeLoadingPaths[relpath];
    }
  }

  async function searchResourceTree() {
    var query = String(resourceTreeSearchDraft || "").trim();
    resourceTreeSearchKeyword = query;
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
      if (!out.res.ok || !out.data || !out.data.ok) {
        throw new Error((out.data && out.data.error) || "资源库目录搜索失败。");
      }
      if (resourceTreeSearchKeyword === query) {
        resourceSearchState = out.data;
        setStatus("", false);
      }
    } finally {
      resourceSearchLoading = false;
      renderLinkIndexPanel();
    }
  }

  function cleanResourceRootDrafts() {
    var roots = resourceRootDrafts || [];
    var out = [];
    var seen = {};
    roots.forEach(function (root) {
      var value = String(root || "").trim();
      if (!value) return;
      var key = value.toLowerCase();
      if (seen[key]) return;
      seen[key] = true;
      out.push(value);
    });
    return out;
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
      resourceScanState = null;
      resourceSearchState = null;
      resourceSearchLoading = false;
      setStatus("资源库目录已保存。", false);
    } finally {
      resourceConfigSaving = false;
      renderLinkIndexPanel();
    }
  }

  async function scanResourceLibraries() {
    resourceScanLoading = true;
    renderLinkIndexPanel();
    setStatus("资源库扫描中...", false);
    try {
      var out = await fetchJson("/api/collection-detail/resource-libraries/scan", { method: "GET" });
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
    } finally {
      resourceScanLoading = false;
      renderLinkIndexPanel();
    }
  }

  async function generateLinkIndex() {
    var summary = linkIndexState ? summaryText(linkIndexState) : "";
    if (!window.confirm("将根据 DB 中已保存的连接配置生成快捷方式。\n" + summary)) return;
    setStatus("索引链接生成中...", false);
    var out = await fetchJson("/api/collection-detail/link-index/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({}),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      setStatus((out.data && out.data.error) || "索引链接生成失败。", true);
      return;
    }
    linkIndexState = Object.assign({}, linkIndexState || {}, out.data);
    renderLinkIndexPanel();
    setStatus("索引链接已处理。", false);
  }

  async function loadLinkAssociations() {
    linkAssociationLoading = true;
    renderLinkIndexPanel();
    setStatus("DB 关联匹配中，正在确认 .lnk 实际目标...", false);
    var out = await fetchJson("/api/collection-detail/link-index/associations?resolve_targets=1", { method: "GET" });
    linkAssociationLoading = false;
    if (!out.res.ok || !out.data || !out.data.ok) {
      renderLinkIndexPanel();
      setStatus((out.data && out.data.error) || "DB 关联匹配失败。", true);
      return;
    }
    linkAssociationState = out.data;
    renderLinkIndexPanel();
    setStatus("", false);
  }

  function associationCandidateForRow(rowEl) {
    var rows = associationRows();
    var rowIndex = Number(rowEl.getAttribute("data-link-assoc-row") || "-1");
    var row = rows[rowIndex] || {};
    var candidates = Array.isArray(row.candidates) ? row.candidates : [];
    var select = rowEl.querySelector("[data-link-assoc-candidate]");
    var candIndex = Number((select && select.value) || "0");
    return candidates[candIndex] || null;
  }

  function syncAssociationCandidateRow(rowEl) {
    if (rowEl.classList.contains("is-written")) return;
    var cand = associationCandidateForRow(rowEl);
    var selectedLink = associationLinkOptionForRow(rowEl);
    var pressSelect = rowEl.querySelector("[data-link-assoc-press]");
    var targetPathInput = rowEl.querySelector("[data-link-assoc-target-path]");
    var targetPathText = rowEl.querySelector("[data-link-assoc-target-text]");
    var applyBtn = rowEl.querySelector('[data-link-assoc-action="apply-row"]');
    var rejectBtn = rowEl.querySelector('[data-link-assoc-action="reject-row"]');
    var check = rowEl.querySelector("[data-link-assoc-check]");
    var rows = associationRows();
    var rowIndex = Number(rowEl.getAttribute("data-link-assoc-row") || "-1");
    var row = rows[rowIndex] || {};
    var canApply = associationCanApply(row, cand, selectedLink);
    if (pressSelect) {
      if (pressSelect.classList.contains("link-assoc-hidden-control")) {
        pressSelect.innerHTML =
          '<option value="' + esc((cand && cand.press_key) || "") + '" selected></option>';
      } else {
        pressSelect.innerHTML = renderAssociationPressOptions(cand, cand && cand.press_key);
      }
      pressSelect.disabled = !(cand && cand.press_key);
    }
    if (targetPathInput) targetPathInput.value = associationSuggestedTargetPath(row, cand, selectedLink);
    if (targetPathText) {
      var targetPath = associationSuggestedTargetPath(row, cand, selectedLink);
      targetPathText.textContent = targetPath;
      targetPathText.setAttribute("title", targetPath);
    }
    if (applyBtn) applyBtn.disabled = !canApply;
    if (rejectBtn) rejectBtn.disabled = !(cand && cand.reject_key);
    if (check) {
      check.disabled = !canApply;
      if (check.disabled) check.checked = false;
    }
    var candidateSelect = rowEl.querySelector("[data-link-assoc-candidate]");
    if (candidateSelect && !candidateSelect.classList.contains("link-assoc-hidden-control")) {
      candidateSelect.setAttribute("title", compactCandidateLabel(cand, row));
    }
    var shortcutSelect = rowEl.querySelector("[data-link-assoc-shortcut]");
    if (shortcutSelect && selectedLink) {
      shortcutSelect.setAttribute("title", associationLinkOptionLabel(selectedLink));
    }
    var shortcutPath = (selectedLink && selectedLink.shortcut_path) || row.shortcut_path || "";
    var shortcutDir = parentPath(shortcutPath);
    var openTargetBtn = rowEl.querySelector("[data-link-assoc-open-target]");
    if (openTargetBtn) {
      openTargetBtn.setAttribute("data-link-index-open", shortcutPath);
      openTargetBtn.setAttribute("data-link-shortcut-path", shortcutPath);
      openTargetBtn.setAttribute("data-can-open", shortcutPath ? "1" : "0");
      openTargetBtn.setAttribute("data-target-exists", selectedLink && selectedLink.target_exists === false ? "0" : "1");
      if (selectedLink && selectedLink.target_path) {
        openTargetBtn.setAttribute("data-link-target-path", selectedLink.target_path);
      } else {
        openTargetBtn.removeAttribute("data-link-target-path");
      }
      if (selectedLink && selectedLink.target_error) {
        openTargetBtn.setAttribute("data-link-target-error", selectedLink.target_error);
      } else {
        openTargetBtn.removeAttribute("data-link-target-error");
      }
      openTargetBtn.disabled = !shortcutPath;
      setLinkTooltipTitle(openTargetBtn, linkTooltipText(openTargetBtn));
    }
    var openShortcutDirBtn = rowEl.querySelector("[data-link-assoc-open-shortcut-dir]");
    if (openShortcutDirBtn) {
      openShortcutDirBtn.setAttribute("data-link-index-open", shortcutDir);
      openShortcutDirBtn.setAttribute("data-can-open", shortcutDir ? "1" : "0");
      openShortcutDirBtn.disabled = !shortcutDir;
    }
  }

  function associationItemFromRow(rowEl) {
    var rows = associationRows();
    var rowIndex = Number(rowEl.getAttribute("data-link-assoc-row") || "-1");
    var row = rows[rowIndex] || {};
    var cand = associationCandidateForRow(rowEl);
    var selectedLink = associationLinkOptionForRow(rowEl);
    if (!cand) return null;
    var pressSelect = rowEl.querySelector("[data-link-assoc-press]");
    var targetPathInput = rowEl.querySelector("[data-link-assoc-target-path]");
    var targetPath = targetPathInput ? targetPathInput.value.trim() : associationSuggestedTargetPath(row, cand, selectedLink);
    var pressKey = pressSelect ? pressSelect.value : cand.press_key || "";
    if (!targetPath || !pressKey) return null;
    return {
      shortcut_relpath: (selectedLink && selectedLink.shortcut_relpath) || row.shortcut_relpath || "",
      yaml_source_rel: cand.yaml_source_rel || "",
      index_in_file: cand.index_in_file,
      press_key: pressKey,
      target_path: targetPath,
    };
  }

  function associationRejectItemFromRow(rowEl) {
    var rows = associationRows();
    var rowIndex = Number(rowEl.getAttribute("data-link-assoc-row") || "-1");
    var row = rows[rowIndex] || {};
    var cand = associationCandidateForRow(rowEl);
    var selectedLink = associationLinkOptionForRow(rowEl);
    if (!cand || !cand.reject_key) return null;
    return {
      reject_key: cand.reject_key || "",
      shortcut_relpath: (selectedLink && selectedLink.shortcut_relpath) || row.shortcut_relpath || "",
      target_path: associationSuggestedTargetPath(row, cand, selectedLink),
      yaml_source_rel: cand.yaml_source_rel || "",
      index_in_file: cand.index_in_file,
      press_key: cand.press_key || "",
    };
  }

  function currentCheckedAssociationCount() {
    var rootEl = slot();
    return rootEl
      ? Array.prototype.slice
          .call(rootEl.querySelectorAll("[data-link-assoc-check]:checked"))
          .filter(function (check) {
            return !check.disabled;
          }).length
      : 0;
  }

  function updateAssociationSelectedCount() {
    var rootEl = slot();
    var btn = rootEl ? rootEl.querySelector('[data-link-assoc-action="apply-selected"]') : null;
    if (!btn) return;
    var count = currentCheckedAssociationCount();
    btn.textContent = "写入勾选关联（" + count + "）";
    btn.disabled = count <= 0 || linkAssociationWriting;
  }

  function setAssociationProgress(message, isErr) {
    var root = slot();
    var el = root ? root.querySelector("[data-link-assoc-progress]") : null;
    if (el) {
      el.textContent = message || "";
      el.classList.toggle("is-error", !!isErr);
    }
    if (message) setStatus(message, !!isErr);
  }

  function setAssociationWritingUi(active) {
    var root = slot();
    var cancelBtn = root ? root.querySelector('[data-link-assoc-action="cancel-write"]') : null;
    if (cancelBtn) {
      cancelBtn.disabled = !active;
      cancelBtn.textContent = active && linkAssociationCancelRequested ? "中断中..." : "中断写入";
    }
  }

  function setAssociationRowWriteNote(rowEl, message, isErr) {
    if (!rowEl) return;
    var note = rowEl.querySelector("[data-link-assoc-write-note]");
    if (!message) {
      if (note) note.remove();
      return;
    }
    if (!note) {
      note = document.createElement("div");
      note.className = "link-assoc-note is-write-note";
      note.setAttribute("data-link-assoc-write-note", "");
      (rowEl.lastElementChild || rowEl).appendChild(note);
    }
    note.textContent = message;
    note.classList.toggle("is-error", !!isErr);
  }

  function markAssociationRowWriting(rowEl, message, skipCountUpdate) {
    if (!rowEl) return;
    rowEl.classList.add("is-writing");
    setAssociationRowWriteNote(rowEl, message || "写入中...", false);
    Array.prototype.slice
      .call(rowEl.querySelectorAll('[data-link-assoc-action="apply-row"], [data-link-assoc-check]'))
      .forEach(function (ctrl) {
        ctrl.disabled = true;
      });
    if (!skipCountUpdate) updateAssociationSelectedCount();
  }

  function markAssociationRowWritten(rowEl, message, skipCountUpdate) {
    if (!rowEl) return;
    rowEl.classList.remove("is-writing");
    rowEl.classList.add("is-written");
    setAssociationRowWriteNote(rowEl, message || "已写入", false);
    Array.prototype.slice.call(rowEl.querySelectorAll("input, select, button")).forEach(function (ctrl) {
      if (ctrl.type === "checkbox") ctrl.checked = false;
      ctrl.disabled = true;
    });
    if (!skipCountUpdate) updateAssociationSelectedCount();
  }

  function markAssociationRowWriteError(rowEl, message, skipCountUpdate) {
    if (!rowEl) return;
    rowEl.classList.remove("is-writing");
    setAssociationRowWriteNote(rowEl, message || "写入失败", true);
    syncAssociationCandidateRow(rowEl);
    if (!skipCountUpdate) updateAssociationSelectedCount();
  }

  async function postAssociationItems(items) {
    var out = await fetchJson("/api/collection-detail/link-index/associations/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ items: items }),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      throw new Error((out.data && out.data.error) || "关联写入失败。");
    }
    linkIndexState = Object.assign({}, linkIndexState || {}, out.data);
    linkAssociationState = out.data.association || linkAssociationState;
    return out.data;
  }

  async function applyAssociationEntries(entries) {
    entries = (entries || []).filter(function (entry) {
      return entry && entry.item;
    });
    if (!entries.length) {
      window.alert("没有可写入的关联项。");
      return;
    }
    if (linkAssociationWriting) {
      window.alert("关联正在写入中，请稍候。");
      return;
    }
    linkAssociationWriting = true;
    linkAssociationCancelRequested = false;
    setAssociationWritingUi(false);
    var total = entries.length;
    var items = entries.map(function (entry) {
      return entry.item;
    });
    try {
      setAssociationProgress("批量写入关联 0 / " + total + "，后端正在聚合 YAML 并重算索引...", false);
      entries.forEach(function (entry) {
        markAssociationRowWriting(entry.rowEl || null, "批量写入中", true);
      });
      updateAssociationSelectedCount();
      var result = await postAssociationItems(items);
      var unresolved = {};
      (Array.isArray(result.association_unresolved) ? result.association_unresolved : []).forEach(function (relpath) {
        unresolved[String(relpath || "").replace(/\\/g, "/").toLowerCase()] = true;
      });
      entries.forEach(function (entry) {
        var relpath = String((entry.item && entry.item.shortcut_relpath) || "").replace(/\\/g, "/").toLowerCase();
        if (relpath && unresolved[relpath]) {
          markAssociationRowWriteError(entry.rowEl || null, "已写入，但校验仍未匹配", true);
        } else {
          markAssociationRowWritten(entry.rowEl || null, "已写入", true);
        }
      });
      updateAssociationSelectedCount();
      if (result.write_warning) {
        setAssociationProgress(result.write_warning, true);
      } else {
        setAssociationProgress("关联批量写入完成 " + total + " / " + total, false);
      }
    } catch (e) {
      entries.forEach(function (entry) {
        markAssociationRowWriteError(entry.rowEl || null, "批量写入失败", true);
      });
      updateAssociationSelectedCount();
      setAssociationProgress("批量写入失败：" + (e.message || String(e)), true);
      throw e;
    } finally {
      linkAssociationWriting = false;
      linkAssociationCancelRequested = false;
      setAssociationWritingUi(false);
      updateAssociationSelectedCount();
    }
  }

  function selectedAssociationEntries() {
    var root = slot();
    if (!root) return [];
    return Array.prototype.slice
      .call(root.querySelectorAll("[data-link-assoc-check]:checked"))
      .map(function (check) {
        var rowEl = check.closest("[data-link-assoc-row]");
        return rowEl ? { rowEl: rowEl, item: associationItemFromRow(rowEl) } : null;
      })
      .filter(function (entry) {
        return entry && entry.item;
      });
  }

  async function applySelectedAssociations() {
    var entries = selectedAssociationEntries();
    if (!entries.length) {
      window.alert("请先勾选可写入的关联项。");
      return;
    }
    if (!window.confirm("将写入 " + entries.length + " 个勾选关联，继续吗？")) return;
    await applyAssociationEntries(entries);
  }

  async function applyRowAssociation(rowEl) {
    var item = associationItemFromRow(rowEl);
    if (!item) {
      window.alert("请先选择作品、压制项，并确认实际资源目录。");
      return;
    }
    await applyAssociationEntries([{ rowEl: rowEl, item: item }]);
  }

  async function rejectAssociationCandidate(rowEl) {
    var item = associationRejectItemFromRow(rowEl);
    if (!item) {
      window.alert("没有可标记的候选关系。");
      return;
    }
    if (!window.confirm("确认这个候选不是关联？双方未变化时它不会再出现在候选列表。")) return;
    var out = await fetchJson("/api/collection-detail/link-index/associations/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(item),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      throw new Error((out.data && out.data.error) || "标记不是关联失败。");
    }
    linkAssociationState = out.data.association || linkAssociationState;
    renderLinkIndexPanel();
    setStatus("已标记不是关联。", false);
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
      throw new Error((out.data && out.data.error) || "修复 .lnk 指向失败。");
    }
    if (opts.refreshPayload !== false) {
      linkIndexState = out.data;
      linkAssociationState = null;
    }
    return out.data;
  }

  async function applyLinkTargetFix(btn) {
    var targetPath = btn.getAttribute("data-link-index-fix-target") || "";
    var shortcutPath = btn.getAttribute("data-link-index-fix-shortcut") || "";
    var shortcutRelpath = btn.getAttribute("data-link-index-fix-relpath") || "";
    var currentTargetPath = btn.getAttribute("data-link-index-fix-current-target") || "";
    if (!targetPath || !shortcutPath) {
      window.alert("这个修复候选缺少 .lnk 信息。");
      return;
    }
    if (!window.confirm("将 .lnk 指向修复为：\n" + targetPath + "\n\n继续吗？")) return;
    btn.disabled = true;
    btn.textContent = "修复中...";
    await postLinkTargetFixItems([
      {
        shortcut_path: shortcutPath,
        shortcut_relpath: shortcutRelpath,
        current_target_path: currentTargetPath,
        target_path: targetPath,
      },
    ]);
    renderLinkIndexPanel();
    setStatus("已修复 .lnk 指向，并刷新索引目录。", false);
  }

  async function applyVisibleLinkTargetFixes() {
    if (linkTargetFixRunning) {
      window.alert("一键修复正在进行中。");
      return;
    }
    var tree = displayTree(linkIndexState || {});
    var items = fixableItemsFromTree(tree);
    if (!items.length) {
      window.alert("当前显示范围内没有可修复的 .lnk。");
      return;
    }
    var signature = fixItemsSignature(items);
    if (linkTargetFixConfirmUntil <= Date.now() || linkTargetFixConfirmSignature !== signature) {
      linkTargetFixConfirmUntil = Date.now() + 15000;
      linkTargetFixConfirmSignature = signature;
      linkTargetFixProgress = {
        total: items.length,
        done: 0,
        success: 0,
        failed: 0,
        message: "将逐个修复当前显示范围内 " + items.length + " 个 .lnk，再次点击“确认修复”开始。",
      };
      renderLinkIndexPanel();
      setStatus("再次点击“确认修复”开始一键修复。", false);
      return;
    }
    linkTargetFixConfirmUntil = 0;
    linkTargetFixConfirmSignature = "";
    linkTargetFixRunning = true;
    linkTargetFixCancelRequested = false;
    linkTargetFixProgress = {
      total: items.length,
      done: 0,
      success: 0,
      failed: 0,
      message: "一键修复准备中...",
    };
    renderLinkIndexPanel();
    setStatus("一键修复准备中...", false);
    var success = 0;
    var failed = 0;
    var lastError = "";
    var canceled = false;
    for (var i = 0; i < items.length; i++) {
      if (linkTargetFixCancelRequested) {
        canceled = true;
        break;
      }
      var item = items[i];
      linkTargetFixProgress = {
        total: items.length,
        done: i,
        success: success,
        failed: failed,
        message: "正在修复 " + (i + 1) + " / " + items.length + "：" + displayLinkName(pathFileName(item.shortcut_path || item.shortcut_relpath || "")),
      };
      renderLinkIndexPanel();
      setStatus(linkTargetFixProgress.message, false);
      try {
        var data = await postLinkTargetFixItems([item], { refreshPayload: false });
        var fixed = data && Array.isArray(data.fixes) ? data.fixes[0] : null;
        success += 1;
        applyLocalLinkTargetFix(item, fixed || item);
        linkTargetFixProgress = {
          total: items.length,
          done: i + 1,
          success: success,
          failed: failed,
          message: "已修复 " + (i + 1) + " / " + items.length + "：" + displayLinkName(pathFileName(item.shortcut_path || item.shortcut_relpath || "")),
        };
        renderLinkIndexPanel();
      } catch (e) {
        failed += 1;
        lastError = e.message || String(e);
        linkTargetFixProgress = {
          total: items.length,
          done: i + 1,
          success: success,
          failed: failed,
          message:
            "修复 " +
            (i + 1) +
            " / " +
            items.length +
            " 失败，继续下一项：" +
            displayLinkName(pathFileName(item.shortcut_path || item.shortcut_relpath || "")),
          error: true,
        };
        renderLinkIndexPanel();
        setStatus("一键修复失败 " + failed + " 项：" + lastError, true);
      }
    }
    linkTargetFixRunning = false;
    linkTargetFixCancelRequested = false;
    linkTargetFixProgress = {
      total: items.length,
      done: success + failed,
      success: success,
      failed: failed,
      message: canceled
        ? "一键修复已中断，正在重读索引..."
        : failed
          ? "一键修复完成，部分失败，正在重读索引..."
          : "一键修复完成，正在重读索引...",
      error: !!failed,
    };
    renderLinkIndexPanel();
    await loadLinkIndex({ refreshLinks: true });
    linkTargetFixProgress = {
      total: items.length,
      done: success + failed,
      success: success,
      failed: failed,
      message: canceled
        ? "一键修复已中断。"
        : failed
          ? "一键修复完成，部分失败：" + lastError
          : "一键修复完成。",
      error: !!failed,
    };
    renderLinkIndexPanel();
    setStatus(
      canceled
        ? "一键修复已中断：成功 " + success + "，失败 " + failed + "。"
        : failed
          ? "一键修复完成：成功 " + success + "，失败 " + failed + "。"
          : "一键修复完成：成功 " + success + "。",
      !!failed
    );
  }

  async function openLinkIndexPath(btn) {
    var canOpen = btn.getAttribute("data-can-open") === "1";
    if (btn.getAttribute("data-db-associated") !== "1" && !canOpen) {
      window.alert("这个索引项在 DB 数据中没有找到关联。");
      return;
    }
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
      var tip = linkTooltipElement();
      if (!tip.hidden) {
        tip.textContent = linkTooltipText(btn);
        tip.classList.toggle("is-error", !!btn.getAttribute("data-link-target-error"));
        if (ev) placeLinkTooltip(ev);
      }
    }
  }

  function showLinkTooltip(btn, ev) {
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
    var tip = linkTooltipElement();
    tip.hidden = true;
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
      resourceTreeCollapsedPaths = {};
      renderLinkIndexPanel();
      return;
    }
    var next = {};
    collectResourceTreeFolderPaths(resourceDisplayTree(resourceScanState || {}), next);
    resourceTreeCollapsedPaths = next;
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
    localStorage.setItem("nimda.linkIndexTreeWidth", String(next));
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
    localStorage.setItem("nimda.resourceTreeWidth", String(next));
    browser.style.setProperty("--link-tree-width", next + "px");
  }

  function bindLinkIndexOnce() {
    if (linkIndexBound) return;
    if (!slot() && !resourceSlot()) return;
    linkIndexBound = true;
    document.addEventListener("mousedown", function (ev) {
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
    document.addEventListener("click", function (ev) {
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
            resourceScanLoading = false;
            renderLinkIndexPanel();
            setStatus("资源库扫描失败：" + (e.message || String(e)), true);
          });
        } else if (resourceActionName === "search-tree") {
          searchResourceTree().catch(function (e) {
            resourceSearchLoading = false;
            renderLinkIndexPanel();
            setStatus("资源库目录搜索失败：" + (e.message || String(e)), true);
          });
        } else if (resourceActionName === "clear-search") {
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
      var assocAction = t.closest("[data-link-assoc-action]");
      if (assocAction && linkRoot && linkRoot.contains(assocAction)) {
        var assocActionName = assocAction.getAttribute("data-link-assoc-action");
        if (assocActionName === "load") {
          loadLinkAssociations().catch(function (e) {
            linkAssociationLoading = false;
            renderLinkIndexPanel();
            setStatus("DB 关联匹配失败：" + (e.message || String(e)), true);
          });
        } else if (assocActionName === "apply-selected") {
          applySelectedAssociations().catch(function (e) {
            setStatus("勾选关联写入失败：" + (e.message || String(e)), true);
          });
        } else if (assocActionName === "cancel-write") {
          if (linkAssociationWriting) {
            linkAssociationCancelRequested = true;
            setAssociationProgress("正在中断写入，当前项完成后停止...", false);
            setAssociationWritingUi(true);
          }
        } else if (assocActionName === "apply-row") {
          var assocRow = assocAction.closest("[data-link-assoc-row]");
          if (assocRow) {
            applyRowAssociation(assocRow).catch(function (e) {
              setStatus("关联写入失败：" + (e.message || String(e)), true);
            });
          }
        } else if (assocActionName === "reject-row") {
          var rejectRow = assocAction.closest("[data-link-assoc-row]");
          if (rejectRow) {
            rejectAssociationCandidate(rejectRow).catch(function (e) {
              setStatus("标记不是关联失败：" + (e.message || String(e)), true);
            });
          }
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
        } else if (browserActionName === "repair-fixable") {
          applyVisibleLinkTargetFixes().catch(function (e) {
            renderLinkIndexPanel();
            setStatus("一键修复失败：" + (e.message || String(e)), true);
          });
        } else if (browserActionName === "cancel-fixable") {
          if (linkTargetFixRunning) {
            linkTargetFixCancelRequested = true;
            linkTargetFixProgress = Object.assign({}, linkTargetFixProgress || {}, {
              message: "已请求中断，当前这一项结束后停止。",
            });
            renderLinkIndexPanel();
            setStatus("已请求中断一键修复。", false);
          }
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
          resourceTreeCollapsedPaths[resourceRelpath] = false;
          if (!resourceSearchActive && resourceNode && !resourceNodeLoaded(resourceNode)) {
            loadResourceTreeNode(resourceRelpath)
              .then(function () {
                resourceTreeCollapsedPaths[resourceRelpath] = false;
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
          resourceTreeCollapsedPaths[resourceRelpath] = true;
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
                resourceSelectedPath = selectedResourceRelpath;
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
          linkIndexLoading = false;
          renderLinkIndexPanel();
          setStatus("索引目录读取失败：" + (e.message || String(e)), true);
        });
      } else if (action === "refresh-links") {
        loadLinkIndex({ refreshLinks: true }).catch(function (e) {
          linkIndexLoading = false;
          renderLinkIndexPanel();
          setStatus("链接确认失败：" + (e.message || String(e)), true);
        });
      } else if (action === "generate") {
        generateLinkIndex().catch(function (e) {
          setStatus("索引链接生成失败：" + (e.message || String(e)), true);
        });
      }
    });
    document.addEventListener("input", function (ev) {
      var t = ev.target;
      var owner = panelEventRoot(t);
      if (!t || !t.matches || !owner) return;
      if (t.matches("[data-resource-root-index]")) {
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
    document.addEventListener("keydown", function (ev) {
      var t = ev.target;
      var owner = panelEventRoot(t);
      if (!t || !t.matches || !owner) return;
      if (t.matches("[data-resource-tree-search-input]") && ev.key === "Enter") {
        resourceTreeSearchDraft = t.value;
        searchResourceTree().catch(function (e) {
          resourceSearchLoading = false;
          renderLinkIndexPanel();
          setStatus("资源库目录搜索失败：" + (e.message || String(e)), true);
        });
        ev.preventDefault();
      }
    });
    document.addEventListener("change", function (ev) {
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
      if (!linkRoot || !linkRoot.contains(t)) return;
      if (t.matches("[data-link-assoc-candidate], [data-link-assoc-shortcut]")) {
        var assocRow = t.closest("[data-link-assoc-row]");
        if (assocRow) syncAssociationCandidateRow(assocRow);
        updateAssociationSelectedCount();
        return;
      }
      if (t.matches("[data-link-assoc-check]")) {
        updateAssociationSelectedCount();
        return;
      }
      if (!t.matches("[data-link-index-group]")) return;
      var key = t.getAttribute("data-link-index-group");
      if (key === "link") {
        linkIndexGroupByLink = !!t.checked;
      } else if (key === "db") {
        linkIndexGroupByDb = !!t.checked;
      } else if (key === "db-linked") {
        linkIndexGroupByDbLinked = !!t.checked;
      } else if (key === "fixable") {
        linkIndexShowFixableOnly = !!t.checked;
      }
      if (key !== "fixable") {
        linkIndexSelectedPath = "";
        linkIndexCollapsedPaths = {};
      }
      renderLinkIndexPanel();
    });
    document.addEventListener("mouseover", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var linkRoot = slot();
      var btn = t.closest("[data-link-index-open]");
      if (!btn || !linkRoot || !linkRoot.contains(btn)) return;
      showLinkTooltip(btn, ev);
    });
    document.addEventListener("mousemove", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var linkRoot = slot();
      var btn = t.closest("[data-link-index-open]");
      if (!btn || !linkRoot || !linkRoot.contains(btn)) return;
      placeLinkTooltip(ev);
    });
    document.addEventListener("mouseout", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var linkRoot = slot();
      var btn = t.closest("[data-link-index-open]");
      if (!btn || !linkRoot || !linkRoot.contains(btn)) return;
      var related = ev.relatedTarget;
      if (related && related.closest && related.closest("[data-link-index-open]") === btn) return;
      hideLinkTooltip();
    });
    document.addEventListener("mousemove", function (ev) {
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
    document.addEventListener("mouseup", function () {
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
      mountLinkIndexPanel(nextCtx);
    },
    activate: function (nextCtx) {
      featureCtx = nextCtx || featureCtx;
      if (ctx() && typeof ctx().syncSaveToolbar === "function") {
        ctx().syncSaveToolbar();
      }
      mountLinkIndexPanel(nextCtx);
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
  });
})();
