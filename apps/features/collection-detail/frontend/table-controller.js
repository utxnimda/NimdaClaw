(function () {
  "use strict";

  function createTableController(context) {
    if (!context || !context.featureHost || typeof context.fetchJson !== "function" || typeof context.setStatus !== "function") {
      throw new Error("Collection table requires featureHost, fetchJson and setStatus context");
    }
    var mounted = false;
    var disposed = false;
    var persistentListeners = [];

    function listen(target, type, callback, options) {
      target.addEventListener(type, callback, options);
      if (type !== "pointermove" && type !== "pointerup") {
        persistentListeners.push(function () { target.removeEventListener(type, callback, options); });
      }
    }

  const $file = document.getElementById("yaml-file");
  const $meta = document.getElementById("file-meta");
  const $yamlPickPanel = document.getElementById("yaml-pick-panel");
  const $yamlPickList = document.getElementById("yaml-pick-list");
  const $yamlPickCount = document.getElementById("yaml-pick-count");
  const $btnYamlPickAll = document.getElementById("btn-yaml-pick-all");
  const $btnYamlPickNone = document.getElementById("btn-yaml-pick-none");
  const $btnYamlPickLoad = document.getElementById("btn-yaml-pick-load");
  /** 当前文件选择器中解析得到的 File[]（与 #yaml-pick-list 勾选项对齐） */
  var yamlPickFiles = [];
  const $view = document.getElementById("viewport");
  const $btnDefault = document.getElementById("btn-load-default");
  const $btnCfg = document.getElementById("btn-reload-cfg");
  const $chkEdit = document.getElementById("chk-sheet-edit");
  const $btnSaveYaml = document.getElementById("btn-sheet-save");
  const $btnAddRow = document.getElementById("btn-sheet-add-row");
  const $btnEnumEditor = document.getElementById("btn-enum-editor");
  const $enumEditorPanel = document.getElementById("enum-editor-panel");
  const $enumEditorBody = document.getElementById("enum-editor-body");
  const $btnEnumSave = document.getElementById("btn-enum-save");
  const $btnEnumClose = document.getElementById("btn-enum-close");
  const $dbCatalogAnchor = document.querySelector(".db-catalog-anchor");
  const $dbCatalogPopover = document.getElementById("db-catalog-popover");
  const $dbCatalogList = document.getElementById("db-catalog-list");
  const $dbCatalogTotal = document.getElementById("db-catalog-total");
  const $btnDbCatalogAll = document.getElementById("btn-db-catalog-all");
  const $btnDbCatalogNone = document.getElementById("btn-db-catalog-none");
  const $selDbCatalogLoadMode = document.getElementById("db-catalog-load-mode");
  const $btnDbCatalogRun = document.getElementById("btn-db-catalog-run");

  /** GET /api/config 返回的 catalog_yaml_relpaths 副本（与后端规范化 key 一致） */
  var lastCatalogYamlRels = [];
  /** 最近一次 GET /api/config 的完整结果；用于未打开数据时判断能否新增到 DB */
  var lastServerConfig = null;
  /** 最近一次成功 POST /api/browse/catalog 的路径列表；本地上传或 GET 整库后为 null · 保存后据此刷新同一子集或整库 */
  var lastDbCatalogLoadedPaths = null;
  const LS_KEY_LAST_DB_CATALOG_LOAD = "nimda.collectionDetail.lastDbCatalogLoad.v2";
  var dbCatalogAutoRestoreAttempted = false;

  let dbCatalogPopoverDismissBound = false;

  function relocateConfigPanelToCollectionDetailTab() {
    var slot = document.getElementById("collection-detail-config-slot");
    var panel = document.getElementById("config-panel");
    if (!slot || !panel || slot.contains(panel)) return;
    slot.appendChild(panel);
    panel.classList.add("cfg-panel--in-feature");
  }

  function syncDbCatalogLoadBtnAria(open) {
    if ($btnDefault)
      $btnDefault.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function closeDbCatalogPopover() {
    if (!$dbCatalogPopover || $dbCatalogPopover.hidden) return;
    $dbCatalogPopover.hidden = true;
    syncDbCatalogLoadBtnAria(false);
  }

  function openDbCatalogPopover() {
    if (!$dbCatalogPopover || !$btnDefault || $btnDefault.disabled) return;
    renderDbCatalogList();
    $dbCatalogPopover.hidden = false;
    syncDbCatalogLoadBtnAria(true);
  }

  /** 「加载DB数据」按钮：切换打开/收起数据浮层；打开前先刷新配置列表 */
  async function toggleDbYearbookPopoverFromPrimaryBtn() {
    await loadServerConfig().catch(function () {});
    if (!$btnDefault || $btnDefault.disabled) {
      setStatus("无法加载数据列表。", true);
      return;
    }
    if (!$dbCatalogPopover) return;
    if ($dbCatalogPopover.hidden) {
      openDbCatalogPopover();
      if (!lastCatalogYamlRels.length) {
        setStatus("数据列表为空。", true);
      } else {
        setStatus("共 " + lastCatalogYamlRels.length + " 个文件。", false);
      }
    } else {
      closeDbCatalogPopover();
    }
  }

  function bindDbCatalogPopoverDismissOnce() {
    if (dbCatalogPopoverDismissBound) return;
    dbCatalogPopoverDismissBound = true;
    listen(document, "click", function (ev) {
      if (!$dbCatalogPopover || $dbCatalogPopover.hidden) return;
      if ($dbCatalogAnchor && $dbCatalogAnchor.contains(ev.target)) return;
      closeDbCatalogPopover();
    });
    listen(document, "keydown", function (evk) {
      if (evk.key !== "Escape") return;
      closeDbCatalogPopover();
    });
  }

  /** 与 #chk-sheet-edit 勾选一致；在每轮表格渲染开始时同步 */
  let sheetEditMode = false;

  /** 便于切换编辑模式后重绘表格而不必重新上传 */
  let lastBrowsePayload = null;

  const FMT = "press_format";
  const GRP = "press_group";
  /** ``data-sort`` 前缀；值段为 encodeURIComponent(format slug) */
  const FMT_SORT_PREFIX = "fmt:";
  /** 压制「汇总」列：sort/filter 专用键（与单列 fmt: 区分） */
  const SHEET_PRESS_AGG_SORT_KEY = "press_fmt_agg";
  /** localStorage：逐项压制分列是否勾选显示 */
  const LS_KEY_PRESS_SPLIT_VISIBLE = "jp-tv-browse-press-split-visible";
  const LS_KEY_SHEET_COL_WIDTHS = "nimda.collectionDetail.sheetColWidths.v3";
  var sheetManualColWidths = loadSheetManualColWidths();
  var sheetColumnResizeBound = false;
  var sheetColumnFitRaf = 0;
  var sheetMeasureEl = null;

  /** 表格列排序：null 表示按数据文件 + 文件内 index_in_file；同一列再次点击切换升序/降序 */
  let sheetSortKey = null;
  let sheetSortDir = 1;
  let newSheetRowSequence = 0;
  let sheetSortEventsBound = false;
  /** 筛选下拉：表格外点击关闭（仅绑定一次） */
  let sheetFilterPopoversBound = false;

  /** 每个「整页会话」唯一，写入筛选 input 的 name，避免浏览器刷新后恢复上一会话的输入 */
  const SHEET_FILTER_NAME_PREFIX =
    "_jp_tv_sf_" + Date.now() + "_" + Math.random().toString(36).slice(2, 11);

  /** 各列筛选框当前文本（仅本次打开页面；不写入 localStorage） */
  let persistedSheetFilters = Object.create(null);
  let sheetFilterListenersBound = false;
  let pressFmtVisBound = false;
  /** 当前表渲染时的压制格式列序（供「压制汇总」列排序比较） */
  let sheetRenderFmColsRef = [];
  let sheetEditActionsBound = false;
  let sheetInlineEditBound = false;
  let sheetAddRowBound = false;
  let enumEditorBound = false;
  let enumEditorDraft = null;
  let deletedSheetRows = Object.create(null);

  function nzForSort(s) {
    var t = String(s == null ? "" : s).trim();
    if (!t || t === "—") return "";
    return t;
  }

  /** 筛选：不区分大小写的子串匹配 */
  function filterHaystackPiece(s) {
    return String(s == null ? "" : s).trim().toLowerCase();
  }

  /** Display-only conversion: preserve partial/unknown dates; validation belongs to the API. */
  function compactCollectionDate(value) {
    return String(value == null ? "" : value).trim().replace(
      /^([0-9Xx?]{4})-([0-9Xx?]{2})-([0-9Xx?]{2})$/, "$1$2$3",
    );
  }

  function displayCollectionDate(value) {
    return compactCollectionDate(value).replace(
      /^([0-9Xx?]{4})([0-9Xx?]{2})([0-9Xx?]{2})$/, "$1-$2-$3",
    );
  }

  function collectionDateFilterHaystack(value) {
    return filterHaystackPiece(displayCollectionDate(value) + " " + compactCollectionDate(value));
  }

  /** 配置里 enum[] 每项 → slug 字符串 */
  function rawEnumOptSlug(entry) {
    if (entry == null) return "";
    if (typeof entry === "object" && !Array.isArray(entry)) {
      if (entry.value != null && String(entry.value).trim() !== "") {
        return String(entry.value).trim();
      }
      return "";
    }
    return String(entry).trim();
  }

  /**
   * 用于筛选下拉的枚举项（若无配置则返回 null → 降级为文本框）。
   * @returns {Array<{value: string, label: string}>}|null
   */
  function enumFilterTuples(enumKey) {
    var opts = browseEnumOptions[enumKey];
    if (!opts || !Array.isArray(opts) || !opts.length) return null;
    var tuples = [];
    var seen = Object.create(null);
    var qi;
    for (qi = 0; qi < opts.length; qi++) {
      var vv = rawEnumOptSlug(opts[qi]);
      if (!vv || seen[vv]) continue;
      seen[vv] = true;
      var disp = browseEnumDisplay(enumKey, vv);
      if (disp === "—") disp = vv;
      tuples.push({ value: vv, label: disp });
    }
    return tuples.length ? tuples : null;
  }

  /** 某格式列下行内出现的压制组 slug（小写可比） */
  function collectGroupsForFmSlug(row, fmWant) {
    var want = fmWant == null ? "" : String(fmWant).trim();
    if (!want) return [];
    var out = [];
    var seen = Object.create(null);
    const ordered = getOrderedTags(row);
    var idxLoop;
    for (idxLoop = 0; idxLoop < ordered.length; idxLoop++) {
      var rawIt = ordered[idxLoop];
      const itObj = rawIt != null && typeof rawIt === "object" ? rawIt : {};
      const pc = pressPairCells(itObj);
      const fmc = pc.fm ? String(pc.fm).trim() : "";
      var gpRi = pc.gp;
      var gpTrim =
        gpRi == null || String(gpRi).trim() === "" ? "" : String(gpRi).trim();
      if (fmc !== want) continue;
      if (!gpTrim) continue;
      var k = filterHaystackPiece(gpTrim);
      if (!k || seen[k]) continue;
      seen[k] = true;
      out.push(k);
    }
    return out;
  }

  function rowSlugSetIntersects(selectedSlugs, rowSlugList) {
    if (!selectedSlugs || !selectedSlugs.length) return false;
    var rk = Object.create(null);
    var ix;
    for (ix = 0; ix < rowSlugList.length; ix++) {
      rk[rowSlugList[ix]] = true;
    }
    var j;
    for (j = 0; j < selectedSlugs.length; j++) {
      var kk = filterHaystackPiece(selectedSlugs[j]);
      if (kk && rk[kk]) return true;
    }
    return false;
  }
  function packSortComparable(pack, key) {
    const r = pack.row;
    const d = r.date || {};
    switch (key) {
      case "domain":
        return (
          nzForSort(browseEnumDisplay("domain", r.domain)) ||
          nzForSort(r.domain)
        ).toLowerCase();
      case "release_type":
        return (
          nzForSort(browseEnumDisplay("release_type", r.release_type)) ||
          nzForSort(r.release_type)
        ).toLowerCase();
      case "date_start":
        return nzForSort(compactCollectionDate(d.start));
      case "date_end":
        return nzForSort(compactCollectionDate(d.end));
      case "country":
        return (
          nzForSort(browseEnumDisplay("country", r.country)) ||
          nzForSort(r.country)
        ).toLowerCase();
      case "name":
        return nzForSort(r.name || "").toLowerCase();
      case "markers": {
        const m = Array.isArray(r.markers) ? r.markers : [];
        const bits = [];
        let j;
        for (j = 0; j < m.length; j++) {
          const mv = typeof m[j] === "string" ? m[j].trim() : "";
          if (!mv) continue;
          bits.push((browseEnumDisplay("markers", mv) || mv).toLowerCase());
        }
        return bits.join("\u0001");
      }
      case SHEET_PRESS_AGG_SORT_KEY:
        return sortComparableAllPressFormats(r, sheetRenderFmColsRef);
      default:
        if (
          typeof key === "string" &&
          key.indexOf(FMT_SORT_PREFIX) === 0 &&
          key.length > FMT_SORT_PREFIX.length
        ) {
          var fmKey = decodeURIComponent(
            key.slice(FMT_SORT_PREFIX.length),
          );
          return sortComparablePressFormatGroups(r, fmKey);
        }
        return "";
    }
  }

  function isUnsavedNewRow(row) {
    return !!(row && row._isNew && !row._persistedPendingRefresh);
  }

  /** 草稿先按新增顺序置顶；普通行再使用当前列的排序规则。 */
  function compareFlatPack(a, b) {
    var aDraft = isUnsavedNewRow(a.row);
    var bDraft = isUnsavedNewRow(b.row);
    if (aDraft !== bDraft) return aDraft ? -1 : 1;
    if (aDraft) return (Number(b.row._newRowOrder) || 0) - (Number(a.row._newRowOrder) || 0);
    if (!sheetSortKey) {
      var rya = nzForSort(String(a.row.yaml_source_rel || "")).toLowerCase();
      var ryb = nzForSort(String(b.row.yaml_source_rel || "")).toLowerCase();
      var lrRel = rya.localeCompare(ryb, undefined, {
        numeric: true,
        sensitivity: "base",
      });
      if (lrRel !== 0) return lrRel;
      return (
        (a.row.index_in_file || 0) - (b.row.index_in_file || 0)
      );
    }
    const key = sheetSortKey;
    const va = packSortComparable(a, key);
    const vb = packSortComparable(b, key);
    const sa = nzForSort(String(va));
    const sb = nzForSort(String(vb));
    var ea = !sa;
    var eb = !sb;
    if (ea && eb) return 0;
    if (ea) return 1 * sheetSortDir;
    if (eb) return -1 * sheetSortDir;
    return (
      String(sa).localeCompare(String(sb), undefined, {
        numeric: true,
        sensitivity: "base",
      }) * sheetSortDir
    );
  }

  function filterSafeControlName(sortKey) {
    return (
      SHEET_FILTER_NAME_PREFIX +
      "_" +
      String(sortKey).replace(/[^a-zA-Z0-9_.-]/g, "_")
    );
  }

  function filterPopoverIdForSortKey(sortKey) {
    return filterSafeControlName(sortKey) + "_pop";
  }

  function persistedFilterLooksActive(sortKey, isEnum) {
    var pk = persistedSheetFilters[sortKey];
    if (pk == null || String(pk).trim() === "") return false;
    if (isEnum) {
      try {
        var a = JSON.parse(pk);
        return Array.isArray(a) && a.length > 0;
      } catch (e_pf) {
        return false;
      }
    }
    return true;
  }

  function loadSheetManualColWidths() {
    try {
      var raw = window.NimdaCommon.readPreference(LS_KEY_SHEET_COL_WIDTHS);
      var parsed = raw ? JSON.parse(raw) : {};
      var out = Object.create(null);
      if (parsed && typeof parsed === "object") {
        Object.keys(parsed).forEach(function (k) {
          var n = Number(parsed[k]);
          if (Number.isFinite(n) && n >= 36 && n <= 1200) out[k] = n;
        });
      }
      return out;
    } catch (e) {
      return Object.create(null);
    }
  }

  function saveSheetManualColWidths() {
    try {
      window.NimdaCommon.writePreference(
        LS_KEY_SHEET_COL_WIDTHS,
        JSON.stringify(sheetManualColWidths || {}),
      );
    } catch (e) {}
  }

  function sheetColumnKeysForVisiblePress(fmColsVisible) {
    var keys = [
      "domain",
      "release_type",
      "date_start",
      "date_end",
      "country",
      "name",
      "markers",
      SHEET_PRESS_AGG_SORT_KEY,
    ];
    var cols = fmColsVisible || [];
    var i;
    for (i = 0; i < cols.length; i++) {
      keys.push(FMT_SORT_PREFIX + encodeURIComponent(String(cols[i] == null ? "" : cols[i])));
    }
    return keys;
  }

  function renderSheetColGroup(fmColsVisible) {
    var keys = sheetColumnKeysForVisiblePress(fmColsVisible);
    var h = "<colgroup>";
    var i;
    for (i = 0; i < keys.length; i++) {
      h += '<col data-col-key="' + esc(keys[i]) + '" />';
    }
    return h + "</colgroup>";
  }

  function sheetColumnHardMin(key) {
    if (key === SHEET_PRESS_AGG_SORT_KEY) return 118;
    if (key === "name") return 84;
    if (key === "markers") return 54;
    if (key === "date_start" || key === "date_end") return 100;
    if (key === "domain" || key === "release_type" || key === "country") return 54;
    if (typeof key === "string" && key.indexOf(FMT_SORT_PREFIX) === 0) return 62;
    return 48;
  }

  function sheetHeaderNeedCap(key) {
    if (key === "release_type" || key === "markers") return 92;
    if (key === "date_start" || key === "date_end") return 108;
    if (key === "domain" || key === "country") return 70;
    if (key === "name") return 92;
    if (key === SHEET_PRESS_AGG_SORT_KEY) return 150;
    if (typeof key === "string" && key.indexOf(FMT_SORT_PREFIX) === 0) return 96;
    return 92;
  }

  function sheetTableAvailableWidth(table) {
    if (!table) return 0;
    var wrap = table.closest(".sheet-wrap");
    var rect = wrap ? wrap.getBoundingClientRect() : table.getBoundingClientRect();
    var left = rect && Number.isFinite(rect.left) ? Math.max(0, rect.left) : 0;
    var viewport = Math.max(320, document.documentElement.clientWidth || window.innerWidth || 0);
    return Math.max(320, Math.floor(viewport - left - 12));
  }

  function sheetVisibleRows(table) {
    return arrSlice.call(table.querySelectorAll("tbody tr.sheet-row")).filter(function (tr) {
      return tr.style.display !== "none";
    });
  }

  function sheetCellsForColumn(table, key) {
    var out = [];
    var ths = arrSlice.call(table.querySelectorAll('thead th[data-col-key]'));
    var i;
    for (i = 0; i < ths.length; i++) {
      if (ths[i].getAttribute("data-col-key") === key) out.push(ths[i]);
    }
    var rows = sheetVisibleRows(table);
    for (i = 0; i < rows.length; i++) {
      var cells = rows[i].cells || [];
      var j;
      for (j = 0; j < cells.length; j++) {
        if (cells[j].getAttribute("data-col-key") === key) {
          out.push(cells[j]);
          break;
        }
      }
    }
    return out;
  }

  function sheetCellInlineExtra(cell) {
    if (!cell) return 0;
    var cs = window.getComputedStyle(cell);
    function px(name) {
      var n = parseFloat(cs.getPropertyValue(name));
      return Number.isFinite(n) ? n : 0;
    }
    return px("padding-left") + px("padding-right") + px("border-left-width") + px("border-right-width") + 2;
  }

  function sheetMeasureTextNeed(cell, text, preserveSpaces) {
    var raw =
      text == null
        ? ""
        : preserveSpaces
          ? String(text).replace(/[\r\n\t]/g, " ")
          : String(text).replace(/\s+/g, " ").trim();
    if (!String(raw).trim()) return 0;
    if (!sheetMeasureEl) {
      sheetMeasureEl = document.createElement("span");
      sheetMeasureEl.style.position = "fixed";
      sheetMeasureEl.style.left = "-10000px";
      sheetMeasureEl.style.top = "-10000px";
      sheetMeasureEl.style.visibility = "hidden";
      sheetMeasureEl.style.whiteSpace = "pre";
      sheetMeasureEl.style.pointerEvents = "none";
      document.body.appendChild(sheetMeasureEl);
    }
    var cs = window.getComputedStyle(cell);
    sheetMeasureEl.style.font = cs.font;
    sheetMeasureEl.style.letterSpacing = cs.letterSpacing;
    sheetMeasureEl.textContent = raw;
    return Math.ceil(sheetMeasureEl.getBoundingClientRect().width + sheetCellInlineExtra(cell));
  }

  function sheetIsVisibleElement(el) {
    if (!el || el.nodeType !== 1) return false;
    var cs = window.getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden";
  }

  function sheetCssGap(el) {
    if (!el) return 0;
    var cs = window.getComputedStyle(el);
    var raw = cs.columnGap || cs.gap || "0";
    var n = Number(String(raw).replace("px", ""));
    return Number.isFinite(n) ? n : 0;
  }

  function sheetHeaderContentNeed(th) {
    if (!th) return 0;
    var inner = th.querySelector(".sheet-th-combo-inner");
    if (!inner) {
      return sheetMeasureTextNeed(th, th.innerText || th.textContent || "");
    }
    var gap = sheetCssGap(inner);
    var kids = arrSlice.call(inner.children || []);
    var total = sheetCellInlineExtra(th);
    var visibleKids = 0;
    var i;
    for (i = 0; i < kids.length; i++) {
      var child = kids[i];
      if (!sheetIsVisibleElement(child)) continue;
      var txt =
        child.innerText ||
        child.textContent ||
        child.getAttribute("aria-label") ||
        "";
      var part = sheetMeasureTextNeed(child, txt);
      if (!part) part = Math.ceil(child.scrollWidth || 0);
      if (part <= 0) continue;
      if (visibleKids) total += gap;
      total += part;
      visibleKids++;
    }
    return Math.ceil(total + 6);
  }

  function sheetLabelTextForCell(cell, key) {
    if (!cell) return "";
    if (key === "name") {
      return cell.getAttribute("title") || cell.innerText || cell.textContent || "";
    }
    var plain = cell.querySelector(
      ".sheet-plain-scalar, .sheet-plain-val, .cell-empty",
    );
    return plain ? plain.textContent || "" : cell.innerText || cell.textContent || "";
  }

  function sheetTagChildLabel(child) {
    if (!child) return "";
    var label = child.querySelector(
      ".pill-agg-fmt-gp, .pill-fmt-plain-pad, .enum-plain, .sheet-pair-plain, .sheet-plain-val",
    );
    return label ? label.textContent || "" : child.innerText || child.textContent || "";
  }

  function sheetTagRowContentNeed(cell) {
    var row = cell ? cell.querySelector(".tag-row") : null;
    if (!row) return 0;
    var gap = sheetCssGap(row);
    var kids = arrSlice.call(row.children || []);
    var total = sheetCellInlineExtra(cell);
    var visibleKids = 0;
    var i;
    for (i = 0; i < kids.length; i++) {
      var child = kids[i];
      if (!sheetIsVisibleElement(child)) continue;
      var label = sheetTagChildLabel(child);
      var preserve = !!(
        child.classList &&
        (child.classList.contains("pill-equal-pre") ||
          child.querySelector(".pill-equal-pre"))
      );
      var part = Math.ceil(child.getBoundingClientRect().width || 0);
      if (!part) part = sheetMeasureTextNeed(child, label, preserve);
      if (!part && child.tagName && String(child.tagName).toLowerCase() === "button") {
        part = Math.ceil(child.getBoundingClientRect().width || child.scrollWidth || 0);
      }
      if (part <= 0) continue;
      if (visibleKids) total += gap;
      total += part;
      visibleKids++;
    }
    return Math.ceil(total + 2);
  }

  function sheetPressAggregateContentNeed(cell) {
    var row = cell ? cell.querySelector(".tag-row-press-agg") : null;
    if (!row) return 0;
    var gap = sheetCssGap(row);
    var kids = arrSlice.call(row.children || []);
    var total = sheetCellInlineExtra(cell);
    var visibleKids = 0;
    var i;
    for (i = 0; i < kids.length; i++) {
      var child = kids[i];
      if (!sheetIsVisibleElement(child)) continue;
      var part = 0;
      if (child.classList && child.classList.contains("pill-press-agg")) {
        part = Math.ceil(child.getBoundingClientRect().width || 0);
        var labelNode = child.querySelector(".pill-agg-fmt-gp");
        if (!part) {
          part = sheetMeasureTextNeed(
            child,
            labelNode ? labelNode.textContent || "" : child.textContent || "",
            true,
          );
        }
      } else {
        part = Math.ceil(Math.max(child.scrollWidth || 0, child.getBoundingClientRect().width || 0));
      }
      if (part <= 0) continue;
      if (visibleKids) total += gap;
      total += part;
      visibleKids++;
    }
    return Math.ceil(total + 2);
  }

  function sheetColumnContentNeed(table, key) {
    var cells = sheetCellsForColumn(table, key);
    var maxNeed = sheetColumnHardMin(key);
    var i;
    for (i = 0; i < cells.length; i++) {
      var cell = cells[i];
      if (
        key === SHEET_PRESS_AGG_SORT_KEY &&
        cell.tagName &&
        String(cell.tagName).toLowerCase() === "th"
      ) {
        continue;
      }
      var need = 0;
      var tagName = cell.tagName ? String(cell.tagName).toLowerCase() : "";
      if (tagName === "th") {
        need = Math.min(sheetHeaderContentNeed(cell), sheetHeaderNeedCap(key));
      } else if (cell.classList && cell.classList.contains("cell-press-aggregate")) {
        need = sheetPressAggregateContentNeed(cell);
      } else if (cell.querySelector && cell.querySelector(".tag-row")) {
        need = sheetTagRowContentNeed(cell);
      } else {
        var textNeed = sheetMeasureTextNeed(
          cell,
          sheetLabelTextForCell(cell, key),
          key === "name",
        );
        if (textNeed) need = textNeed;
      }
      maxNeed = Math.max(maxNeed, need + 2);
    }
    return maxNeed;
  }

  function applySheetColumnWidths(table, cols) {
    var total = 0;
    var colNodes = arrSlice.call(table.querySelectorAll("col[data-col-key]"));
    var i;
    for (i = 0; i < cols.length; i++) {
      var w = Math.max(sheetColumnHardMin(cols[i].key), Math.round(cols[i].width || 0));
      cols[i].width = w;
      total += w;
      var c;
      for (c = 0; c < colNodes.length; c++) {
        if (colNodes[c].getAttribute("data-col-key") === cols[i].key) {
          colNodes[c].style.width = w + "px";
          break;
        }
      }
    }
    table.classList.add("sheet-cols-managed");
    table.style.width = total + "px";
    table.style.minWidth = total + "px";
  }

  function shrinkSheetColumns(cols, shrinkNeeded, allowManual, useHardMin) {
    var guard = 0;
    while (shrinkNeeded > 0.5 && guard < 80) {
      guard++;
      var candidates = cols
        .filter(function (c) {
          if (c.key === SHEET_PRESS_AGG_SORT_KEY) return false;
          if (!allowManual && c.manual) return false;
          var floor = useHardMin ? c.hardMin : Math.min(c.fitNeed, c.width);
          return c.width - floor > 0.5;
        })
        .sort(function (a, b) {
          var af = useHardMin ? a.hardMin : Math.min(a.fitNeed, a.width);
          var bf = useHardMin ? b.hardMin : Math.min(b.fitNeed, b.width);
          return b.width - bf - (a.width - af);
        });
      if (!candidates.length) break;
      var top = candidates[0];
      var floorTop = useHardMin ? top.hardMin : Math.min(top.fitNeed, top.width);
      var take = Math.min(shrinkNeeded, Math.max(0, top.width - floorTop));
      top.width -= take;
      shrinkNeeded -= take;
    }
    return shrinkNeeded;
  }

  function fitSheetColumnsNow() {
    if (!$view) return;
    var table = $view.querySelector("table.sheet");
    if (!table) return;
    var colNodes = arrSlice.call(table.querySelectorAll("col[data-col-key]"));
    if (!colNodes.length) return;
    table.classList.remove("sheet-cols-managed");
    table.style.width = "";
    table.style.minWidth = "";
    var clearI;
    for (clearI = 0; clearI < colNodes.length; clearI++) {
      colNodes[clearI].style.width = "";
    }
    var cols = colNodes.map(function (col) {
      var key = col.getAttribute("data-col-key") || "";
      var th = table.querySelector('thead th[data-col-key="' + key.replace(/"/g, '\\"') + '"]');
      var measured = th ? sheetHeaderContentNeed(th) : 0;
      var fitNeed = sheetColumnContentNeed(table, key);
      var hardMin = sheetColumnHardMin(key);
      var manual = Object.prototype.hasOwnProperty.call(sheetManualColWidths, key);
      var width = manual ? Number(sheetManualColWidths[key]) : Math.max(measured, fitNeed, hardMin);
      return {
        key: key,
        width: Math.max(hardMin, width),
        fitNeed: Math.max(hardMin, fitNeed),
        hardMin: hardMin,
        manual: manual,
      };
    });
    var total = cols.reduce(function (acc, c) { return acc + c.width; }, 0);
    var available = sheetTableAvailableWidth(table);
    var overflow = Math.max(0, total - available);
    overflow = shrinkSheetColumns(cols, overflow, false, false);
    applySheetColumnWidths(table, cols);
  }

  function scheduleSheetColumnFit() {
    if (sheetColumnFitRaf) return;
    sheetColumnFitRaf = requestAnimationFrame(function () {
      sheetColumnFitRaf = 0;
      fitSheetColumnsNow();
    });
  }

  function bindSheetColumnResizeOnce() {
    if (sheetColumnResizeBound || !$view) return;
    sheetColumnResizeBound = true;
    listen(window, "resize", scheduleSheetColumnFit);
    listen($view, "dblclick", function (ev) {
      var handle = ev.target && ev.target.closest ? ev.target.closest(".sheet-col-resizer") : null;
      if (!handle || !$view.contains(handle)) return;
      ev.preventDefault();
      ev.stopPropagation();
      var th = handle.closest("th[data-col-key]");
      var key = th ? th.getAttribute("data-col-key") : "";
      if (!key) return;
      delete sheetManualColWidths[key];
      saveSheetManualColWidths();
      scheduleSheetColumnFit();
    });
    listen($view, "pointerdown", function (ev) {
      var handle = ev.target && ev.target.closest ? ev.target.closest(".sheet-col-resizer") : null;
      if (!handle || !$view.contains(handle)) return;
      var th = handle.closest("th[data-col-key]");
      var key = th ? th.getAttribute("data-col-key") : "";
      var table = th ? th.closest("table.sheet") : null;
      if (!key || !table) return;
      ev.preventDefault();
      ev.stopPropagation();
      var startX = ev.clientX;
      var startW = Math.ceil(th.getBoundingClientRect().width || sheetColumnHardMin(key));
      var minW = sheetColumnHardMin(key);
      document.body.classList.add("sheet-col-resizing");
      function onMove(moveEv) {
        var next = Math.max(minW, Math.round(startW + moveEv.clientX - startX));
        sheetManualColWidths[key] = next;
        saveSheetManualColWidths();
        fitSheetColumnsNow();
      }
      function onUp() {
        document.body.classList.remove("sheet-col-resizing");
        window.removeEventListener("pointermove", onMove, true);
        window.removeEventListener("pointerup", onUp, true);
      }
      listen(window, "pointermove", onMove, true);
      listen(window, "pointerup", onUp, true);
    });
  }

  function filterPopoverTextBodyHtml(sortKey, placeholder) {
    var raw =
      persistedSheetFilters[sortKey] != null
        ? String(persistedSheetFilters[sortKey])
        : "";
    var safeName = filterSafeControlName(sortKey);
    return (
      '<div class="sheet-filter-popover-body">' +
      '<input type="search" class="sheet-col-filter sheet-col-filter-field sheet-filter-popover-input" data-filter-key="' +
      esc(sortKey) +
      '" name="' +
      esc(safeName) +
      '" placeholder="' +
      esc(placeholder) +
      '" value="' +
      esc(raw) +
      '" autocomplete="off" spellcheck="false" />' +
      "</div>"
    );
  }

  function filterPopoverEnumBodyHtml(sortKey, tuples) {
    var selMap = Object.create(null);
    var ps = persistedSheetFilters[sortKey];
    if (ps) {
      try {
        var arr = JSON.parse(ps);
        if (Array.isArray(arr)) {
          var ui_pb;
          for (ui_pb = 0; ui_pb < arr.length; ui_pb++) {
            selMap[filterHaystackPiece(String(arr[ui_pb]))] = true;
          }
        }
      } catch (e_pb) {}
    }
    var safeNamePb = filterSafeControlName(sortKey);
    var bi =
      '<div class="sheet-filter-popover-body sheet-filter-enum-checks">';
    var qi_pb;
    for (qi_pb = 0; qi_pb < tuples.length; qi_pb++) {
      var tv = tuples[qi_pb].value;
      var sel = selMap[filterHaystackPiece(tv)] ? " checked" : "";
      bi +=
        '<label class="sheet-filter-enum-label">' +
        '<input type="checkbox" class="sheet-enum-filter-cb" name="' +
        esc(safeNamePb) +
        '" data-filter-key="' +
        esc(sortKey) +
        '" value="' +
        esc(tv) +
        '"' +
        sel +
        ' />' +
        "<span>" +
        esc(tuples[qi_pb].label) +
        "</span>" +
        "</label>";
    }
    bi += "</div>";
    return bi;
  }

  /**
   * 排序表头 + 可选「分列▼」（压制汇总列） + 「▼」筛选（枚举为复选，文本列为搜索框）
   * @param {null|{fmCols: Array<string>, visMap: Record<string,true>}} pressSplitPack
   */
  function sortThWithFilter(
    sortKey,
    sectionEnumKeyOrNull,
    fallbackText,
    filterEnumKeyOrNull,
    filterTextPlaceholder,
    pressSplitPack,
  ) {
    pressSplitPack = pressSplitPack != null ? pressSplitPack : null;
    var labelHead =
      sectionEnumKeyOrNull == null
        ? fallbackText
        : enumSectionTh(sectionEnumKeyOrNull, fallbackText);
    var actClassSw = sheetSortKey === sortKey ? " sheet-sort-active" : "";
    var sufSw = "";
    if (sheetSortKey === sortKey) {
      sufSw = sheetSortDir > 0 ? " ▲" : " ▼";
    }
    var popIdSw = filterPopoverIdForSortKey(sortKey);
    var tuplesSw =
      filterEnumKeyOrNull != null && String(filterEnumKeyOrNull).trim() !== ""
        ? enumFilterTuples(filterEnumKeyOrNull)
        : null;
    var hasEnumSw = tuplesSw && tuplesSw.length;
    var innerSw = hasEnumSw
      ? filterPopoverEnumBodyHtml(sortKey, tuplesSw)
      : filterPopoverTextBodyHtml(
          sortKey,
          filterTextPlaceholder ? String(filterTextPlaceholder) : "…",
        );
    var hintTxtSw = "";
    var trigClsSw = "sheet-filter-trigger";
    if (persistedFilterLooksActive(sortKey, !!hasEnumSw)) {
      trigClsSw += " sheet-filter-trigger-active";
    }

    var splitBtnHtml = "";
    var splitPopHtml = "";
    if (
      pressSplitPack &&
      pressSplitPack.fmCols &&
      pressSplitPack.fmCols.length &&
      pressSplitPack.visMap
    ) {
      var splitPopIdRaw = filterPopoverIdForSortKey(sortKey) + "_split";
      var splitTrigCls = "sheet-filter-trigger sheet-press-split-trigger";
      if (pressSplitAnyVisibleForCols(pressSplitPack.fmCols, pressSplitPack.visMap))
        splitTrigCls += " sheet-filter-trigger-active";
      splitBtnHtml =
        '<button type="button" class="' +
        splitTrigCls +
        '" aria-expanded="false" aria-haspopup="true" aria-controls="' +
        esc(splitPopIdRaw) +
        '" aria-label="' +
        esc("分列") +
        '">' +
        esc("分列▼") +
        "</button>";
      splitPopHtml =
        '<div id="' +
        esc(splitPopIdRaw) +
        '" class="sheet-filter-popover sheet-press-split-popover" hidden role="dialog" aria-label="' +
        esc("分列") +
        '">' +
        pressFmtSplitChecksInnerHtml(
          pressSplitPack.fmCols,
          pressSplitPack.visMap,
        ) +
        "</div>";
    }

    var filtHintBlk =
      String(hintTxtSw || "").trim() === ""
        ? ""
        : '<p class="sheet-filter-popover-hint">' + esc(hintTxtSw) + "</p>";

    return (
      '<th scope="col" class="sheet-sort-col sheet-th-combo" data-col-key="' +
      esc(sortKey) +
      '">' +
      '<div class="sheet-th-combo-inner">' +
      '<span class="sheet-th-sort sheet-th-sortable' +
      actClassSw +
      '" data-sort="' +
      esc(sortKey) +
      '" tabindex="0">' +
      esc(labelHead + sufSw) +
      "</span>" +
      splitBtnHtml +
      '<button type="button" class="' +
      esc(trigClsSw) +
      '" aria-expanded="false" aria-haspopup="true" aria-controls="' +
      esc(popIdSw) +
      '" aria-label="' +
      esc(labelHead) +
      '">' +
      esc("▼") +
      "</button>" +
      "</div>" +
      splitPopHtml +
      '<div id="' +
      esc(popIdSw) +
      '" class="sheet-filter-popover" hidden role="dialog" aria-label="' +
      esc(labelHead) +
      '">' +
      filtHintBlk +
      innerSw +
      "</div>" +
      '<span class="sheet-col-resizer" role="separator" aria-hidden="true" title="拖动调整列宽，双击恢复自动宽度"></span>' +
      "</th>"
    );
  }

  function stripSheetFilterDropdownThLift() {
    if (!$view) return;
    arrSlice
      .call($view.querySelectorAll("th.sheet-th-dropdown-open"))
      .forEach(function (thEl) {
        thEl.classList.remove("sheet-th-dropdown-open");
      });
  }

  function stripSheetFilterPopoverPlacement(popEl) {
    if (!popEl || !popEl.style) return;
    var keys = ["position","left","top","right","bottom","visibility","transform","maxWidth","width"];
    var ki;
    for (ki = 0; ki < keys.length; ki++) popEl.style[keys[ki]] = "";
  }

  /**
   * 筛选下拉：水平以「▼」按钮中心对齐（正下方略偏即视觉在箭头下），
   * 仅当会超出视口左右边时做夹紧微调，不再向屏幕正中拉拢。
   */
  function positionSheetFilterPopover(trigEl, popEl) {
    if (!trigEl || !popEl) return;
    var margin = 10;
    var vpW = window.innerWidth || 640;
    var vpH = window.innerHeight || 480;
    /** 触发元素须为 .sheet-filter-trigger，使面板顶边从箭头下缘起算 */
    var rect = trigEl.getBoundingClientRect();

    popEl.style.position = "fixed";
    popEl.style.right = "auto";
    popEl.style.bottom = "auto";
    popEl.style.transform = "none";

    popEl.style.visibility = "hidden";
    popEl.style.left = "-10000px";
    popEl.style.top = "-10000px";

    popEl.style.width = "";
    popEl.style.maxWidth = vpW - margin * 2 + "px";

    void popEl.offsetWidth;
    void popEl.offsetHeight;
    var pw = popEl.offsetWidth;
    var ph = popEl.offsetHeight;

    var trigCx = rect.left + rect.width / 2;
    var left = trigCx - pw / 2;
    if (left < margin) left = margin;
    if (left + pw > vpW - margin) left = vpW - margin - pw;

    var gap = 5;
    var topBelow = rect.bottom + gap;
    var belowFits = topBelow + ph <= vpH - margin;
    var topAbove = rect.top - gap - ph;
    var aboveFits = topAbove >= margin;
    var top;
    if (belowFits) {
      top = topBelow;
    } else if (aboveFits) {
      top = topAbove;
    } else {
      top = topBelow;
      if (top + ph > vpH - margin) top = vpH - margin - ph;
      if (top < margin) top = margin;
    }

    popEl.style.left = Math.round(left) + "px";
    popEl.style.top = Math.round(top) + "px";
    popEl.style.visibility = "";
  }

  function repositionOpenSheetFilterPopover() {
    if (!$view) return;
    arrSlice
      .call($view.querySelectorAll(".sheet-filter-trigger[aria-expanded='true']"))
      .forEach(function (btn) {
        var pid = btn.getAttribute("aria-controls");
        var popFound = pid ? document.getElementById(pid) : null;
        if (
          popFound &&
          !popFound.hidden &&
          $view.contains(btn)
        )
          positionSheetFilterPopover(btn, popFound);
      });
  }

  var sheetFilterReposRaf = 0;

  function scheduleSheetFilterPopoverRepos() {
    if (!$view) return;
    if (sheetFilterReposRaf) return;
    sheetFilterReposRaf = requestAnimationFrame(function () {
      sheetFilterReposRaf = 0;
      repositionOpenSheetFilterPopover();
    });
  }

  function closeAllSheetFilterPopovers() {
    if (!$view) return;
    stripSheetFilterDropdownThLift();
    arrSlice
      .call($view.querySelectorAll(".sheet-filter-popover"))
      .forEach(function (pEl) {
        stripSheetFilterPopoverPlacement(pEl);
        pEl.hidden = true;
      });
    arrSlice
      .call($view.querySelectorAll(".sheet-filter-trigger"))
      .forEach(function (bEl) {
        bEl.setAttribute("aria-expanded", "false");
      });
  }

  function bindSheetFilterPopoversOnce() {
    if (sheetFilterPopoversBound) return;
    sheetFilterPopoversBound = true;
    listen($view, "click", function (ev) {
      var trig = ev.target.closest(".sheet-filter-trigger");
      if (!trig || !$view.contains(trig)) return;
      ev.stopPropagation();
      var pid = trig.getAttribute("aria-controls");
      var pop = pid ? document.getElementById(pid) : null;
      var wasOpen = !!(pop && !pop.hidden);
      arrSlice
        .call($view.querySelectorAll(".sheet-filter-popover"))
        .forEach(function (pEl) {
          stripSheetFilterPopoverPlacement(pEl);
          pEl.hidden = true;
        });
      arrSlice
        .call($view.querySelectorAll(".sheet-filter-trigger"))
        .forEach(function (bEl) {
          bEl.setAttribute("aria-expanded", "false");
        });
      stripSheetFilterDropdownThLift();
      if (pop && !wasOpen) {
        var thLift = trig.closest("th.sheet-th-combo");
        if (thLift) {
          thLift.classList.add("sheet-th-dropdown-open");
        }
        pop.hidden = false;
        requestAnimationFrame(function () {
          if (!pop.hidden && document.getElementById(pid || "") === pop) {
            positionSheetFilterPopover(trig, pop);
          }
        });
        trig.setAttribute("aria-expanded", "true");
      }
    });
    listen(window, "resize", scheduleSheetFilterPopoverRepos);
    listen(window,
      "scroll",
      scheduleSheetFilterPopoverRepos,
      true,
    );
    listen(document, "click", function (evDoc) {
      if (!$view.contains(evDoc.target)) {
        closeAllSheetFilterPopovers();
        return;
      }
      if (
        evDoc.target.closest(".sheet-filter-popover") ||
        evDoc.target.closest(".sheet-filter-trigger")
      ) {
        return;
      }
      closeAllSheetFilterPopovers();
    });
    listen(document, "keydown", function (evK) {
      if (evK.key !== "Escape") return;
      closeAllSheetFilterPopovers();
    });
  }

  function bindSheetSortingOnce() {
    if (sheetSortEventsBound) return;
    sheetSortEventsBound = true;
    listen($view, "click", function (ev) {
      var sortEl = ev.target.closest(".sheet-th-sort");
      if (!sortEl || !$view.contains(sortEl)) return;
      var skSort = sortEl.getAttribute("data-sort");
      if (!skSort) return;
      if (sheetSortKey === skSort) {
        sheetSortDir = -sheetSortDir;
      } else {
        sheetSortKey = skSort;
        sheetSortDir = 1;
      }
      if (lastBrowsePayload && lastBrowsePayload.ok) {
        renderPayload(lastBrowsePayload, true);
      }
    });
    listen($view, "keydown", function (ev) {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      var sortElK = ev.target.closest(".sheet-th-sort");
      if (!sortElK || !$view.contains(sortElK)) return;
      ev.preventDefault();
      sortElK.click();
    });
  }

  /** 读出格式/组；非标量转为字符串（避免 typeof !== "string" 导致整列为空）。 */
  function pressPairCells(it) {
    if (!it || typeof it !== "object") {
      return { fm: "", gp: "" };
    }
    function pick(field) {
      var v = it[field];
      if (v == null || String(v).trim() === "") return "";
      return typeof v === "string" ? v.trim() : String(v).trim();
    }
    return { fm: pick(FMT), gp: enumValueForUi(GRP, pick(GRP)) };
  }

  function setStatus(msg, isErr) {
    context.setStatus(msg, isErr);
  }

  function esc(s) {
    return window.NimdaCommon.escapeHtml(s);
  }

  /** name / date / 作品名等在编辑模式下的输入框 */
  function renderSheetScalarField(fieldKey, rawVal, rowIndexNum, yamlRelEsc, displayLabel) {
    yamlRelEsc = yamlRelEsc == null ? "" : String(yamlRelEsc);
    var cur = rawVal == null ? "" : String(rawVal);
    if (fieldKey === "date_start" || fieldKey === "date_end") {
      cur = displayCollectionDate(cur);
    }
    var sid = esc(String(Number(rowIndexNum)));
    if (!sheetEditMode) {
      return '<span class="sheet-plain-val sheet-plain-scalar">' + esc(cur) + "</span>";
    }
    return (
      '<span class="sheet-plain-val sheet-plain-scalar sheet-edit-trigger" tabindex="0" role="button" data-action="inline-edit-scalar" data-sheet-iif="' +
      sid +
      '" data-yaml-rel="' +
      yamlRelEsc +
      '" data-field="' +
      esc(fieldKey) +
      '" data-current-value="' +
      esc(cur) +
      '">' +
      esc(displayLabel == null ? cur : displayLabel) +
      "</span>"
    );
  }

  function renderWorkNameField(row, yamlRelEsc) {
    var title = String(row.name || "未命名作品");
    var h = '<button type="button" class="sheet-work-name-link" data-action="work-details" data-sheet-iif="' +
      esc(String(Number(row.index_in_file))) + '" data-yaml-rel="' + yamlRelEsc +
      '" title="点击查看完整作品信息">' + esc(title) + '</button>';
    if (sheetEditMode) {
      h += '<span class="sheet-work-name-edit">' +
        renderSheetScalarField("name", row.name || "", row.index_in_file, yamlRelEsc, "编辑") + '</span>';
      h = '<span class="sheet-work-name-content">' + h + '</span>';
    }
    return h;
  }

  function workDetailDraftRecord(row) {
    var collection = { domain: row.domain || "", release_type: row.release_type || "",
      path: row.path || "", markers: (row.markers || []).slice(), collectioned: [] };
    var continuations = new Map();
    getOrderedTags(row).forEach(function (item) {
      var press = JSON.parse(JSON.stringify(item));
      delete press.segment;
      delete press.continuation_index;
      delete press.continuation_title;
      if (item.segment !== "continuation") {
        collection.collectioned.push(press);
        return;
      }
      var key = item.continuation_index == null ? 0 : item.continuation_index;
      if (!continuations.has(key)) {
        var block = { collectioned: [] };
        if (item.continuation_title) block.title = item.continuation_title;
        continuations.set(key, block);
      }
      continuations.get(key).collectioned.push(press);
    });
    if (continuations.size) collection.continuations = Array.from(continuations.values());
    return { attributes: [
      { type: "date", data: { start: compactCollectionDate((row.date || {}).start), end: compactCollectionDate((row.date || {}).end) } },
      { type: "collection-type", data: collection },
      { type: "country", data: row.country || "" },
      { type: "name", data: row.name || "" },
    ] };
  }

  function sourceHashForRow(row) {
    if (!row || row._isNew || !lastBrowsePayload) return "";
    var sources = Array.isArray(lastBrowsePayload.sources_loaded) ? lastBrowsePayload.sources_loaded : [];
    var source = sources.find(function (item) {
      return item && normalizedCatalogRelKey(item.relpath || "") === normalizedCatalogRelKey(row.yaml_source_rel || "");
    });
    return source && typeof source.sha256 === "string" ? source.sha256.trim().toLowerCase() : "";
  }

  function openWorkDetails(yamlRel, indexInFile) {
    var hit = findPayloadRow(yamlRel, indexInFile);
    if (!hit || !Number.isInteger(Number(indexInFile))) return false;
    if (!window.NimdaWorkDetailDialog) {
      setStatus("作品信息页面尚未加载，请刷新应用后重试。", true);
      return false;
    }
    var row = JSON.parse(JSON.stringify(hit.row));
    var sourceHash = sourceHashForRow(row);
    var uploaded = !row._isNew && row.source_record && typeof row.source_record === "object";
    var sourceLabel = row._isNew ? "当前新增草稿 · 尚未写入数据库" :
      (row.yaml_source_rel || "上传记录") + " · 第 " + (Number(row.index_in_file) + 1) + " 条";
    var notice = row._isNew ? "这里展示当前新增草稿，不代表已保存的数据库记录。" :
      uploaded ? "只读查看上传文件中的完整记录，不会写入数据库。" :
        "只读查看数据库已保存的完整记录；列表中尚未保存的修改不会计入本页。";
    window.NimdaWorkDetailDialog.open({
      title: row.name || "未命名作品", sourceLabel: sourceLabel, notice: notice,
      enumLabel: browseEnumDisplay,
      loadRecord: async function () {
        if (row._persistedPendingRefresh) throw new Error("作品已经保存，请先加载 DB 数据刷新列表后再查看完整记录。");
        if (row._isNew) return { record: workDetailDraftRecord(row) };
        if (uploaded) return { record: row.source_record };
        if (!sourceHash) throw new Error("当前列表缺少来源版本信息，请重新加载 DB 数据后再查看作品详情。");
        var out = await fetchJson("/api/collection-detail/work/detail", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ yaml_source_rel: row.yaml_source_rel,
            index_in_file: Number(row.index_in_file), source_sha256: sourceHash }),
        });
        if (!out.res.ok || !out.data || out.data.ok !== true || !out.data.record) {
          throw new Error((out.data && out.data.error) || "读取完整作品记录失败，请稍后重试。");
        }
        return { record: out.data.record, source: out.data.source };
      },
    });
    return true;
  }

  /** GET /api/config 字段 ``enum_options``：配置文件 ``enum[].name`` → 允许取值 */
  var browseEnumOptions = {};
  /** GET /api/config ``enum_labels``：name → { 取值 slug: ``desc``/``description`` 等展示的文案 } */
  var browseEnumLabels = {};
  /** GET /api/config ``enum_section_labels``：枚举块顶层 ``desc`` 等 → 表格列头等 */
  var browseEnumSectionLabels = {};

  function setBrowseEnumOptions(map) {
    browseEnumOptions = map && typeof map === "object" ? map : {};
  }

  function setBrowseEnumLabels(map) {
    browseEnumLabels = map && typeof map === "object" ? map : {};
  }

  function setBrowseEnumSectionLabels(map) {
    browseEnumSectionLabels = map && typeof map === "object" ? map : {};
  }

  /** 使用配置里 enum 块顶层 ``desc`` 等；无时回退 fallback */
  function enumSectionTh(enumKey, fallback) {
    var s =
      browseEnumSectionLabels &&
      Object.prototype.hasOwnProperty.call(browseEnumSectionLabels, enumKey)
        ? browseEnumSectionLabels[enumKey]
        : null;
    if (s != null && String(s).trim() !== "") {
      return String(s).trim();
    }
    return fallback;
  }

  /** 表格展示用可读文案（无映射时仍为 YAML 取值） */
  function browseEnumDisplay(enumKey, rawVal) {
    var v = enumValueForUi(enumKey, rawVal);
    if (!v) {
      return "—";
    }
    var row = browseEnumLabels[enumKey];
    if (
      row &&
      typeof row === "object" &&
      Object.prototype.hasOwnProperty.call(row, v)
    ) {
      var t = row[v];
      if (t != null && String(t).trim() !== "") {
        return String(t).trim();
      }
    }
    return v;
  }

  /** 非编辑模式：纯文本；编辑模式：下拉（表格内普通下拉见 browse.css 与各主题 ``--select-*``） */
  function renderEnumSelect(enumKey, cur, clsFragment, extraAttrs) {
    extraAttrs = extraAttrs || "";
    var opts = browseEnumOptions[enumKey];
    cur = cur == null ? "" : String(cur).trim();
    var clsCombined = clsFragment ? "sheet-select " + clsFragment : "sheet-select";

    if (!sheetEditMode) {
      if (!cur) {
        return '<span class="cell-empty">—</span>';
      }
      var plainCls = "sheet-plain-val";
      if (clsFragment && clsFragment.indexOf("sheet-select-pair") !== -1) {
        plainCls += " sheet-pair-plain";
      }
      var show = browseEnumDisplay(enumKey, cur);
      return '<span class="' + plainCls + '">' + esc(show) + "</span>";
    }

    var editPlainCls = "sheet-plain-val sheet-edit-trigger";
    if (clsFragment && clsFragment.indexOf("sheet-select-pair") !== -1) {
      editPlainCls += " sheet-pair-plain";
    }
    if (!cur) editPlainCls += " cell-empty";
    var editShow = browseEnumDisplay(enumKey, cur);
    return (
      '<span class="' +
      editPlainCls +
      '" tabindex="0" role="button" data-action="inline-edit-enum" data-enum-key="' +
      esc(enumKey) +
      '" data-current-value="' +
      esc(cur) +
      '" ' +
      extraAttrs +
      ">" +
      esc(editShow) +
      "</span>"
    );

    if (!opts || !Array.isArray(opts) || opts.length === 0) {
      return (
        '<span class="enum-plain' +
        (clsFragment ? " " + clsFragment : "") +
        '">' +
        esc(browseEnumDisplay(enumKey, cur)) +
        "</span>"
      );
    }
    var vals = opts.slice();
    var seen = {};
    var qi;
    for (qi = 0; qi < vals.length; qi++) seen[String(vals[qi])] = true;
    if (cur && !seen[cur]) vals.unshift(cur);
    var bh =
      '<select class="' +
      esc(clsCombined) +
      '" data-enum-key="' +
      esc(enumKey) +
      '" ' +
      extraAttrs +
      ">";
    for (qi = 0; qi < vals.length; qi++) {
      var v = String(vals[qi]);
      bh +=
        '<option value="' +
        esc(v) +
        '"' +
        (v === cur ? " selected" : "") +
        ">" +
        esc(browseEnumDisplay(enumKey, v)) +
        "</option>";
    }
    bh += "</select>";
    return bh;
  }

  function sheetRowKey(yamlRel, indexInFile) {
    return normalizedCatalogRelKey(yamlRel) + "#" + String(Number(indexInFile));
  }

  function defaultEnumValue(enumKey, fallback) {
    var opts = browseEnumOptions[enumKey];
    if (Array.isArray(opts)) {
      var i;
      for (i = 0; i < opts.length; i++) {
        var v = rawEnumOptSlug(opts[i]);
        if (v) return v;
      }
    }
    return fallback || "";
  }

  function currentYearCatalogRelForNewRow(patch) {
    patch = patch || {};
    var country = String(patch.country || defaultEnumValue("country", "japan")).trim().toLowerCase();
    var codes = { japan: "JP", korea: "KR", china: "CN", usa: "US", uk: "UK" };
    if (!codes[country]) throw new Error("新增作品暂不支持国家代码：" + country);
    var start = compactCollectionDate((patch.date || {}).start || patch.start);
    var year = /^\d{4}/.test(start) && start.slice(0, 4) !== "0000" ? start.slice(0, 4) : String(new Date().getFullYear());
    return "[" + codes[country] + "][TVInfo][" + year + "].yaml";
  }

  function hasWritableDbConfigForNewRow() {
    var paths = lastServerConfig && lastServerConfig.paths ? lastServerConfig.paths : null;
    return !!(paths && typeof paths.filesystem_root === "string" && paths.filesystem_root.trim());
  }

  function nextSyntheticIndexForRel(yamlRel) {
    var keyRel = normalizedCatalogRelKey(yamlRel);
    var maxIx = -1;
    if (lastBrowsePayload && lastBrowsePayload.profile_groups) {
      var groups = lastBrowsePayload.profile_groups || [];
      var gi;
      for (gi = 0; gi < groups.length; gi++) {
        var rows = groups[gi].rows || [];
        var ri;
        for (ri = 0; ri < rows.length; ri++) {
          var r = rows[ri];
          if (normalizedCatalogRelKey(r.yaml_source_rel || "") !== keyRel) continue;
          var n = Number(r.index_in_file);
          if (Number.isFinite(n)) maxIx = Math.max(maxIx, n);
        }
      }
    }
    return maxIx + 1;
  }

  function profileKeyForPayloadRow(row) {
    return String(row.domain || "").trim() + "-" + String(row.release_type || "").trim();
  }

  function findOrCreateProfileGroupForRow(row) {
    if (!lastBrowsePayload.profile_groups) lastBrowsePayload.profile_groups = [];
    var key = profileKeyForPayloadRow(row);
    var groups = lastBrowsePayload.profile_groups;
    var gi;
    for (gi = 0; gi < groups.length; gi++) {
      if (String(groups[gi].profile_key || "") === key) return groups[gi];
    }
    var g = {
      profile_key: key,
      registered_profile: false,
      profile_label: "新增 · " + key,
      presenter_key: "_fallback",
      rows: [],
    };
    groups.push(g);
    return g;
  }

  function ensurePayloadForNewRow(patch) {
    if (
      lastBrowsePayload &&
      lastBrowsePayload.ok &&
      lastBrowsePayload.save &&
      lastBrowsePayload.save.enabled
    ) {
      return true;
    }
    if (!hasWritableDbConfigForNewRow()) return false;
    var rel = currentYearCatalogRelForNewRow(patch);
    lastBrowsePayload = {
      ok: true,
      filename: "新增行",
      profile_groups: [],
      counts_by_profile: {},
      sources_loaded: [{ relpath: rel, count: 0 }],
      total: 0,
      save: {
        enabled: true,
        multi_file: false,
        target_path: rel,
        target_paths: [rel],
        history_hint: "",
        help: "新增作品按国家和开播年份归档；开播年份未知时使用当前年份。",
      },
    };
    lastDbCatalogLoadedPaths = null;
    deletedSheetRows = Object.create(null);
    return true;
  }

  function addPayloadRow(patch) {
    if (!patch || !String(patch.name || "").trim()) {
      throw new Error("请填写作品名称，不能添加空白行。");
    }
    var rel = currentYearCatalogRelForNewRow(patch);
    if (!ensurePayloadForNewRow(patch)) return false;
    var row = {
      _isNew: true,
      _newRowOrder: ++newSheetRowSequence,
      index_in_file: nextSyntheticIndexForRel(rel),
      yaml_source_rel: rel,
      domain: String(patch.domain || defaultEnumValue("domain", "animation")),
      release_type: String(patch.release_type || defaultEnumValue("release_type", "tv")),
      country: String(patch.country || defaultEnumValue("country", "japan")),
      name: String(patch.name).trim(),
      path: String(patch.path || "").trim(),
      date: { start: compactCollectionDate((patch.date || {}).start), end: compactCollectionDate((patch.date || {}).end) },
      markers: Array.isArray(patch.markers) ? patch.markers.slice() : [],
      collectioned_ordered: JSON.parse(JSON.stringify(patch.collectioned_ordered || [])),
    };
    var group = findOrCreateProfileGroupForRow(row);
    if (!Array.isArray(group.rows)) group.rows = [];
    group.rows.push(row);
    if (typeof lastBrowsePayload.total === "number") lastBrowsePayload.total += 1;
    return rel;
  }

  function findPayloadRow(yamlRel, indexInFile) {
    if (!lastBrowsePayload || !lastBrowsePayload.profile_groups) return null;
    var keyRel = normalizedCatalogRelKey(yamlRel);
    var idx = Number(indexInFile);
    var groups = lastBrowsePayload.profile_groups || [];
    var gi;
    for (gi = 0; gi < groups.length; gi++) {
      var rows = groups[gi].rows || [];
      var ri;
      for (ri = 0; ri < rows.length; ri++) {
        var r = rows[ri];
        if (
          Number(r.index_in_file) === idx &&
          normalizedCatalogRelKey(r.yaml_source_rel || "") === keyRel
        ) {
          return { group: groups[gi], rows: rows, row: r, rowIndex: ri };
        }
      }
    }
    return null;
  }

  function renderPressItemDeleteButton(rowIndexNum, yamlRelEsc, sourceOrd) {
    if (!sheetEditMode) return "";
    return (
      '<button type="button" class="press-item-delete-btn" title="删除此压制项" aria-label="删除此压制项" data-action="delete-press-item" data-sheet-iif="' +
      esc(String(Number(rowIndexNum))) +
      '" data-yaml-rel="' +
      yamlRelEsc +
      '" data-source-ord="' +
      esc(String(sourceOrd)) +
      '">×</button>'
    );
  }

  function trimPathValue(v) {
    return v == null ? "" : String(v).trim();
  }

  function joinPressTargetForUi(workPath, pressPath) {
    var work = trimPathValue(workPath);
    var press = trimPathValue(pressPath);
    if (!work) return press;
    if (!press) return work;
    return work.replace(/[\\\/]+$/g, "") + "/" + press.replace(/^[\\\/]+/g, "");
  }

  function pressConnectionState(row, item) {
    var workPath = trimPathValue(row && row.path);
    var pressPath = trimPathValue(item && item.press_path);
    if (workPath && pressPath) return "configured";
    if (workPath || pressPath) return "partial";
    return "empty";
  }

  function pressConnectionClass(row, item) {
    return " press-link-" + pressConnectionState(row, item);
  }

  function pressConnectionTooltipText(row, item) {
    var workPath = trimPathValue(row && row.path);
    var pressPath = trimPathValue(item && item.press_path);
    if (workPath && pressPath) {
      return "连接：" + joinPressTargetForUi(workPath, pressPath);
    }
    if (workPath || pressPath) {
      return (
        "连接配置不完整：" +
        (workPath ? "作品父路径已配置" : "作品父路径未配置") +
        " / " +
        (pressPath ? "压制路径已配置" : "压制路径未配置")
      );
    }
    return "无连接";
  }

  function renderPressOpenFolderButton(row, item) {
    var workPath = trimPathValue(row && row.path);
    var pressPath = trimPathValue(item && item.press_path);
    if (!workPath || !pressPath) return "";
    var targetLabel = joinPressTargetForUi(workPath, pressPath);
    return (
      '<button type="button" class="press-open-folder-btn" data-action="open-press-folder" data-work-path="' +
      esc(workPath) +
      '" data-press-path="' +
      esc(pressPath) +
      '" title="' +
      esc("打开连接目录：" + targetLabel) +
      '" aria-label="打开连接目录">打开</button>'
    );
  }

  function pressItemEditAttrs(rowIndexNum, yamlRelEsc, sourceOrd, mode, lockedFmt) {
    if (!sheetEditMode) return "";
    return (
      ' data-action="edit-press-item" data-edit-mode="' +
      esc(mode || "full") +
      '" data-sheet-iif="' +
      esc(String(Number(rowIndexNum))) +
      '" data-yaml-rel="' +
      yamlRelEsc +
      '" data-source-ord="' +
      esc(String(sourceOrd)) +
      '"' +
      (lockedFmt ? ' data-press-fmt="' + esc(lockedFmt) + '"' : "")
    );
  }

  function renderPressAddButton(rowIndexNum, yamlRelEsc) {
    if (!sheetEditMode) return "";
    return (
      '<button type="button" class="press-add-btn" title="新增压制信息" aria-label="新增压制信息" data-action="add-press-item" data-sheet-iif="' +
      esc(String(Number(rowIndexNum))) +
      '" data-yaml-rel="' +
      yamlRelEsc +
      '">+</button>'
    );
  }

  function normalizePressRelPathForUi(path) {
    return String(path == null ? "" : path)
      .trim()
      .replace(/\\/g, "/")
      .replace(/^\/+|\/+$/g, "");
  }

  /** Legacy empty-group placeholders and new empty strings share one UI value; source data stays unchanged. */
  function enumValueForUi(enumKey, value) {
    var next = value == null ? "" : String(value).trim();
    return enumKey === GRP && next === "----" ? "" : next;
  }

  function enumValueTuplesForEditor(enumKey, currentValue) {
    var opts = browseEnumOptions[enumKey];
    // A group is optional; the placeholder is a UI choice, not a registered group.
    var out = enumKey === GRP ? [{ value: "", label: browseEnumDisplay(GRP, "") }] : [];
    var seen = Object.create(null);
    var i;
    if (Array.isArray(opts)) {
      for (i = 0; i < opts.length; i++) {
        var v = enumValueForUi(enumKey, rawEnumOptSlug(opts[i]));
        if (!v || seen[v]) continue;
        seen[v] = true;
        out.push({ value: v, label: browseEnumDisplay(enumKey, v) });
      }
    }
    var cur = enumValueForUi(enumKey, currentValue);
    if (cur && !seen[cur]) {
      out.splice(enumKey === GRP ? 1 : 0, 0, { value: cur, label: browseEnumDisplay(enumKey, cur) });
    }
    return out;
  }

  function enumSelectForPressEditor(enumKey, currentValue, id, disabled) {
    var cur = enumValueForUi(enumKey, currentValue);
    var vals = enumValueTuplesForEditor(enumKey, cur);
    if (enumKey !== GRP && !cur && vals.length) cur = vals[0].value;
    var h =
      '<select id="' +
      esc(id) +
      '" class="press-editor-select"' +
      (disabled ? " disabled" : "") +
      ">";
    var i;
    for (i = 0; i < vals.length; i++) {
      h +=
        '<option value="' +
        esc(vals[i].value) +
        '"' +
        (vals[i].value === cur ? " selected" : "") +
        ">" +
        esc(vals[i].label) +
        "</option>";
    }
    if (!vals.length) {
      h += '<option value=""></option>';
    }
    h += "</select>";
    return h;
  }

  function closePressEditor() {
    var old = document.getElementById("press-editor-modal");
    if (old && old.parentNode) old.parentNode.removeChild(old);
  }

  function openPressItemEditor(yamlRel, indexInFile, sourceOrd, mode, lockedFmt) {
    if (!sheetEditMode) return false;
    var hit = findPayloadRow(yamlRel, indexInFile);
    if (!hit) return false;
    var isAdd = mode === "add";
    var isSplit = mode === "split";
    var ord = Number(sourceOrd);
    var ordered;
    try {
      ordered = JSON.parse(JSON.stringify(getOrderedTags(hit.row)));
    } catch (_unused) {
      ordered = [];
    }
    if (!isAdd && (!Number.isFinite(ord) || ord < 0 || !ordered[ord])) return false;
    var item = isAdd ? {} : ordered[ord];
    var curFmt = lockedFmt || item[FMT] || defaultEnumValue(FMT, "");
    var curGrp = enumValueForUi(GRP, item[GRP]);
    var curWorkPath = typeof hit.row.path === "string" ? hit.row.path : "";
    var curPressPath = typeof item.press_path === "string" ? item.press_path : "";

    closePressEditor();
    var modal = document.createElement("div");
    modal.id = "press-editor-modal";
    modal.className = "press-editor-backdrop";
    modal.innerHTML =
      '<div class="press-editor-panel" role="dialog" aria-modal="true" aria-label="压制信息编辑">' +
      '<div class="press-editor-head">' +
      '<strong>' +
      esc(isAdd ? "新增压制信息" : "编辑压制信息") +
      "</strong>" +
      '<button type="button" class="press-editor-close" data-press-editor-action="cancel" aria-label="关闭">×</button>' +
      "</div>" +
      '<div class="press-editor-grid">' +
      '<label><span>压制格式</span>' +
      enumSelectForPressEditor(FMT, curFmt, "press-editor-fmt", isSplit) +
      "</label>" +
      '<label><span>压制组</span>' +
      enumSelectForPressEditor(GRP, curGrp, "press-editor-grp", false) +
      "</label>" +
      '<label class="press-editor-wide"><span>作品父路径</span><input id="press-editor-work-path" type="text" value="' +
      esc(curWorkPath) +
      '" autocomplete="off" spellcheck="false" /></label>' +
      '<label class="press-editor-wide"><span>压制路径</span><input id="press-editor-press-path" type="text" value="' +
      esc(curPressPath) +
      '" autocomplete="off" spellcheck="false" /></label>' +
      "</div>" +
      '<div class="press-editor-actions">' +
      (!isAdd
        ? '<button type="button" class="press-editor-danger" data-press-editor-action="delete">删除</button>'
        : "") +
      '<span class="press-editor-spacer"></span>' +
      '<button type="button" class="btn sm" data-press-editor-action="cancel">取消</button>' +
      '<button type="button" class="btn sm primary" data-press-editor-action="save">保存</button>' +
      "</div>" +
      "</div>";
    document.body.appendChild(modal);

    var panel = modal.querySelector(".press-editor-panel");
    function savePressEditor() {
      var fmtNode = modal.querySelector("#press-editor-fmt");
      var grpNode = modal.querySelector("#press-editor-grp");
      var workNode = modal.querySelector("#press-editor-work-path");
      var pressNode = modal.querySelector("#press-editor-press-path");
      var nextFmt = normalizePressRelPathForUi(isSplit ? lockedFmt : fmtNode && fmtNode.value);
      var nextGrp = normalizePressRelPathForUi(grpNode && grpNode.value);
      if (!nextFmt) {
        setStatus("压制格式不能为空。", true);
        return;
      }
      hit.row.path = normalizePressRelPathForUi(workNode && workNode.value);
      var nextItem = isAdd ? {} : ordered[ord];
      nextItem[FMT] = nextFmt;
      nextItem[GRP] = nextGrp;
      var nextPressPath = normalizePressRelPathForUi(pressNode && pressNode.value);
      if (nextPressPath) {
        nextItem.press_path = nextPressPath;
      } else if (Object.prototype.hasOwnProperty.call(nextItem, "press_path")) {
        delete nextItem.press_path;
      }
      if (isAdd) {
        ordered.push(nextItem);
      } else {
        ordered[ord] = nextItem;
      }
      hit.row.collectioned_ordered = ordered;
      closePressEditor();
      renderPayload(lastBrowsePayload, true);
      setStatus(isAdd ? "已新增压制信息，保存后写入 YAML。" : "已更新压制信息，保存后写入 YAML。", false);
    }

    modal.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      if (t === modal) {
        closePressEditor();
        return;
      }
      var actionNode = t.closest("[data-press-editor-action]");
      if (!actionNode || !modal.contains(actionNode)) return;
      var action = actionNode.getAttribute("data-press-editor-action");
      if (action === "cancel") {
        closePressEditor();
        return;
      }
      if (action === "save") {
        savePressEditor();
        return;
      }
      if (action === "delete" && !isAdd) {
        if (!window.confirm("删除这个压制信息？")) return;
        if (removePayloadPressItem(yamlRel, indexInFile, ord)) {
          closePressEditor();
          renderPayload(lastBrowsePayload, true);
          setStatus("已删除 1 个压制信息，保存后写入 YAML。", false);
        }
      }
    });
    modal.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") {
        ev.preventDefault();
        closePressEditor();
      }
      if (ev.key === "Enter" && ev.ctrlKey) {
        ev.preventDefault();
        savePressEditor();
      }
    });
    var focusTarget = modal.querySelector(isSplit ? "#press-editor-grp" : "#press-editor-fmt");
    if (focusTarget) focusTarget.focus();
    if (panel) panel.scrollTop = 0;
    return true;
  }

  function removePayloadRow(yamlRel, indexInFile) {
    var hit = findPayloadRow(yamlRel, indexInFile);
    if (!hit) return false;
    var isNew = !!hit.row._isNew;
    hit.rows.splice(hit.rowIndex, 1);
    if (typeof lastBrowsePayload.total === "number" && lastBrowsePayload.total > 0) {
      lastBrowsePayload.total -= 1;
    }
    if (!isNew) {
      deletedSheetRows[sheetRowKey(yamlRel, indexInFile)] = {
        yaml_source_rel: normalizedCatalogRelKey(yamlRel),
        index_in_file: Number(indexInFile),
      };
    }
    return true;
  }

  function removePayloadPressItem(yamlRel, indexInFile, sourceOrd) {
    var hit = findPayloadRow(yamlRel, indexInFile);
    if (!hit) return false;
    var ord = Number(sourceOrd);
    if (!Number.isFinite(ord) || ord < 0) return false;
    var ordered;
    try {
      ordered = JSON.parse(JSON.stringify(getOrderedTags(hit.row)));
    } catch (_unused) {
      ordered = [];
    }
    if (!ordered[ord]) return false;
    ordered.splice(ord, 1);
    hit.row.collectioned_ordered = ordered;
    return true;
  }

  function updatePayloadScalarField(yamlRel, indexInFile, fieldKey, value) {
    var hit = findPayloadRow(yamlRel, indexInFile);
    if (!hit) return false;
    var next = value == null ? "" : String(value);
    if (fieldKey === "date_start") {
      if (!hit.row.date || typeof hit.row.date !== "object") hit.row.date = {};
      hit.row.date.start = next.trim();
      return true;
    }
    if (fieldKey === "date_end") {
      if (!hit.row.date || typeof hit.row.date !== "object") hit.row.date = {};
      hit.row.date.end = next.trim();
      return true;
    }
    if (fieldKey === "name") {
      hit.row.name = next;
      return true;
    }
    return false;
  }

  function updatePayloadEnumFieldFromEl(el, value) {
    if (!el) return false;
    var yrel = el.getAttribute("data-yaml-rel") || "";
    var iif = parseInt(el.getAttribute("data-sheet-iif"), 10);
    if (!Number.isFinite(iif)) return false;
    var enumKey = el.getAttribute("data-enum-key") || "";
    var fieldKey = el.getAttribute("data-field") || "";
    var next = value == null ? "" : String(value).trim();
    var hit = findPayloadRow(yrel, iif);
    if (!hit) return false;
    if (fieldKey && (fieldKey === "domain" || fieldKey === "release_type" || fieldKey === "country")) {
      hit.row[fieldKey] = next;
      return true;
    }
    if (enumKey === "markers") {
      var markerI = parseInt(el.getAttribute("data-marker-i"), 10);
      if (!Number.isFinite(markerI) || markerI < 0) return false;
      var markers = Array.isArray(hit.row.markers) ? hit.row.markers.slice() : [];
      markers[markerI] = next;
      hit.row.markers = markers.filter(function (x) {
        return x != null && String(x).trim();
      });
      return true;
    }
    if (enumKey === GRP) {
      var ord = parseInt(el.getAttribute("data-source-ord"), 10);
      if (!Number.isFinite(ord) || ord < 0) return false;
      var ordered;
      try {
        ordered = JSON.parse(JSON.stringify(getOrderedTags(hit.row)));
      } catch (_unused) {
        ordered = [];
      }
      if (!ordered[ord] || typeof ordered[ord] !== "object") return false;
      var fmCol = el.getAttribute("data-press-fmt") || "";
      ordered[ord][FMT] = fmCol;
      ordered[ord][GRP] = next;
      hit.row.collectioned_ordered = ordered;
      return true;
    }
    return false;
  }

  function enumEditSelectHtml(trigger) {
    var enumKey = trigger.getAttribute("data-enum-key") || "";
    var cur = enumValueForUi(enumKey, trigger.getAttribute("data-current-value"));
    var opts = browseEnumOptions[enumKey];
    if (enumKey !== GRP && (!Array.isArray(opts) || !opts.length)) return "";
    var vals = enumValueTuplesForEditor(enumKey, cur);
    var i;
    var attrs = "";
    var attrNames = [
      "data-sheet-iif",
      "data-yaml-rel",
      "data-field",
      "data-source-ord",
      "data-press-fmt",
      "data-marker-i",
    ];
    for (i = 0; i < attrNames.length; i++) {
      var av = trigger.getAttribute(attrNames[i]);
      if (av != null) attrs += " " + attrNames[i] + '="' + esc(av) + '"';
    }
    var cls = "sheet-select sheet-inline-select";
    if (trigger.classList && trigger.classList.contains("sheet-pair-plain")) {
      cls += " sheet-select-pair";
    }
    var h =
      '<select class="' +
      esc(cls) +
      '" data-enum-key="' +
      esc(enumKey) +
      '"' +
      attrs +
      ">";
    for (i = 0; i < vals.length; i++) {
      var v = vals[i].value;
      h +=
        '<option value="' +
        esc(v) +
        '"' +
        (v === cur ? " selected" : "") +
        ">" +
        esc(vals[i].label) +
        "</option>";
    }
    h += "</select>";
    return h;
  }

  function beginInlineEnumEdit(trigger) {
    if (!sheetEditMode || !trigger || trigger.getAttribute("data-inline-active") === "1") return;
    var html = enumEditSelectHtml(trigger);
    if (!html) return;
    trigger.setAttribute("data-inline-active", "1");
    trigger.innerHTML = html;
    var sel = trigger.querySelector("select.sheet-inline-select");
    if (sel) {
      sel.focus();
      if (typeof sel.showPicker === "function") {
        try {
          sel.showPicker();
        } catch (_unused) {}
      }
    }
  }

  function beginInlineScalarEdit(trigger) {
    if (!sheetEditMode || !trigger || trigger.getAttribute("data-inline-active") === "1") return;
    var cur = trigger.getAttribute("data-current-value") || "";
    var sid = trigger.getAttribute("data-sheet-iif") || "";
    var yrel = trigger.getAttribute("data-yaml-rel") || "";
    var fieldKey = trigger.getAttribute("data-field") || "";
    var dateAttrs = fieldKey === "date_start" || fieldKey === "date_end"
      ? ' placeholder="YYYY-MM-DD" title="日期格式：YYYY-MM-DD；未知部分保留 0 或 X"'
      : "";
    if (dateAttrs) cur = displayCollectionDate(cur);
    trigger.setAttribute("data-inline-active", "1");
    trigger.innerHTML =
      '<input type="text" class="sheet-field-input sheet-field-scalar sheet-inline-input" autocomplete="off" spellcheck="false" data-sheet-iif="' +
      esc(sid) +
      '" data-yaml-rel="' +
      esc(yrel) +
      '" data-field="' +
      esc(fieldKey) +
      '" value="' +
      esc(cur) +
      '"' + dateAttrs + ' />';
    var inp = trigger.querySelector("input.sheet-inline-input");
    if (inp) {
      inp.focus();
      inp.select();
    }
  }

  function commitInlineScalarInput(input) {
    if (!input) return false;
    var wrap = input.closest('[data-action="inline-edit-scalar"]');
    if (!wrap) return false;
    var yrel = wrap.getAttribute("data-yaml-rel") || "";
    var iif = parseInt(wrap.getAttribute("data-sheet-iif"), 10);
    var fieldKey = wrap.getAttribute("data-field") || "";
    if (!Number.isFinite(iif)) return false;
    return updatePayloadScalarField(yrel, iif, fieldKey, input.value);
  }

  function bindSheetInlineEditOnce() {
    if (sheetInlineEditBound || !$view) return;
    sheetInlineEditBound = true;
    listen($view, "click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var workTrigger = t.closest('[data-action="work-details"]');
      if (workTrigger && $view.contains(workTrigger)) {
        openWorkDetails(workTrigger.getAttribute("data-yaml-rel") || "", Number(workTrigger.getAttribute("data-sheet-iif")));
        return;
      }
      if (!sheetEditMode) return;
      if (t.closest("select, input, button")) return;
      var enumTrigger = t.closest('[data-action="inline-edit-enum"]');
      if (enumTrigger && $view.contains(enumTrigger)) {
        beginInlineEnumEdit(enumTrigger);
        return;
      }
      var scalarTrigger = t.closest('[data-action="inline-edit-scalar"]');
      if (scalarTrigger && $view.contains(scalarTrigger)) {
        beginInlineScalarEdit(scalarTrigger);
      }
    });
    listen($view, "keydown", function (ev) {
      var t = ev.target;
      if (!t || !t.closest || !sheetEditMode) return;
      if (t.matches && t.matches("input.sheet-inline-input")) {
        if (ev.key === "Enter") {
          if (commitInlineScalarInput(t)) renderPayload(lastBrowsePayload, true);
        } else if (ev.key === "Escape") {
          renderPayload(lastBrowsePayload, true);
        }
        return;
      }
      if (ev.key !== "Enter" && ev.key !== " ") return;
      var enumTrigger = t.closest('[data-action="inline-edit-enum"]');
      var scalarTrigger = t.closest('[data-action="inline-edit-scalar"]');
      if (enumTrigger && $view.contains(enumTrigger)) {
        ev.preventDefault();
        beginInlineEnumEdit(enumTrigger);
      } else if (scalarTrigger && $view.contains(scalarTrigger)) {
        ev.preventDefault();
        beginInlineScalarEdit(scalarTrigger);
      }
    });
    listen($view, "change", function (ev) {
      var t = ev.target;
      if (!t || !t.matches || !t.matches("select.sheet-inline-select")) return;
      if (updatePayloadEnumFieldFromEl(t, t.value)) {
        renderPayload(lastBrowsePayload, true);
      }
    });
    listen($view,
      "focusout",
      function (ev) {
        var t = ev.target;
        if (!t || !t.matches || !t.matches("input.sheet-inline-input")) return;
        window.setTimeout(function () {
          if (commitInlineScalarInput(t)) renderPayload(lastBrowsePayload, true);
        }, 0);
      },
      true,
    );
  }

  function deletedRowsForSave() {
    var out = [];
    var k;
    for (k in deletedSheetRows) {
      if (Object.prototype.hasOwnProperty.call(deletedSheetRows, k)) {
        var row = Object.assign({}, deletedSheetRows[k]);
        var sourceHash = sourceHashForRow(row);
        if (sourceHash) row.source_sha256 = sourceHash;
        out.push(row);
      }
    }
    return out;
  }

  async function openPressConfiguredFolder(btn) {
    var workPath = trimPathValue(btn && btn.getAttribute("data-work-path"));
    var pressPath = trimPathValue(btn && btn.getAttribute("data-press-path"));
    if (!workPath || !pressPath) {
      setStatus("连接配置不完整，无法打开目录。", true);
      return;
    }
    setStatus("打开连接目录中…", false);
    try {
      var out = await fetchJson("/api/collection-detail/press/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: workPath, press_path: pressPath }),
      });
      if (out.res.status >= 400 || !out.data || out.data.ok === false) {
        setStatus(
          (out.data && out.data.error) || browseHttpFailHint(out.res.status, "打开连接目录"),
          true,
        );
        return;
      }
      setStatus("已打开连接目录：" + (out.data.path || pressPath), false);
    } catch (err) {
      setStatus("打开连接目录失败：" + (err.message || String(err)), true);
    }
  }

  function bindSheetEditActionsOnce() {
    if (sheetEditActionsBound || !$view) return;
    sheetEditActionsBound = true;
    listen($view, "click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var btn = t.closest("[data-action]");
      if (!btn || !$view.contains(btn)) return;
      var action = btn.getAttribute("data-action");
      if (
        action !== "delete-row" &&
        action !== "delete-press-item" &&
        action !== "edit-press-item" &&
        action !== "add-press-item" &&
        action !== "open-press-folder"
      )
        return;
      if (action === "open-press-folder") {
        ev.preventDefault();
        ev.stopPropagation();
        void openPressConfiguredFolder(btn);
        return;
      }
      if (!sheetEditMode) return;
      var yrel = btn.getAttribute("data-yaml-rel") || "";
      var iif = parseInt(btn.getAttribute("data-sheet-iif"), 10);
      if (!Number.isFinite(iif)) return;
      if (action === "delete-row") {
        if (removePayloadRow(yrel, iif)) {
          renderPayload(lastBrowsePayload, true);
          setStatus("已删除 1 行，保存后写入 YAML。", false);
        }
        return;
      }
      var ord = parseInt(btn.getAttribute("data-source-ord"), 10);
      if (action === "add-press-item") {
        openPressItemEditor(yrel, iif, null, "add", "");
        return;
      }
      if (action === "edit-press-item") {
        openPressItemEditor(
          yrel,
          iif,
          ord,
          btn.getAttribute("data-edit-mode") || "full",
          btn.getAttribute("data-press-fmt") || "",
        );
        return;
      }
      if (removePayloadPressItem(yrel, iif, ord)) {
        renderPayload(lastBrowsePayload, true);
        setStatus("已删除 1 个压制项，保存后写入 YAML。", false);
      }
    });
  }

  function bindSheetAddRowOnce() {
    if (sheetAddRowBound || !$btnAddRow) return;
    sheetAddRowBound = true;
    listen($btnAddRow, "click", openNewWorkDialog);
  }

  function canAddNewWork() {
    if (browseSaving || browseLoading || context.featureHost.getActiveId() !== "collection-detail") return false;
    if (lastBrowsePayload && lastBrowsePayload.ok) {
      return !!(lastBrowsePayload.save && lastBrowsePayload.save.enabled);
    }
    return hasWritableDbConfigForNewRow();
  }

  function openNewWorkDialog() {
    if (!canAddNewWork()) {
      setStatus("当前不可新增作品；请载入可写 DB 数据，或等待保存完成。", true);
      return;
    }
    if (!window.NimdaNewWorkDialog) {
      setStatus("新增作品表单尚未加载，请刷新页面后重试。", true);
      return;
    }
    var openingPayload = lastBrowsePayload;
    var enumOptions = {};
    Object.keys(browseEnumOptions).forEach(function (key) {
      enumOptions[key] = (browseEnumOptions[key] || []).map(rawEnumOptSlug).filter(Boolean);
    });
    window.NimdaNewWorkDialog.open({
      enumOptions: enumOptions,
      enumLabel: browseEnumDisplay,
      defaults: {
        domain: defaultEnumValue("domain", "animation"),
        release_type: defaultEnumValue("release_type", "tv"),
        country: defaultEnumValue("country", "japan"),
      },
      targetLabel: currentYearCatalogRelForNewRow(),
      targetLabelForDraft: currentYearCatalogRelForNewRow,
      onSubmit: function (patch) {
        if (!canAddNewWork() || lastBrowsePayload !== openingPayload) {
          throw new Error("列表数据已重新加载，请保留填写内容，关闭后重新打开新增表单。");
        }
        var rel = addPayloadRow(patch);
        if (!rel) throw new Error("当前数据不可写，尚未添加作品。");
        if ($chkEdit) $chkEdit.checked = true;
        sheetEditMode = true;
        renderPayload(lastBrowsePayload, true);
        setStatus("已加入待保存列表：" + patch.name + " · 点击「保存到 YAML」写入 " + rel + "。", false);
      },
    });
  }

  function enumDraftFromCurrent() {
    var keys = [FMT, GRP];
    var draft = {};
    var ki;
    for (ki = 0; ki < keys.length; ki++) {
      var key = keys[ki];
      var vals = Array.isArray(browseEnumOptions[key])
        ? browseEnumOptions[key].slice()
        : [];
      draft[key] = vals.map(function (v) {
        var s = String(v == null ? "" : v).trim();
        return { old: s, value: s, deleted: false };
      });
    }
    return draft;
  }

  function ensureEnumDraft() {
    if (!enumEditorDraft) enumEditorDraft = enumDraftFromCurrent();
    return enumEditorDraft;
  }

  function enumEditorLabel(key) {
    return key === FMT ? "压制格式" : "压制组";
  }

  function renderEnumEditor() {
    if (!$enumEditorPanel || !$enumEditorBody || $enumEditorPanel.hidden) return;
    var draft = ensureEnumDraft();
    var keys = [FMT, GRP];
    var h = "";
    var ki;
    for (ki = 0; ki < keys.length; ki++) {
      var key = keys[ki];
      var rows = draft[key] || [];
      h +=
        '<section class="enum-editor-section" data-enum-key="' +
        esc(key) +
        '"><div class="enum-editor-head"><span>' +
        esc(enumEditorLabel(key)) +
        '</span><button type="button" class="enum-add-btn" data-action="enum-add" data-enum-key="' +
        esc(key) +
        '">＋</button></div><div class="enum-editor-list">';
      var ri;
      for (ri = 0; ri < rows.length; ri++) {
        var row = rows[ri];
        var deletedCls = row.deleted ? " is-deleted" : "";
        h +=
          '<div class="enum-editor-row' +
          deletedCls +
          '" data-enum-key="' +
          esc(key) +
          '" data-enum-row-index="' +
          esc(String(ri)) +
          '"><input class="enum-editor-input" type="text" autocomplete="off" spellcheck="false" value="' +
          esc(row.value || "") +
          '" data-action="enum-input" data-enum-key="' +
          esc(key) +
          '" data-enum-row-index="' +
          esc(String(ri)) +
          '"' +
          (row.deleted ? " disabled" : "") +
          '/><button type="button" class="enum-row-delete-btn' +
          (row.deleted ? " is-undo" : "") +
          '" data-action="enum-delete" data-enum-key="' +
          esc(key) +
          '" data-enum-row-index="' +
          esc(String(ri)) +
          '">' +
          (row.deleted ? "撤销" : "删除") +
          "</button></div>";
      }
      h += "</div></section>";
    }
    $enumEditorBody.innerHTML = h;
  }

  function enumEditorEditsFromDraft() {
    var draft = ensureEnumDraft();
    var keys = [FMT, GRP];
    var edits = [];
    var finalSeenByKey = {};
    var ki;
    for (ki = 0; ki < keys.length; ki++) {
      finalSeenByKey[keys[ki]] = Object.create(null);
    }
    for (ki = 0; ki < keys.length; ki++) {
      var key = keys[ki];
      var rows = draft[key] || [];
      var ri;
      for (ri = 0; ri < rows.length; ri++) {
        var row = rows[ri];
        var oldVal = String(row.old || "").trim();
        var newVal = String(row.value || "").trim();
        if (!row.deleted && newVal) {
          if (finalSeenByKey[key][newVal]) {
            throw new Error(enumEditorLabel(key) + " 重复：" + newVal);
          }
          finalSeenByKey[key][newVal] = true;
        }
        if (!oldVal) {
          if (!row.deleted && newVal) {
            edits.push({ enum_key: key, action: "add", new_value: newVal });
          }
          continue;
        }
        if (row.deleted || !newVal) {
          edits.push({ enum_key: key, action: "delete", value: oldVal });
        } else if (newVal !== oldVal) {
          edits.push({
            enum_key: key,
            action: "rename",
            value: oldVal,
            new_value: newVal,
          });
        }
      }
    }
    return edits;
  }

  function bindEnumEditorOnce() {
    if (enumEditorBound) return;
    enumEditorBound = true;
    if ($btnEnumEditor) {
      listen($btnEnumEditor, "click", function () {
        if (!sheetEditMode) {
          setStatus("请先勾选「编辑模式」。", true);
          return;
        }
        enumEditorDraft = enumDraftFromCurrent();
        if ($enumEditorPanel) $enumEditorPanel.hidden = false;
        renderEnumEditor();
      });
    }
    if ($btnEnumClose) {
      listen($btnEnumClose, "click", function () {
        if ($enumEditorPanel) $enumEditorPanel.hidden = true;
      });
    }
    if ($enumEditorPanel) {
      listen($enumEditorPanel, "input", function (ev) {
        var t = ev.target;
        if (!t || !t.getAttribute || t.getAttribute("data-action") !== "enum-input") {
          return;
        }
        var key = t.getAttribute("data-enum-key");
        var ix = parseInt(t.getAttribute("data-enum-row-index"), 10);
        var draft = ensureEnumDraft();
        if (!draft[key] || !draft[key][ix]) return;
        draft[key][ix].value = String(t.value || "");
      });
      listen($enumEditorPanel, "click", function (ev) {
        var t = ev.target;
        if (!t || !t.closest) return;
        var btn = t.closest("[data-action]");
        if (!btn || !$enumEditorPanel.contains(btn)) return;
        var action = btn.getAttribute("data-action");
        var key = btn.getAttribute("data-enum-key");
        var draft = ensureEnumDraft();
        if (action === "enum-add") {
          if (!draft[key]) draft[key] = [];
          draft[key].push({ old: "", value: "", deleted: false });
          renderEnumEditor();
          var inputs = $enumEditorPanel.querySelectorAll(
            '.enum-editor-input[data-enum-key="' + esc(key || "") + '"]',
          );
          if (inputs.length) inputs[inputs.length - 1].focus();
          return;
        }
        if (action === "enum-delete") {
          var ix = parseInt(btn.getAttribute("data-enum-row-index"), 10);
          if (!draft[key] || !draft[key][ix]) return;
          if (!draft[key][ix].old) {
            draft[key].splice(ix, 1);
          } else {
            draft[key][ix].deleted = !draft[key][ix].deleted;
          }
          renderEnumEditor();
        }
      });
    }
    if ($btnEnumSave) {
      listen($btnEnumSave, "click", function () {
        void doSaveEnumEdits();
      });
    }
  }

  async function doSaveEnumEdits() {
    var edits;
    try {
      edits = enumEditorEditsFromDraft();
    } catch (e) {
      setStatus(e.message || String(e), true);
      return;
    }
    if (!edits.length) {
      setStatus("枚举没有变化。", false);
      return;
    }
    setStatus("保存枚举中…", false);
    try {
      var out = await fetchJson("/api/config/enum-edits", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ edits: edits }),
      });
      if (!out.res.ok || !out.data.ok) {
        setStatus(out.data.error || "枚举保存失败", true);
        return;
      }
      enumEditorDraft = null;
      await loadServerConfig().catch(function () {});
      if ($enumEditorPanel && !$enumEditorPanel.hidden) {
        enumEditorDraft = enumDraftFromCurrent();
        renderEnumEditor();
      }
      var dw = out.data.data_writes || [];
      setStatus(
        "枚举已保存" + (dw.length ? "，同步更新 " + dw.length + " 个 YAML。" : "。"),
        false,
      );
      if (lastBrowsePayload && lastBrowsePayload.ok) {
        if (lastBrowsePayload.save && lastBrowsePayload.save.enabled) {
          await reloadBrowseAfterSave().catch(function () {});
        } else {
          renderPayload(lastBrowsePayload, true);
        }
      }
    } catch (err) {
      setStatus("枚举保存异常：" + (err.message || String(err)), true);
    }
  }

  /**
   * Color chips by press_format (720p / BDRip, etc.).
   */
  function pressFormatTintKey(fmt) {
    const t = String(fmt || "")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, "");
    if (!t) return "other";
    if (t.includes("2160") || t.includes("3840") || t === "4k") return "uhd";
    if (t.includes("1080")) return "p1080";
    if (t.includes("720")) return "p720";
    if (t.includes("480")) return "p480";
    if (t.includes("bdrip") || t === "bd" || t.includes("bluray")) return "bdrip";
    if (t.includes("webrip")) return "webrip";
    if (t.includes("web-dl") || t.includes("webdl")) return "webdl";
    if (t.includes("hdtv")) return "hdtv";
    if (t.includes("dvd")) return "dvd";
    if (t.includes("aac") || t.includes("mp4") || t.includes("mkv")) return "mux";
    return "other";
  }

  /** 后端未提供 ordered 时在浏览器侧拼接 */
  function buildCollectionedOrderedFallback(r) {
    const out = [];
    const main = r.collectioned || [];
    let i;
    for (i = 0; i < main.length; i++) {
      out.push(
        Object.assign({}, main[i], {
          segment: "main",
          continuation_index: null,
          continuation_title: null,
        }),
      );
    }
    const conts = r.continuations || [];
    for (let bi = 0; bi < conts.length; bi++) {
      const b = conts[bi];
      const title = typeof b.title === "string" && b.title.trim() ? b.title.trim() : null;
      const rows = b.collectioned || [];
      for (let j = 0; j < rows.length; j++) {
        out.push(
          Object.assign({}, rows[j], {
            segment: "continuation",
            continuation_index: bi,
            continuation_title: title,
          }),
        );
      }
    }
    return out;
  }

  function getOrderedTags(r) {
    const od = r.collectioned_ordered;
    let base = Array.isArray(od) ? od : buildCollectionedOrderedFallback(r);
    if (!Array.isArray(base)) return [];
    return base.filter(function (x) {
      return x != null && typeof x === "object";
    });
  }

  /**
   * 列顺序：`enum_options.press_format` 中出现的格式优先，再接数据中出现但尚未列出的格式（出现顺序）。
   */
  function derivePressFormatColumns(flat) {
    const cols = [];
    const seen = Object.create(null);
    var fmOpts = browseEnumOptions[FMT];
    if (Array.isArray(fmOpts)) {
      var qi;
      for (qi = 0; qi < fmOpts.length; qi++) {
        var kk = fmOpts[qi];
        kk = kk == null ? "" : String(kk).trim();
        if (!kk || seen[kk]) continue;
        seen[kk] = true;
        cols.push(kk);
      }
    }
    var fi;
    for (fi = 0; fi < flat.length; fi++) {
      const ordered = getOrderedTags(flat[fi].row);
      let j;
      for (j = 0; j < ordered.length; j++) {
        const pc = pressPairCells(ordered[j]);
        var kkCol = pc.fm == null ? "" : String(pc.fm).trim();
        if (!kkCol || seen[kkCol]) continue;
        seen[kkCol] = true;
        cols.push(kkCol);
      }
    }
    return cols;
  }

  function buildBrowseFlatPacks(groups) {
    const flatOut = [];
    var giBp;
    for (giBp = 0; giBp < groups.length; giBp++) {
      const gb = groups[giBp];
      const pkB = gb.profile_key || "";
      const lblB = gb.profile_label || pkB;
      const rowsB = gb.rows || [];
      var riBp;
      for (riBp = 0; riBp < rowsB.length; riBp++) {
        flatOut.push({
          profile_key: pkB,
          profile_label: lblB,
          registered_profile: !!gb.registered_profile,
          row: rowsB[riBp],
        });
      }
    }
    return flatOut;
  }

  function loadPressSplitVisibleMap() {
    var o = Object.create(null);
    try {
      var s = window.NimdaCommon.readPreference(LS_KEY_PRESS_SPLIT_VISIBLE);
      if (!s) return o;
      var j = JSON.parse(s);
      if (j && typeof j === "object" && !Array.isArray(j)) {
        var mk;
        for (mk in j) {
          if (Object.prototype.hasOwnProperty.call(j, mk) && j[mk] === true) {
            o[String(mk)] = true;
          }
        }
      }
    } catch (eLm) {}
    return o;
  }

  function savePressSplitVisibleMap(map) {
    try {
      window.NimdaCommon.writePreference(
        LS_KEY_PRESS_SPLIT_VISIBLE,
        JSON.stringify(map && typeof map === "object" ? map : {}),
      );
    } catch (eSm) {}
  }

  function sanitizePressSplitVisForCols(fmCols, rawMap) {
    var out = Object.create(null);
    if (!fmCols || !fmCols.length || !rawMap) return out;
    var si;
    for (si = 0; si < fmCols.length; si++) {
      var fk = fmCols[si];
      if (rawMap[fk] === true) out[fk] = true;
    }
    return out;
  }

  /** 是否有任一压制格式分列被勾选（表头「分列▼」高亮） */
  function pressSplitAnyVisibleForCols(fmCols, visMap) {
    if (!fmCols || !fmCols.length || !visMap) return false;
    var hi;
    for (hi = 0; hi < fmCols.length; hi++) {
      if (visMap[fmCols[hi]] === true) return true;
    }
    return false;
  }

  /** 压制分列复选列表（默认不勾，仅「压制汇总」列；与表头下拉 / change 共用） */
  function pressFmtSplitChecksInnerHtml(fmCols, visMap) {
    if (!fmCols || !fmCols.length) return "";
    var chi;
    var items = "";
    for (chi = 0; chi < fmCols.length; chi++) {
      var fm = fmCols[chi];
      var labFm = browseEnumDisplay(FMT, fm);
      if (labFm === "—") labFm = String(fm);
      var ck = visMap[fm] === true ? " checked" : "";
      items +=
        '<label class="press-fmt-split-item">' +
        '<input type="checkbox" class="press-fmt-vis-cb" data-press-fmt-slug="' +
        esc(String(fm)) +
        '"' +
        ck +
        '/>' +
        "<span>" +
        esc(labFm) +
        "</span></label>";
    }
    return (
      '<div class="press-fmt-split-cbs press-fmt-split-cbs-head" role="group" aria-label="压制分列">' +
      items +
      "</div>"
    );
  }

  /** 压制组展示名／slug（用于同人列多 pill 的字母序） */
  function pressGroupSortKey(gpTrim) {
    if (!gpTrim) return "";
    var d = browseEnumDisplay(GRP, gpTrim);
    if (d === "—") d = gpTrim;
    return String(d).trim();
  }

  /** 同人列多条目：字母序（空组垫底）；同日文比较 */
  function comparePressFormatCellEntries(a, b) {
    var ka = nzForSort(pressGroupSortKey(a.gpTrim)).toLowerCase();
    var kb = nzForSort(pressGroupSortKey(b.gpTrim)).toLowerCase();
    var ea = ka === "";
    var eb = kb === "";
    if (ea !== eb) return ea ? 1 : -1;
    var cmp = ka.localeCompare(kb, undefined, {
      numeric: true,
      sensitivity: "base",
    });
    if (cmp !== 0) return cmp;
    return a._sourceOrd - b._sourceOrd;
  }

  /** 与分列 cell 一致：指定压制格式 slug 下的条目（含 _sourceOrd），组内 comparePressFormatCellEntries */
  function collectPressFmtEntriesForWant(row, want) {
    want = want == null ? "" : String(want).trim();
    if (!want) return [];
    const ordered = getOrderedTags(row);
    var out = [];
    var idxLoop;
    for (idxLoop = 0; idxLoop < ordered.length; idxLoop++) {
      var rawIt = ordered[idxLoop];
      const itObj = rawIt != null && typeof rawIt === "object" ? rawIt : {};
      const pcLoop = pressPairCells(itObj);
      const fmcLoop = pcLoop.fm ? String(pcLoop.fm).trim() : "";
      var gpRi = pcLoop.gp;
      var gpTrim =
        gpRi == null || String(gpRi).trim() === ""
          ? ""
          : String(gpRi).trim();
      if (fmcLoop !== want) continue;
      if (!fmcLoop && !gpTrim) continue;
      out.push({
        _sourceOrd: idxLoop,
        itObj: itObj,
        gpTrim: gpTrim,
      });
    }
    out.sort(comparePressFormatCellEntries);
    return out;
  }

  /** 汇总 pill：按格式 slug 稳定映射到 12 个色槽，减少不同 slug 撞色 */
  function pressFmtAggHueSlot(slug) {
    var k = filterHaystackPiece(slug == null ? "" : String(slug));
    var u = 2166136261 >>> 0;
    var qq;
    for (qq = 0; qq < k.length; qq++)
      u = Math.imul(u ^ k.charCodeAt(qq), 16777619) >>> 0;
    var n = u % 12;
    return n < 10 ? "0" + n : String(n);
  }

  /** 汇总行对齐：粗略计算「半宽等效格数」（ASCII≈1、常见日文/汉字区块≈2） */
  function aggDispSlots(str) {
    var s = String(str || "");
    var total = 0;
    var i = 0;
    while (i < s.length) {
      var cp = s.codePointAt(i);
      i += cp > 0xffff ? 2 : 1;
      var w = 1;
      if (cp >= 0x20 && cp <= 0x7e) w = 1;
      else if (
        cp === 0x3000 ||
        (cp >= 0x3040 && cp <= 0x30ff) ||
        (cp >= 0x3400 && cp <= 0x4dbf) ||
        (cp >= 0x4e00 && cp <= 0x9fff) ||
        (cp >= 0xf900 && cp <= 0xfaff) ||
        (cp >= 0xff00 && cp <= 0xffef) ||
        (cp >= 0x20000 && cp <= 0x2ffff)
      )
        w = 2;
      total += w;
    }
    return total;
  }

  /** 尾部补 ASCII 空格，使 aggDispSlots 达到 targetSlots（与同列其它 pill 拉齐） */
  function aggPadTrailingToSlots(str, targetSlots) {
    var tgt =
      typeof targetSlots === "number" && targetSlots > 0
        ? targetSlots
        : 0;
    var out = String(str || "");
    while (aggDispSlots(out) < tgt) out += "\u0020";
    return out;
  }

  /** 压制汇总本条所有 pill（顺序与渲染一致） */
  function gatherPressAggSpecs(row, fmCols, splitVisMap) {
    fmCols = fmCols || [];
    splitVisMap = splitVisMap || {};
    var specs = [];

    function addSegment(fmSlugP) {
      if (!fmSlugP) return;
      var entsSeg = collectPressFmtEntriesForWant(row, fmSlugP);
      if (!entsSeg.length) return;
      var qix;
      for (qix = 0; qix < entsSeg.length; qix++) {
        var itCellAg = entsSeg[qix].itObj;
        var gpTrimP = entsSeg[qix].gpTrim;
        var dispFm = browseEnumDisplay(FMT, fmSlugP);
        if (dispFm === "—") dispFm = String(fmSlugP);
        var dispGpBare = gpTrimP ? browseEnumDisplay(GRP, gpTrimP) : "";
        var dispGp = dispGpBare === "—" ? String(gpTrimP) : dispGpBare;
        var plainLine = dispFm + " / " + (gpTrimP ? dispGp : "—");
        var splitOn = !!(fmSlugP && splitVisMap[fmSlugP]);
        specs.push({
          fmSlugP: fmSlugP,
          itCellAg: itCellAg,
          gpTrimP: gpTrimP,
          dispFm: dispFm,
          dispGp: dispGp,
          plainLine: plainLine,
          splitOn: splitOn,
          _sourceOrd: entsSeg[qix]._sourceOrd,
        });
      }
    }

    var fai;
    for (fai = 0; fai < fmCols.length; fai++) {
      var fmW = fmCols[fai] == null ? "" : String(fmCols[fai]).trim();
      addSegment(fmW);
    }
    var orphans = orphanPressFormatsForRow(row, fmCols);
    for (fai = 0; fai < orphans.length; fai++) addSegment(orphans[fai]);
    return specs;
  }

  /** 某格式列排序：与各 cell 展示的压制组顺序一致（多组字母序；分隔 \\u0001）。 */
  function sortComparablePressFormatGroups(r, fm) {
    const want =
      fm == null ? "" : String(fm).trim();
    if (!want) return "";
    const ordered = getOrderedTags(r);
    var bits = [];
    var ii;
    for (ii = 0; ii < ordered.length; ii++) {
      const pc = pressPairCells(ordered[ii]);
      const fmc =
        pc.fm == null ? "" : String(pc.fm).trim();
      var gpRaw = pc.gp;
      var gp =
        gpRaw == null || String(gpRaw).trim() === ""
          ? ""
          : String(gpRaw).trim();
      if (fmc !== want) continue;
      bits.push({
        gpTrim: gp,
        plain: nzForSort(pressGroupSortKey(gp)).toLowerCase(),
      });
    }
    bits.sort(function (aa, bb) {
      var paa = aa.plain;
      var pbb = bb.plain;
      var ezA = paa === "";
      var ezB = pbb === "";
      if (ezA !== ezB) return ezA ? 1 : -1;
      return paa.localeCompare(pbb, undefined, {
        numeric: true,
        sensitivity: "base",
      });
    });
    var outParts = [];
    var biJoin;
    for (biJoin = 0; biJoin < bits.length; biJoin++) {
      outParts.push(bits[biJoin].plain);
    }
    return outParts.join("\u0001");
  }

  /** 行内出现、但未列入当前分列顺序的压制格式（按在行内标签序中首次出现排序） */
  function orphanPressFormatsForRow(row, fmColOrder) {
    var inCols = Object.create(null);
    var ci;
    for (ci = 0; ci < (fmColOrder || []).length; ci++) {
      var sc = fmColOrder[ci] == null ? "" : String(fmColOrder[ci]).trim();
      if (sc) inCols[sc] = true;
    }
    var out = [];
    var seen = Object.create(null);
    const ordered = getOrderedTags(row);
    var ii;
    for (ii = 0; ii < ordered.length; ii++) {
      var pc = pressPairCells(ordered[ii]);
      var fmc = pc.fm == null ? "" : String(pc.fm).trim();
      if (!fmc || inCols[fmc] || seen[fmc]) continue;
      seen[fmc] = true;
      out.push(fmc);
    }
    return out;
  }

  /** 「压制汇总」列排序：按当前格式列序拼接各格式可比串，未分列的格式按 orphan 序接在末尾 */
  function sortComparableAllPressFormats(row, fmColOrder) {
    if (!fmColOrder || !fmColOrder.length) return "";
    var acc = [];
    var ai;
    for (ai = 0; ai < fmColOrder.length; ai++) {
      var piece = sortComparablePressFormatGroups(row, fmColOrder[ai]);
      if (piece) acc.push(piece);
    }
    var orphans = orphanPressFormatsForRow(row, fmColOrder);
    for (ai = 0; ai < orphans.length; ai++) {
      var op = sortComparablePressFormatGroups(row, orphans[ai]);
      if (op) acc.push(op);
    }
    return acc.join("\u0002");
  }

  /** 单列压制筛选用：枚举展示名 + slug + 各组展示名／原文 + 续行标题 */
  function pressFormatFilterHaystack(row, fm) {
    var want = fm == null ? "" : String(fm).trim();
    if (!want) return "";
    const parts = [];
    var dispF = browseEnumDisplay(FMT, want);
    if (dispF !== "—") parts.push(dispF);
    parts.push(want);

    const ordered = getOrderedTags(row);
    var idxLoop;
    for (idxLoop = 0; idxLoop < ordered.length; idxLoop++) {
      var rawIt = ordered[idxLoop];
      const itObj = rawIt != null && typeof rawIt === "object" ? rawIt : {};
      const pcLoop = pressPairCells(itObj);
      const fmcLoop = pcLoop.fm ? String(pcLoop.fm).trim() : "";
      var gpRi = pcLoop.gp;
      var gpTrim =
        gpRi == null || String(gpRi).trim() === ""
          ? ""
          : String(gpRi).trim();
      if (fmcLoop !== want) continue;
      if (!gpTrim && !fmcLoop) continue;
      parts.push(browseEnumDisplay(GRP, gpTrim));
      parts.push(gpTrim);
      if (itObj.segment === "continuation" && itObj.continuation_title)
        parts.push(String(itObj.continuation_title));
    }
    return parts.join(" ");
  }

  function buildRowFilterBlob(pack, fmCols) {
    const r = pack.row;
    const d = r.date || {};
    const blob = {};

    blob.__domainSlug = filterHaystackPiece(r.domain || "");
    blob.__releaseSlug = filterHaystackPiece(r.release_type || "");
    blob.__countrySlug = filterHaystackPiece(r.country || "");

    blob.domain =
      filterHaystackPiece(browseEnumDisplay("domain", r.domain)) +
      " " +
      filterHaystackPiece(r.domain);
    blob.release_type =
      filterHaystackPiece(browseEnumDisplay("release_type", r.release_type)) +
      " " +
      filterHaystackPiece(r.release_type);
    blob.date_start = collectionDateFilterHaystack(d.start);
    blob.date_end = collectionDateFilterHaystack(d.end);
    blob.country =
      filterHaystackPiece(browseEnumDisplay("country", r.country)) +
      " " +
      filterHaystackPiece(r.country);

    blob.name =
      filterHaystackPiece(r.name || "") +
      " " +
      filterHaystackPiece(pack.profile_label || "") +
      " " +
      filterHaystackPiece(r.yaml_source_rel || "");

    var mkParts = [];
    const mArr = Array.isArray(r.markers) ? r.markers : [];
    var mj;
    var mkSlugs = [];
    for (mj = 0; mj < mArr.length; mj++) {
      var mv = typeof mArr[mj] === "string" ? mArr[mj].trim() : "";
      if (!mv) continue;
      mkSlugs.push(filterHaystackPiece(mv));
      mkParts.push(filterHaystackPiece(browseEnumDisplay("markers", mv) + " " + mv));
    }
    blob.markers = mkParts.join(" ");
    blob.__markersSlugs = mkSlugs;

    var fmtGrpBag = {};
    var fq;
    for (fq = 0; fq < fmCols.length; fq++) {
      var slug2 =
        fmCols[fq] == null ? "" : String(fmCols[fq]).trim();
      var fk2 = FMT_SORT_PREFIX + encodeURIComponent(slug2 || "");
      blob[fk2] = filterHaystackPiece(pressFormatFilterHaystack(r, slug2));
      fmtGrpBag[fk2] = collectGroupsForFmSlug(r, slug2);
    }
    blob.__fmtGroupSlugs = fmtGrpBag;

    var aggHayParts = [];
    for (fq = 0; fq < fmCols.length; fq++) {
      var slugAgg =
        fmCols[fq] == null ? "" : String(fmCols[fq]).trim();
      if (!slugAgg) continue;
      aggHayParts.push(pressFormatFilterHaystack(r, slugAgg));
    }
    var orphanAgg = orphanPressFormatsForRow(r, fmCols);
    for (fq = 0; fq < orphanAgg.length; fq++) {
      aggHayParts.push(pressFormatFilterHaystack(r, orphanAgg[fq]));
    }
    blob[SHEET_PRESS_AGG_SORT_KEY] = filterHaystackPiece(
      aggHayParts.join(" "),
    );
    return blob;
  }

  function syncSheetFiltersFromInputs() {
    var active = Object.create(null);
    arrSlice
      .call($view.querySelectorAll("[data-filter-key].sheet-col-filter"))
      .forEach(function (el) {
        var fk = el.getAttribute("data-filter-key");
        if (!fk) return;
        if (el.type === "checkbox") return;
        var need = filterHaystackPiece(el.value);
        if (!need) return;
        active[fk] = { filterKind: "substr", needle: need };
      });
    var enumAcc = Object.create(null);
    arrSlice
      .call($view.querySelectorAll("input.sheet-enum-filter-cb[data-filter-key]"))
      .forEach(function (ecb) {
        var fkEb = ecb.getAttribute("data-filter-key");
        if (!fkEb || !ecb.checked) return;
        var slugV = filterHaystackPiece(
          ecb.value == null ? "" : String(ecb.value),
        );
        if (!slugV) return;
        if (!enumAcc[fkEb]) enumAcc[fkEb] = [];
        enumAcc[fkEb].push(slugV);
      });
    var fkEn;
    for (fkEn in enumAcc) {
      if (!Object.prototype.hasOwnProperty.call(enumAcc, fkEn)) continue;
      if (enumAcc[fkEn].length)
        active[fkEn] = { filterKind: "enumAny", vals: enumAcc[fkEn] };
    }
    return active;
  }

  function applySheetFilters() {
    if (!$view) return;
    var active = syncSheetFiltersFromInputs();
    var rows = arrSlice.call($view.querySelectorAll("tbody tr.sheet-row"));
    var total = rows.length;
    var vis = 0;
    var pinned = 0;
    var fkList = Object.keys(active);
    var hasFilter = fkList.length > 0;
    var ri;
    for (ri = 0; ri < rows.length; ri++) {
      var tr = rows[ri];
      if (tr.getAttribute("data-sheet-pinned-new") === "1") {
        tr.style.display = "";
        vis++;
        pinned++;
        continue;
      }
      if (!hasFilter) {
        tr.style.display = "";
        vis++;
        continue;
      }
      var raw = tr.getAttribute("data-sheet-fblob");
      if (!raw) {
        tr.style.display = "none";
        continue;
      }
      var blob;
      try {
        blob = JSON.parse(decodeURIComponent(raw));
      } catch (e) {
        tr.style.display = "";
        vis++;
        continue;
      }
      var ok = true;
      var fi;
      for (fi = 0; fi < fkList.length; fi++) {
        var fk = fkList[fi];
        var spec = active[fk];
        if (!spec || !spec.filterKind) continue;
        if (spec.filterKind === "substr") {
          var haySub = blob[fk];
          if (
            haySub == null ||
            String(haySub).indexOf(spec.needle) === -1
          ) {
            ok = false;
            break;
          }
          continue;
        }
        if (spec.filterKind === "enumAny") {
          if (fk === "domain") {
            if (
              !rowSlugSetIntersects(spec.vals, [
                blob.__domainSlug || "",
              ])
            ) {
              ok = false;
              break;
            }
            continue;
          }
          if (fk === "release_type") {
            if (
              !rowSlugSetIntersects(spec.vals, [
                blob.__releaseSlug || "",
              ])
            ) {
              ok = false;
              break;
            }
            continue;
          }
          if (fk === "country") {
            if (
              !rowSlugSetIntersects(spec.vals, [
                blob.__countrySlug || "",
              ])
            ) {
              ok = false;
              break;
            }
            continue;
          }
          if (fk === "markers") {
            if (
              !rowSlugSetIntersects(
                spec.vals,
                Array.isArray(blob.__markersSlugs)
                  ? blob.__markersSlugs
                  : [],
              )
            ) {
              ok = false;
              break;
            }
            continue;
          }
          if (
            typeof fk === "string" &&
            fk.indexOf(FMT_SORT_PREFIX) === 0
          ) {
            var grpMap =
              blob.__fmtGroupSlugs && typeof blob.__fmtGroupSlugs === "object"
                ? blob.__fmtGroupSlugs
                : null;
            var rowGrps =
              grpMap && Object.prototype.hasOwnProperty.call(grpMap, fk)
                ? grpMap[fk]
                : [];
            if (!rowSlugSetIntersects(spec.vals, rowGrps)) {
              ok = false;
              break;
            }
            continue;
          }
          ok = false;
          break;
        }
      }
      tr.style.display = ok ? "" : "none";
      if (ok) vis++;
    }
    var hint = document.getElementById("sheet-filter-hint");
    scheduleSheetColumnFit();
    if (!hint) return;
    var pinnedHint = pinned ? "待保存新增 " + pinned + " 行已置顶（不受筛选影响）" : "";
    if (!hasFilter || !total) {
      hint.textContent = pinnedHint;
      return;
    }
    hint.textContent =
      "筛选后可见 " +
      vis +
      " / " +
      total +
      " 行（列标题右侧 ▼：枚举勾选任一；文本为包含；清空该列即取消条件）" +
      (pinnedHint ? "；" + pinnedHint : "");
  }

  function syncFilterPersistedFromEnumKey(k) {
    var valsAcc = [];
    var allCb = arrSlice.call(
      $view.querySelectorAll("input.sheet-enum-filter-cb[data-filter-key]"),
    );
    var ic;
    for (ic = 0; ic < allCb.length; ic++) {
      if (String(allCb[ic].getAttribute("data-filter-key")) !== String(k))
        continue;
      if (!allCb[ic].checked) continue;
      var vv = filterHaystackPiece(
        allCb[ic].value == null ? "" : String(allCb[ic].value),
      );
      if (vv) valsAcc.push(vv);
    }
    if (valsAcc.length) persistedSheetFilters[k] = JSON.stringify(valsAcc);
    else delete persistedSheetFilters[k];
  }

  function syncFilterPersistedFromEl(t) {
    var kEl = t.getAttribute("data-filter-key");
    if (!kEl) return;
    if (filterHaystackPiece(t.value)) persistedSheetFilters[kEl] = t.value;
    else delete persistedSheetFilters[kEl];
  }

  function bindSheetFiltersOnce() {
    if (sheetFilterListenersBound) return;
    sheetFilterListenersBound = true;
    function onFilter(ev) {
      var tf = ev.target;
      if (!tf || !tf.getAttribute || typeof tf.closest !== "function") return;
      if (!$view.contains(tf)) return;
      if (tf.classList && tf.classList.contains("sheet-enum-filter-cb")) {
        var kk = tf.getAttribute("data-filter-key");
        if (!kk) return;
        syncFilterPersistedFromEnumKey(kk);
        applySheetFilters();
        return;
      }
      if (!tf.classList || !tf.classList.contains("sheet-col-filter"))
        return;
      if (!tf.getAttribute("data-filter-key")) return;
      syncFilterPersistedFromEl(tf);
      applySheetFilters();
    }
    listen($view, "input", onFilter);
    listen($view, "change", onFilter);
  }

  function bindPressFmtVisibilityOnce() {
    if (pressFmtVisBound) return;
    pressFmtVisBound = true;
    listen($view, "change", function (evPv) {
      var tPv = evPv.target;
      if (
        !tPv ||
        !tPv.classList ||
        !tPv.classList.contains("press-fmt-vis-cb")
      ) {
        return;
      }
      if (!$view.contains(tPv)) return;
      var nextMap = Object.create(null);
      arrSlice.call($view.querySelectorAll(".press-fmt-vis-cb")).forEach(
        function (cbPv) {
          var slPv = cbPv.getAttribute("data-press-fmt-slug");
          if (cbPv.checked && slPv != null && String(slPv).trim())
            nextMap[String(slPv).trim()] = true;
        },
      );
      savePressSplitVisibleMap(nextMap);
      if (lastBrowsePayload && lastBrowsePayload.ok) {
        renderPayload(lastBrowsePayload, true);
      }
    });
  }

  function renderFormatColumnCell(
    row,
    fm,
    rowIndexNum,
    yamlRelEsc,
    readOnlyWhenSplitVisible,
  ) {
    yamlRelEsc = yamlRelEsc == null ? "" : String(yamlRelEsc);
    readOnlyWhenSplitVisible = !!readOnlyWhenSplitVisible;
    var want = fm == null ? "" : String(fm).trim();
    if (!want) return '<span class="cell-empty">—</span>';

    var thLabFmt = browseEnumDisplay(FMT, want);
    if (thLabFmt === "—") thLabFmt = want;

    var entries = collectPressFmtEntriesForWant(row, want);

    function gpDispPlainFmtCell(gpTr) {
      if (!gpTr) return "—";
      var bare = browseEnumDisplay(GRP, gpTr);
      return bare === "—" ? String(gpTr).trim() : bare;
    }

    var maxGpSlots = 0;
    var mi;
    for (mi = 0; mi < entries.length; mi++) {
      maxGpSlots = Math.max(
        maxGpSlots,
        aggDispSlots(gpDispPlainFmtCell(entries[mi].gpTrim)),
      );
    }

    let h =
      '<div class="tag-row" aria-label="' +
      esc(thLabFmt) +
      '">';
    const tint = pressFormatTintKey(want);
    var slotFmt = pressFmtAggHueSlot(want);
    var baseCls = "pill pill-pair fmt-agg-s" + slotFmt;
    var lastContBi = null;
    var ki;
    for (ki = 0; ki < entries.length; ki++) {
      var itCell = entries[ki].itObj;
      var gpTrim = entries[ki].gpTrim;
      var gpPlainFm = gpDispPlainFmtCell(gpTrim);
      var gpPlainPadFm = aggPadTrailingToSlots(gpPlainFm, maxGpSlots);

      const isContin = itCell.segment === "continuation";
      var tipStr = "";
      if (isContin) {
        var partsCt = ["续行"];
        if (itCell.continuation_title)
          partsCt.push(String(itCell.continuation_title));
        partsCt.push(String(want) + " / " + (gpTrim || "—"));
        partsCt.push(pressConnectionTooltipText(row, itCell));
        tipStr = esc(partsCt.join(" · "));
        var biIx =
          typeof itCell.continuation_index === "number"
            ? itCell.continuation_index
            : 0;
        if (lastContBi !== null && biIx !== lastContBi) {
          h += '<span class="pill-gap" aria-hidden="true"></span>';
        }
        lastContBi = biIx;
      } else {
        lastContBi = null;
        tipStr = esc(
          "主行 · " +
            String(want) +
            " / " +
            (gpTrim || "—") +
            " · " +
            pressConnectionTooltipText(row, itCell),
        );
      }

      h +=
        '<span class="' +
        baseCls +
        pressConnectionClass(row, itCell) +
        '" data-fmt-tint="' +
        esc(tint) +
        '" title="' +
        tipStr +
        '"' +
        (sheetEditMode && !readOnlyWhenSplitVisible
          ? pressItemEditAttrs(rowIndexNum, yamlRelEsc, entries[ki]._sourceOrd, "split", want)
          : "") +
        '">';
      if (sheetEditMode && !readOnlyWhenSplitVisible) {
        h +=
          '<span class="pill-fmt-plain-pad pill-equal-pre">' +
          esc(gpPlainPadFm) +
          "</span>";
        h += renderPressItemDeleteButton(rowIndexNum, yamlRelEsc, entries[ki]._sourceOrd);
      } else {
        h +=
          '<span class="pill-fmt-plain-pad pill-equal-pre">' +
          esc(gpPlainPadFm) +
          "</span>";
      }
      h += renderPressOpenFolderButton(row, itCell);
      h += "</span>";
    }

    if (!entries.length) return '<span class="cell-empty">—</span>';
    return h + "</div>";
  }

  /** 压制汇总：分列顺序与各「压制类型」列一致；同格式内压制组顺序与分列 cell 一致；色槽按格式 slug 区分；同行 pill 用空格拉齐等效宽度 */
  function renderPressAggregateColumnCell(
    row,
    fmCols,
    splitVisMap,
    rowIndexNum,
    yamlRelEsc,
  ) {
    yamlRelEsc = yamlRelEsc == null ? "" : String(yamlRelEsc);
    splitVisMap = splitVisMap || {};
    fmCols = fmCols || [];

    var specs = gatherPressAggSpecs(row, fmCols, splitVisMap);
    if (!specs.length) {
      if (sheetEditMode) {
        return (
          '<div class="tag-row tag-row-press-agg" aria-label="压制汇总">' +
          '<span class="cell-empty">—</span>' +
          renderPressAddButton(rowIndexNum, yamlRelEsc) +
          "</div>"
        );
      }
      return '<span class="cell-empty">—</span>';
    }

    var maxSlots = 0;
    var si;
    for (si = 0; si < specs.length; si++) {
      maxSlots = Math.max(maxSlots, aggDispSlots(specs[si].plainLine));
    }

    var rowOpen = '<div class="tag-row tag-row-press-agg" aria-label="压制汇总">';
    var hInner = "";

    var lastContBiAg = null;
    var qi;
    for (qi = 0; qi < specs.length; qi++) {
      var sp = specs[qi];
      var itCellAg = sp.itCellAg;
      var fmSlugP = sp.fmSlugP;
      var gpTrimP = sp.gpTrimP;
      var dispFm = sp.dispFm;
      var plainLinePad = aggPadTrailingToSlots(sp.plainLine, maxSlots);

      const isContinAg = itCellAg.segment === "continuation";
      var tipStrAg = "";
      if (isContinAg) {
        var partsCtAg = ["续行"];
        if (itCellAg.continuation_title)
          partsCtAg.push(String(itCellAg.continuation_title));
        partsCtAg.push(String(fmSlugP) + " / " + (gpTrimP || "—"));
        partsCtAg.push(pressConnectionTooltipText(row, itCellAg));
        tipStrAg = esc(partsCtAg.join(" · "));
        var biIxAg =
          typeof itCellAg.continuation_index === "number"
            ? itCellAg.continuation_index
            : 0;
        if (lastContBiAg !== null && biIxAg !== lastContBiAg) {
          hInner += '<span class="pill-gap" aria-hidden="true"></span>';
        }
        lastContBiAg = biIxAg;
      } else {
        lastContBiAg = null;
        tipStrAg = esc(
          "主行 · " +
            String(fmSlugP) +
            " / " +
            (gpTrimP || "—") +
            " · " +
            pressConnectionTooltipText(row, itCellAg),
        );
      }

      var tintAg = pressFormatTintKey(fmSlugP);
      var slotAg = pressFmtAggHueSlot(fmSlugP);
      var splitOn = sp.splitOn;
      var aggKindCls = sheetEditMode ? " pill-press-agg-ed" : " pill-press-agg-plain";
      var baseClsAg =
        "pill pill-pair pill-press-agg fmt-agg-s" +
        slotAg +
        aggKindCls +
        pressConnectionClass(row, itCellAg);

      hInner +=
        '<span class="' +
        baseClsAg +
        '" data-fmt-tint="' +
        esc(tintAg) +
        '" title="' +
        tipStrAg +
        '"' +
        (sheetEditMode
          ? pressItemEditAttrs(rowIndexNum, yamlRelEsc, sp._sourceOrd, "full", "")
          : "") +
        '">';
      hInner +=
        '<span class="pill-agg-fmt-gp pill-equal-pre">' +
        esc(plainLinePad) +
        "</span>";
      hInner += renderPressOpenFolderButton(row, itCellAg);
      hInner += "</span>";
    }

    return rowOpen + hInner + renderPressAddButton(rowIndexNum, yamlRelEsc) + "</div>";
  }

  function renderMarkerSelects(markers, rowIndexNum, yamlRelEsc) {
    yamlRelEsc = yamlRelEsc == null ? "" : String(yamlRelEsc);
    markers = markers || [];
    if (!Array.isArray(markers) || !markers.length) {
      return '<span class="cell-empty">—</span>';
    }
    var stackCls = sheetEditMode
      ? "tag-row markers-enum-stack"
      : "tag-row markers-plain-stack";
    let h = '<div class="' + stackCls + '" aria-label="Markers">';
    markers.forEach(function (m, mi) {
      if (typeof m !== "string" || !String(m).trim()) return;
      const mv = String(m).trim();
      var mkExtras = "";
      if (sheetEditMode && rowIndexNum != null && !Number.isNaN(Number(rowIndexNum))) {
        mkExtras =
          'data-sheet-iif="' +
          esc(String(rowIndexNum)) +
          '" data-yaml-rel="' +
          yamlRelEsc +
          '" data-marker-i="' +
          String(mi) +
          '" ';
      }
      h +=
        '<div class="enum-stack-cell">' +
        '<span class="pill pill-marker" title="' +
        esc(mv) +
        '">' +
        renderEnumSelect("markers", mv, "sheet-select-marker", mkExtras) +
        "</span>" +
        "</div>";
    });
    h += "</div>";
    return h;
  }

  /** 每条作品表格一行；「压制汇总」+ 可按勾选拆分为各格式分列（默认只有汇总） */
  function renderFlatTable(groups) {
    bindSheetSortingOnce();
    bindSheetFilterPopoversOnce();
    bindPressFmtVisibilityOnce();
    bindSheetInlineEditOnce();
    if (sheetSortKey === "idx") {
      sheetSortKey = null;
    }
    if ($chkEdit) sheetEditMode = !!$chkEdit.checked;

    const flat = buildBrowseFlatPacks(groups);
    const fmCols = derivePressFormatColumns(flat);
    sheetRenderFmColsRef = fmCols;
    var rawVisPick = loadPressSplitVisibleMap();
    var visMap = sanitizePressSplitVisForCols(fmCols, rawVisPick);
    savePressSplitVisibleMap(visMap);

    if (
      sheetSortKey &&
      typeof sheetSortKey === "string" &&
      sheetSortKey.indexOf(FMT_SORT_PREFIX) === 0 &&
      sheetSortKey.length > FMT_SORT_PREFIX.length
    ) {
      var encPeek = sheetSortKey.slice(FMT_SORT_PREFIX.length);
      var decSlugPeek = "";
      try {
        decSlugPeek = decodeURIComponent(encPeek);
      } catch (ePkPeek) {
        decSlugPeek = "";
      }
      if (!decSlugPeek || !visMap[decSlugPeek]) {
        sheetSortKey = null;
      }
    }

    flat.sort(compareFlatPack);

    var fmColsVisible = fmCols.filter(function (fmVc) {
      return visMap[fmVc] === true;
    });
    const colCount = 8 + fmColsVisible.length;
    let h =
      '<div class="sheet-wrap"><table class="sheet' +
      (sheetEditMode ? " sheet-editing" : "") +
      '" role="table">' +
      renderSheetColGroup(fmColsVisible) +
      "<thead><tr>" +
      sortThWithFilter(
        "domain",
        "domain",
        "大类",
        "domain",
        "大类…",
      ) +
      sortThWithFilter(
        "release_type",
        "release_type",
        "发行形态",
        "release_type",
        "发行形态…",
      ) +
      sortThWithFilter("date_start", "date_start", "开播", null, "开播…") +
      sortThWithFilter("date_end", "date_end", "完结", null, "完结…") +
      sortThWithFilter("country", "country", "国家", "country", "国家…") +
      sortThWithFilter(
        "name",
        "name",
        "作品",
        null,
        "作品 / profile / 数据文件名…",
      ) +
      sortThWithFilter(
        "markers",
        "markers",
        "收集标记",
        "markers",
        "标记…",
      ) +
      sortThWithFilter(
        SHEET_PRESS_AGG_SORT_KEY,
        null,
        "压制汇总",
        null,
        "压制格式 / 组 / 续行…",
        fmCols.length
          ? { fmCols: fmCols, visMap: visMap }
          : null,
      );

    var fchi;
    for (fchi = 0; fchi < fmColsVisible.length; fchi++) {
      const fmSlugRt = fmColsVisible[fchi];
      var skFmt =
        FMT_SORT_PREFIX +
        encodeURIComponent(fmSlugRt == null ? "" : String(fmSlugRt));
      var fmHeadLblRt = browseEnumDisplay(FMT, fmSlugRt);
      if (fmHeadLblRt === "—") fmHeadLblRt = String(fmSlugRt);
      var phFmRt = browseEnumDisplay(FMT, fmSlugRt);
      if (phFmRt === "—") phFmRt = String(fmSlugRt);
      h += sortThWithFilter(skFmt, null, fmHeadLblRt, GRP, phFmRt + " 压制组…");
    }
    h += '</tr></thead><tbody>';

    let i;
    for (i = 0; i < flat.length; i++) {
      const pack = flat[i];
      const r = pack.row;
      const d = r.date || {};
      const iifStr = String(Number(r.index_in_file));
      var yrAttr = esc(String(r.yaml_source_rel == null ? "" : r.yaml_source_rel));
      const enumRowAttr =
        'data-sheet-iif="' + esc(iifStr) + '" data-yaml-rel="' + yrAttr + '"';
      var fbAttr = encodeURIComponent(
        JSON.stringify(buildRowFilterBlob(pack, fmCols)),
      );

      h +=
        '<tr class="sheet-row' +
        (r._isNew ? " sheet-row-new" : "") +
        '" data-sheet-pinned-new="' + (isUnsavedNewRow(r) ? "1" : "0") +
        '" data-sheet-iif="' +
        esc(iifStr) +
        '" data-yaml-rel="' +
        yrAttr +
        '" data-sheet-fblob="' +
        fbAttr +
        '">';
      h +=
        '<td class="col-tax" data-col-key="domain" title="' +
        esc(pack.profile_label) +
        '">' +
        renderEnumSelect(
          "domain",
          r.domain,
          "",
          enumRowAttr + ' data-field="domain"',
        ) +
        "</td>";
      h +=
        '<td class="col-tax" data-col-key="release_type">' +
        renderEnumSelect(
          "release_type",
          r.release_type,
          "",
          enumRowAttr + ' data-field="release_type"',
        ) +
        "</td>";
      if (!sheetEditMode) {
        h +=
          '<td class="col-date" data-col-key="date_start">' + esc(displayCollectionDate(d.start)) + "</td>" +
          '<td class="col-date" data-col-key="date_end">' + esc(displayCollectionDate(d.end)) + "</td>";
      } else {
        h +=
          '<td class="col-date" data-col-key="date_start">' +
          renderSheetScalarField("date_start", d.start || "", r.index_in_file, yrAttr) +
          "</td>" +
          '<td class="col-date" data-col-key="date_end">' +
          renderSheetScalarField("date_end", d.end || "", r.index_in_file, yrAttr) +
          "</td>";
      }
      var rowDeleteInline = "";
      if (sheetEditMode) {
        rowDeleteInline =
          '<button type="button" class="row-delete-btn row-delete-inline-btn" title="删除此行" aria-label="删除此行" data-action="delete-row" data-sheet-iif="' +
          esc(iifStr) +
          '" data-yaml-rel="' +
          yrAttr +
          '">删除</button>';
      }
      h +=
        '<td class="col-tax" data-col-key="country">' +
        renderEnumSelect(
          "country",
          r.country,
          "",
          enumRowAttr + ' data-field="country"',
        ) +
        '</td><td class="col-name" data-col-key="name" title="' +
        esc(String(r.name || "")) +
        '">' +
        renderWorkNameField(r, yrAttr) +
        (r._isNew ? '<span class="sheet-new-work-badge">' +
          (r._persistedPendingRefresh ? "已保存，待刷新" : "待保存") + '</span>' : "") +
        rowDeleteInline +
        "</td>";
      h +=
        '<td class="cell-tags" data-col-key="markers">' +
        renderMarkerSelects(r.markers || [], r.index_in_file, yrAttr) +
        "</td>" +
        '<td class="cell-tags cell-tags-press cell-press-aggregate" data-col-key="' +
        esc(SHEET_PRESS_AGG_SORT_KEY) +
        '">' +
        renderPressAggregateColumnCell(
          r,
          fmCols,
          visMap,
          r.index_in_file,
          yrAttr,
        ) +
        "</td>";
      for (fchi = 0; fchi < fmColsVisible.length; fchi++) {
        var fmCellKey = FMT_SORT_PREFIX + encodeURIComponent(String(fmColsVisible[fchi] == null ? "" : fmColsVisible[fchi]));
        h +=
          '<td class="cell-tags cell-tags-press" data-col-key="' +
          esc(fmCellKey) +
          '">' +
          renderFormatColumnCell(
            r,
            fmColsVisible[fchi],
            r.index_in_file,
            yrAttr,
            false,
          ) +
          "</td>";
      }
      h += "</tr>";
    }

    if (!flat.length) {
      h +=
        '<tr><td colspan="' +
        String(colCount) +
        "\" class=\"cell-empty\">—</td></tr>";
    }

    h += "</tbody></table></div>";
    return h;
  }

  var arrSlice = Array.prototype.slice;
  var browseSaveBound = false;
  var browseSaving = false;
  var browseLoading = false;

  function beginBrowseLoad() {
    if (browseSaving || browseLoading) {
      setStatus("正在保存或载入数据，请完成后再切换数据源。", true);
      return false;
    }
    browseLoading = true;
    syncSaveToolbar();
    return true;
  }

  function finishBrowseLoad() {
    browseLoading = false;
    syncSaveToolbar();
  }

  function syncSaveToolbar() {
    if (!$btnSaveYaml) return;
    sheetEditMode = $chkEdit ? !!$chkEdit.checked : false;
    if ($chkEdit) $chkEdit.disabled = browseSaving || browseLoading;
    if (browseSaving || browseLoading) {
      $btnSaveYaml.disabled = true;
      $btnSaveYaml.title = browseSaving ? "正在保存，请勿重复提交" : "正在载入数据";
      if ($btnAddRow) $btnAddRow.disabled = true;
      if ($btnEnumEditor) $btnEnumEditor.disabled = true;
      return;
    }
    if (context.featureHost.getActiveId() !== "collection-detail") {
      $btnSaveYaml.disabled = true;
      $btnSaveYaml.title = "";
      if ($btnAddRow) {
        $btnAddRow.disabled = true;
        $btnAddRow.title = "";
      }
      if ($btnEnumEditor) {
        $btnEnumEditor.disabled = true;
        $btnEnumEditor.title = "";
      }
      return;
    }
    if ($btnEnumEditor) {
      $btnEnumEditor.disabled = !sheetEditMode;
      $btnEnumEditor.title = sheetEditMode ? "编辑压制格式 / 压制组枚举" : "请先勾选编辑模式";
    }
    if ($btnAddRow) {
      var addAllowedWithoutPayload = hasWritableDbConfigForNewRow();
      $btnAddRow.disabled = !addAllowedWithoutPayload;
      $btnAddRow.title = addAllowedWithoutPayload
        ? "打开新增作品表单，确认后加入待保存列表"
        : "请先读取包含 paths.filesystem_root 的配置";
    }
    if (!lastBrowsePayload || !lastBrowsePayload.ok) {
      $btnSaveYaml.disabled = true;
      $btnSaveYaml.title = "";
      return;
    }
    var sg = lastBrowsePayload.save;
    var allow = !!(sg && sg.enabled);
    $btnSaveYaml.disabled = !sheetEditMode || !allow;
    if ($btnAddRow) {
      $btnAddRow.disabled = !allow;
      $btnAddRow.title = allow
        ? "打开新增作品表单，确认后加入待保存列表"
        : ((sg && sg.reason) || "不可新增");
    }
    if (allow) {
      if (sg.multi_file && sg.target_paths && sg.target_paths.length > 1) {
        $btnSaveYaml.title =
          "写入 " +
          sg.target_paths.length +
          " 个 YAML（按数据文件拆分保存）";
      } else {
        $btnSaveYaml.title = sg.target_path ? "写入：" + sg.target_path : "保存到磁盘";
      }
    } else {
      $btnSaveYaml.title = (sg && sg.reason) || "不可保存";
    }
  }

  function gatherBrowseSaveRows() {
    var rows = [];
    var newRows = [];
    if (!lastBrowsePayload || !lastBrowsePayload.profile_groups) {
      return { rows: rows, new_rows: newRows };
    }
    var groups = lastBrowsePayload.profile_groups;
    var gix;
    for (gix = 0; gix < groups.length; gix++) {
      var gx = groups[gix];
      var rowList = gx.rows || [];
      var rj;
      for (rj = 0; rj < rowList.length; rj++) {
        var r = rowList[rj];
        var iif = parseInt(String(r.index_in_file), 10);
        if (!Number.isFinite(iif)) continue;
        var ysr = typeof r.yaml_source_rel === "string" ? r.yaml_source_rel : "";
        var tr = $view.querySelector(
          'tr.sheet-row[data-sheet-iif="' +
            esc(String(iif)) +
            '"][data-yaml-rel="' +
            esc(ysr) +
            '"]',
        );
        if (!tr && !r._isNew) continue;
        // New drafts remain part of the save even if a view/filter did not
        // materialize their row. The payload is their source of truth.
        if (!tr) tr = { querySelector: function () { return null; }, querySelectorAll: function () { return []; } };

        var sd = tr.querySelector('input[data-field="date_start"]');
        var ed = tr.querySelector('input[data-field="date_end"]');
        var nmNode = tr.querySelector('input[data-field="name"]');
        var sdSel = tr.querySelector('select[data-field="domain"]');
        var rtSel = tr.querySelector('select[data-field="release_type"]');
        var cySel = tr.querySelector('select[data-field="country"]');

        var ordered = [];
        try {
          ordered = JSON.parse(JSON.stringify(r.collectioned_ordered || []));
        } catch (_unused) {
          ordered = [];
        }

        arrSlice
          .call(tr.querySelectorAll("select.sheet-select-pair[data-source-ord][data-press-fmt]"))
          .forEach(function (sel) {
            var ordStr = sel.getAttribute("data-source-ord");
            var fmCol = sel.getAttribute("data-press-fmt") || "";
            var ord = parseInt(ordStr, 10);
            if (Number.isNaN(ord) || ord < 0) return;
            if (!ordered[ord] || typeof ordered[ord] !== "object") return;
            ordered[ord][FMT] = fmCol;
            ordered[ord][GRP] = sel.value == null ? "" : String(sel.value).trim();
          });

        var markersOut;
        var markerNodes = arrSlice.call(tr.querySelectorAll("select[data-marker-i]"));
        if (markerNodes.length) {
          markerNodes.sort(function (a, b) {
            var na = parseInt(a.getAttribute("data-marker-i"), 10);
            var nb = parseInt(b.getAttribute("data-marker-i"), 10);
            return (Number.isNaN(na) ? 0 : na) - (Number.isNaN(nb) ? 0 : nb);
          });
          markersOut = [];
          var mk;
          for (mk = 0; mk < markerNodes.length; mk++) {
            var vx = markerNodes[mk].value == null ? "" : String(markerNodes[mk].value).trim();
            if (vx) markersOut.push(vx);
          }
        } else {
          markersOut = Array.isArray(r.markers) ? r.markers.slice() : [];
        }

        var patch = {
          index_in_file: iif,
          yaml_source_rel: ysr,
          domain: sdSel ? String(sdSel.value).trim() : String(r.domain || "").trim(),
          release_type: rtSel ? String(rtSel.value).trim() : String(r.release_type || "").trim(),
          country: cySel ? String(cySel.value).trim() : String(r.country || "").trim(),
          name: nmNode ? String(nmNode.value) : String(r.name || ""),
          date: {
            start: sd ? String(sd.value || "").trim() : ((r.date && r.date.start) || ""),
            end: ed ? String(ed.value || "").trim() : ((r.date && r.date.end) || ""),
          },
          markers: markersOut,
          collectioned_ordered: ordered,
          path: typeof r.path === "string" ? r.path : "",
        };
        if (r._isNew) {
          newRows.push(patch);
        } else {
          var sourceHash = sourceHashForRow(r);
          if (sourceHash) patch.source_sha256 = sourceHash;
          rows.push(patch);
        }
      }
    }
    return { rows: rows, new_rows: newRows };
  }

  function bindBrowseSaveOnce() {
    if (browseSaveBound || !$btnSaveYaml) return;
    browseSaveBound = true;
    listen($btnSaveYaml, "click", function () {
      void doSaveBrowseYaml();
    });
  }

  async function doSaveBrowseYaml() {
    if (browseSaving || browseLoading) return;
    bindBrowseSaveOnce();
    if (!lastBrowsePayload || !lastBrowsePayload.save || !lastBrowsePayload.save.enabled) {
      var r0 =
        lastBrowsePayload && lastBrowsePayload.save && lastBrowsePayload.save.reason
          ? lastBrowsePayload.save.reason
          : "当前不可保存（请通过「加载DB数据」按钮浮层载入）。";
      setStatus(r0, true);
      return;
    }
    if (!sheetEditMode) {
      setStatus("请先勾选「编辑模式」再保存。", true);
      return;
    }
    var collected;
    var newRows;
    var deletedRows;
    try {
      var gathered = gatherBrowseSaveRows();
      collected = gathered.rows || [];
      newRows = gathered.new_rows || [];
      deletedRows = deletedRowsForSave();
    } catch (err) {
      setStatus("收集表格数据失败：" + (err.message || String(err)), true);
      return;
    }
    if (!collected.length && !newRows.length && !deletedRows.length) {
      setStatus(
        "当前没有收集到可保存的数据变更。请先通过「加载DB数据」浮层载入 DB 数据后再保存；不要使用仅上传预览。仍异常请 Ctrl+F5。",
        true,
      );
      return;
    }
    /* path 不写进 body：始终以服务端配置的 resolved YAML 为准，避免 Windows 盘符大小写等与 JSON 比对失败 */
    var body = { rows: collected, new_rows: newRows, deleted_rows: deletedRows };
    browseSaving = true;
    syncSaveToolbar();
    var previousInert = $view.inert;
    $view.inert = true;
    setStatus("保存中…", false);
    try {
      var out = await fetchJson("/api/browse/save", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify(body),
      });
      if (!out.data || !out.data.ok) {
        setStatus(
          (out.data && out.data.error)
            ? String(out.data.error)
            : "保存失败（HTTP " + out.res.status + "）",
          true,
        );
        return;
      }
      var w = out.data.writes || [];
      var msgHist =
        w.length === 1
          ? String(w[0].history_file || "新建文件")
          : w.length + " 个备份文件（History）";
      // A confirmed save must never leave its old new_rows executable when
      // the following reload fails; retrying them would append duplicates.
      lastBrowsePayload.save.enabled = false;
      lastBrowsePayload.save.reason = "本次数据已写入。请重新加载 DB 数据后再编辑，避免重复新增。";
      (lastBrowsePayload.profile_groups || []).forEach(function (group) {
        (group.rows || []).forEach(function (row) {
          if (row._isNew) row._persistedPendingRefresh = true;
        });
      });
      if (newRows.length) {
        lastDbCatalogLoadedPaths = null;
      }
      if (await reloadBrowseAfterSave()) {
        setStatus("已保存 · " + msgHist + " · 已刷新列表。", false);
      } else {
        renderPayload(lastBrowsePayload, true);
        setStatus("数据已经保存，但刷新列表失败。请使用「加载DB数据」重新载入，不要重复新增。", true);
      }
    } catch (err2) {
      setStatus("保存请求异常：" + (err2.message || String(err2)), true);
    } finally {
      browseSaving = false;
      $view.inert = previousInert;
      syncSaveToolbar();
    }
  }

  /** @param preserveSheetSort 为 true 时保留当前排序列与升降序（表头排序、勾选编辑模式重绘时使用） */
  function renderPayload(data, preserveSheetSort) {
    if (disposed) return;
    if (!data.ok) {
      lastBrowsePayload = null;
      sheetSortKey = null;
      sheetSortDir = 1;
      persistedSheetFilters = Object.create(null);
      deletedSheetRows = Object.create(null);
      enumEditorDraft = null;
      if ($enumEditorPanel) $enumEditorPanel.hidden = true;
      setStatus(data.error || "未知错误", true);
      $view.innerHTML = "";
      syncSaveToolbar();
      return;
    }
    lastBrowsePayload = data;
    if (!preserveSheetSort) {
      sheetSortKey = null;
      sheetSortDir = 1;
      persistedSheetFilters = Object.create(null);
      deletedSheetRows = Object.create(null);
      enumEditorDraft = null;
      if ($enumEditorPanel) $enumEditorPanel.hidden = true;
    }
    setStatus("", false);
    const groups = data.profile_groups || [];
    if (!groups.length) {
      setStatus("无数据分组", false);
      $view.innerHTML = "";
      syncSaveToolbar();
      return;
    }
    var saveNote = "";
    if (data.save && !data.save.enabled && data.save.reason) {
      saveNote =
        '<p class="save-blocked-banner" role="status">' +
        esc(String(data.save.reason)) +
        "</p>";
    }
    const hdr =
      saveNote +
      '<span id="sheet-filter-hint" class="sheet-filter-hint" aria-live="polite"></span>';
    const blk = "profile-block profile-wrap animation-tv";
    $view.innerHTML =
      '<section class="' +
      blk +
      '">' +
      hdr +
      renderFlatTable(groups) +
      "</section>";
    bindBrowseSaveOnce();
    bindSheetAddRowOnce();
    bindSheetEditActionsOnce();
    bindEnumEditorOnce();
    bindSheetFiltersOnce();
    bindSheetColumnResizeOnce();
    applySheetFilters();
    scheduleSheetColumnFit();
    syncSaveToolbar();
  }

  async function fetchJson(url, opts) {
    return context.fetchJson(url, opts);
  }

  function browseHttpFailHint(status, verb) {
    return window.NimdaCommon.httpErrorHint(status, verb);
  }

  function clearYamlPickUi() {
    yamlPickFiles = [];
    if ($yamlPickPanel) $yamlPickPanel.hidden = true;
    if ($yamlPickList) $yamlPickList.innerHTML = "";
    if ($yamlPickCount) $yamlPickCount.textContent = "0";
  }

  function setYamlPickCheckAll(on) {
    if (!$yamlPickList) return;
    var cbs = $yamlPickList.querySelectorAll("input.yaml-pick-cb");
    var ci;
    for (ci = 0; ci < cbs.length; ci++) cbs[ci].checked = !!on;
  }

  function refreshYamlPickFromInput() {
    if (!$yamlPickPanel || !$yamlPickList || !$yamlPickCount) return;
    var fl = $file && $file.files ? $file.files : null;
    if (!fl || !fl.length) {
      clearYamlPickUi();
      return;
    }
    yamlPickFiles = Array.prototype.slice.call(fl, 0);
    $yamlPickCount.textContent = String(yamlPickFiles.length);
    $yamlPickList.innerHTML = "";
    var ix;
    for (ix = 0; ix < yamlPickFiles.length; ix++) {
      var f = yamlPickFiles[ix];
      var li = document.createElement("li");
      li.className = "yaml-pick-row";
      var lab = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "yaml-pick-cb";
      cb.checked = true;
      cb.dataset.idx = String(ix);
      var sp = document.createElement("span");
      sp.className = "yaml-pick-name";
      sp.textContent = f.name + "（" + f.size + " B）";
      lab.appendChild(cb);
      lab.appendChild(sp);
      li.appendChild(lab);
      $yamlPickList.appendChild(li);
    }
    $yamlPickPanel.hidden = false;
  }

  function collectCheckedYamlFiles() {
    if (!$yamlPickList || !yamlPickFiles.length) return [];
    var out = [];
    var cbs = $yamlPickList.querySelectorAll("input.yaml-pick-cb");
    var ci;
    for (ci = 0; ci < cbs.length; ci++) {
      if (!cbs[ci].checked) continue;
      var ix = parseInt(String(cbs[ci].getAttribute("data-idx") || ""), 10);
      if (!Number.isNaN(ix) && yamlPickFiles[ix]) out.push(yamlPickFiles[ix]);
    }
    return out;
  }

  async function uploadYamlFiles(fileArr) {
    if (!fileArr || !fileArr.length) return;
    if (!beginBrowseLoad()) return;
    try {
      await loadServerConfig().catch(function () {});
      var sumB = 0;
      var ni;
      for (ni = 0; ni < fileArr.length; ni++) sumB += fileArr[ni].size || 0;
      $meta.textContent =
        fileArr.length === 1
          ? fileArr[0].name + "（" + fileArr[0].size + " B）"
          : fileArr.length + " 个文件 · 共 " + sumB + " B";
      setStatus("解析中…", false);
      $view.innerHTML = "";
      var fd = new FormData();
      var fi;
      for (fi = 0; fi < fileArr.length; fi++) {
        fd.append("file", fileArr[fi], fileArr[fi].name);
      }
      const out = await fetchJson("/api/browse", { method: "POST", body: fd });
      if (!out.res.ok && !out.data.error) {
        setStatus("HTTP " + out.res.status, true);
        return;
      }
      renderPayload(out.data);
      if (out.data && out.data.ok) {
        lastDbCatalogLoadedPaths = null;
        clearRememberedDbCatalogLoad();
        clearYamlPickUi();
        if ($file) $file.value = "";
        closeDbCatalogPopover();
      }
    } finally {
      finishBrowseLoad();
    }
  }

  async function loadServerConfig() {
    const out = await fetchJson("/api/config");
    if (disposed) return;
    const c = out.data;
    lastServerConfig = c && typeof c === "object" ? c : null;
    setBrowseEnumOptions(c.enum_options || {});
    setBrowseEnumLabels(c.enum_labels || {});
    setBrowseEnumSectionLabels(c.enum_section_labels || {});
    context.featureHost.configure(c.app || {});
    document.getElementById("cfg-used-path").value = c.config_used || "";
    document.getElementById("cfg-fs-root").value = (c.paths && c.paths.filesystem_root) || "";
    var crls = (c.paths && c.paths.catalog_yaml_relpaths) || [];
    lastCatalogYamlRels = Array.isArray(crls) ? crls.slice() : [];
    document.getElementById("cfg-history-root").value =
      (c.paths && c.paths.history_root) || "";
    var linkCfg = c.link_index || {};
    var linkShortcut = document.getElementById("cfg-link-shortcut-root");
    var linkLayout = document.getElementById("cfg-link-layout");
    var linkName = document.getElementById("cfg-link-name");
    if (linkShortcut) {
      var linkRoots = Array.isArray(linkCfg.shortcut_roots)
        ? linkCfg.shortcut_roots
        : [linkCfg.shortcut_root];
      linkShortcut.value = linkRoots
        .map(function (root) { return String(root || "").trim(); })
        .filter(Boolean)
        .join(" / ");
      linkShortcut.title = linkShortcut.value;
    }
    if (linkLayout) {
      linkLayout.value = Array.isArray(linkCfg.layout_levels)
        ? linkCfg.layout_levels.join(" / ")
        : "";
    }
    if (linkName) linkName.value = linkCfg.shortcut_name || "";

    var cfgHelpEl = document.getElementById("cfg-help");
    if (cfgHelpEl) {
      cfgHelpEl.textContent = c.help || (c.error ? String(c.error) : "") || "";
    }
    const en = !!(c.default_load && c.default_load.enabled);
    $btnDefault.disabled = !en;
    if ($btnDbCatalogRun) $btnDbCatalogRun.disabled = !en;
    if ($selDbCatalogLoadMode) $selDbCatalogLoadMode.disabled = !en;
    if (!en) closeDbCatalogPopover();
    if (out.res.status >= 400 && c.error) {
      setStatus("配置解析失败：" + String(c.error), true);
    }
    syncSaveToolbar();
    context.featureHost.refresh();
  }

  function renderDbCatalogList() {
    if (!$dbCatalogList || !$dbCatalogTotal) return;
    var rels = lastCatalogYamlRels || [];
    var remembered = readRememberedDbCatalogLoad();
    var rememberedSet = Object.create(null);
    if (remembered && remembered.mode === "paths") {
      remembered.paths.forEach(function (rel) {
        rememberedSet[rel] = true;
      });
    }
    $dbCatalogTotal.textContent = String(rels.length);
    $dbCatalogList.innerHTML = "";
    var ix;
    for (ix = 0; ix < rels.length; ix++) {
      var rel = normalizedCatalogRelKey(String(rels[ix] || ""));
      if (!rel) continue;
      var li = document.createElement("li");
      li.className = "yaml-pick-row";
      var lab = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.className = "yaml-pick-cb db-catalog-cb";
      cb.checked = remembered && remembered.mode === "all" ? true : !!rememberedSet[rel];
      cb.dataset.rel = rel;
      var sp = document.createElement("span");
      sp.className = "yaml-pick-name";
      sp.textContent = rel;
      lab.appendChild(cb);
      lab.appendChild(sp);
      li.appendChild(lab);
      $dbCatalogList.appendChild(li);
    }
  }

  function setDbCatalogCheckAll(on) {
    if (!$dbCatalogList) return;
    var cbs = $dbCatalogList.querySelectorAll("input.db-catalog-cb");
    var ci;
    for (ci = 0; ci < cbs.length; ci++) cbs[ci].checked = !!on;
  }

  function normalizedCatalogRelKey(s) {
    return String(s == null ? "" : s).replace(/\\/g, "/").trim().replace(/^\/+/g, "");
  }

  function currentDbCatalogScope() {
    return String(
      (lastServerConfig && lastServerConfig.paths && lastServerConfig.paths.filesystem_root) || "",
    );
  }

  function availableDbCatalogRelSet() {
    var set = Object.create(null);
    var rels = lastCatalogYamlRels || [];
    var i;
    for (i = 0; i < rels.length; i++) {
      var key = normalizedCatalogRelKey(rels[i]);
      if (key) set[key] = true;
    }
    return set;
  }

  function filterAvailableDbCatalogRels(paths) {
    var set = availableDbCatalogRelSet();
    var out = [];
    var seen = Object.create(null);
    var arr = Array.isArray(paths) ? paths : [];
    var i;
    for (i = 0; i < arr.length; i++) {
      var key = normalizedCatalogRelKey(arr[i]);
      if (!key || !set[key] || seen[key]) continue;
      seen[key] = true;
      out.push(key);
    }
    return out;
  }

  function rememberDbCatalogLoad(mode, paths) {
    try {
      var rec = {
        mode: mode === "all" ? "all" : "paths",
        paths: mode === "all" ? [] : filterAvailableDbCatalogRels(paths),
        filesystem_root: currentDbCatalogScope(),
        saved_at: new Date().toISOString(),
      };
      if (rec.mode === "paths" && !rec.paths.length) return;
      window.NimdaCommon.writePreference(LS_KEY_LAST_DB_CATALOG_LOAD, JSON.stringify(rec));
    } catch (_) {}
  }

  function clearRememberedDbCatalogLoad() {
    window.NimdaCommon.writePreference(LS_KEY_LAST_DB_CATALOG_LOAD, null);
  }

  function readRememberedDbCatalogLoad() {
    var raw = window.NimdaCommon.readPreference(LS_KEY_LAST_DB_CATALOG_LOAD) || "";
    if (!raw) return null;
    var rec = null;
    try {
      rec = JSON.parse(raw);
    } catch (_) {
      clearRememberedDbCatalogLoad();
      return null;
    }
    if (!rec || typeof rec !== "object") return null;
    if (String(rec.filesystem_root || "") !== currentDbCatalogScope()) return null;
    if (rec.mode === "all") return { mode: "all", paths: [] };
    var paths = filterAvailableDbCatalogRels(rec.paths);
    if (!paths.length) {
      clearRememberedDbCatalogLoad();
      return null;
    }
    return { mode: "paths", paths: paths };
  }

  /** 按列表从上到下收集已勾选的相对路径（与配置字符串一致） */
  function collectCheckedDbCatalogRels() {
    if (!$dbCatalogList) return [];
    var out = [];
    var cbs = $dbCatalogList.querySelectorAll("input.db-catalog-cb");
    var ci;
    for (ci = 0; ci < cbs.length; ci++) {
      if (!cbs[ci].checked) continue;
      var r = cbs[ci].getAttribute("data-rel");
      var nk = normalizedCatalogRelKey(r);
      if (nk !== "") out.push(nk);
    }
    return out;
  }

  /** 主区尚无表格时的说明 */
  function paintDbCatalogWelcomeViewport() {
    if (!$view) return;
    $view.innerHTML =
      '<p class="muted browse-db-hint browse-db-hint-placeholder" aria-hidden="true">&nbsp;</p>';
  }

  async function autoRestoreLastDbCatalogLoad() {
    if (dbCatalogAutoRestoreAttempted) return false;
    dbCatalogAutoRestoreAttempted = true;
    if (!$btnDefault || $btnDefault.disabled) return false;
    var rec = readRememberedDbCatalogLoad();
    if (!rec) return false;
    if (rec.mode === "all") {
      await loadAllDbCatalogFromDefault({ autoRestore: true });
      return true;
    }
    await loadDbCatalogPathsFromApi(rec.paths, { autoRestore: true });
    return true;
  }

  /** 浮层内：下拉载入范围 +「载入」—— POST catalog 子集或 GET default 整库 */
  function runDbCatalogLoadFromUi() {
    var mode =
      $selDbCatalogLoadMode && $selDbCatalogLoadMode.value
        ? String($selDbCatalogLoadMode.value)
        : "picked";
    if (mode === "all") {
      return loadAllDbCatalogFromDefault();
    }
    return loadCheckedDbCatalogFromApi();
  }

  async function loadCheckedDbCatalogFromApi() {
    var paths = collectCheckedDbCatalogRels();
    if (!paths.length) {
      setStatus("请至少勾选一个 DB 数据文件。", true);
      return;
    }
    return loadDbCatalogPathsFromApi(paths, {});
  }

  async function loadDbCatalogPathsFromApi(paths, opts) {
    opts = opts || {};
    paths = filterAvailableDbCatalogRels(paths);
    if (!paths.length) {
      setStatus(opts.autoRestore ? "上次 DB 数据文件不在当前配置中，已跳过自动加载。" : "请至少勾选一个 DB 数据文件。", !!opts.autoRestore);
      if (opts.autoRestore) clearRememberedDbCatalogLoad();
      return;
    }
    if (!beginBrowseLoad()) return;
    setStatus(opts.autoRestore ? "自动加载上次 DB 数据…" : "从 DB 读取所选数据…", false);
    $view.innerHTML = "";
    try {
      var out = await fetchJson("/api/browse/catalog", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify({ paths: paths }),
      });
      if (!out.res.ok || !out.data.ok) {
        setStatus(out.data.error || browseHttpFailHint(out.res.status, "加载"), true);
        return;
      }
      lastDbCatalogLoadedPaths = paths.slice();
      rememberDbCatalogLoad("paths", paths);
      clearYamlPickUi();
      if ($file) $file.value = "";
      renderPayload(out.data);
      closeDbCatalogPopover();
    } catch (errCat) {
      setStatus("加载请求异常：" + (errCat.message || String(errCat)), true);
    } finally {
      finishBrowseLoad();
    }
  }

  function renderSavedBrowsePayload(data) {
    // Keep the user's sort/filter choices, but never carry old record indexes
    // in deletion or enum drafts across a successful catalog reload.
    deletedSheetRows = Object.create(null);
    enumEditorDraft = null;
    if ($enumEditorPanel) $enumEditorPanel.hidden = true;
    renderPayload(data, true);
  }

  /** 保存成功后刷新：与子集载入一致则用 POST catalog，否则退回整库 GET default */
  async function reloadBrowseAfterSave() {
    try {
      if (lastDbCatalogLoadedPaths && lastDbCatalogLoadedPaths.length > 0) {
        var outSub = await fetchJson("/api/browse/catalog", {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify({ paths: lastDbCatalogLoadedPaths }),
        });
        if (!outSub.res.ok || !outSub.data.ok) {
          setStatus(outSub.data.error || browseHttpFailHint(outSub.res.status, "刷新"), true);
          return false;
        }
        renderSavedBrowsePayload(outSub.data);
        return true;
      }
      setStatus("从 DB 重新读取数据中…", false);
      var outAll = await fetchJson("/api/browse/default", { method: "GET" });
      if (!outAll.res.ok || !outAll.data.ok) {
        setStatus(outAll.data.error || browseHttpFailHint(outAll.res.status, "刷新"), true);
        return false;
      }
      renderSavedBrowsePayload(outAll.data);
      return true;
    } catch (errRf) {
      setStatus("刷新请求异常：" + (errRf.message || String(errRf)), true);
      return false;
    }
  }

  /** 与服务端 GET /api/browse/default 对齐：整库载入，保存刷新走整库 */
  async function loadAllDbCatalogFromDefault(opts) {
    opts = opts || {};
    if (!$btnDefault || $btnDefault.disabled) {
      setStatus("当前配置无法从 DB 载入数据。", true);
      return;
    }
    if (!beginBrowseLoad()) return;
    clearYamlPickUi();
    if ($file) $file.value = "";
    setStatus(opts.autoRestore ? "自动加载上次 DB 数据（全部）…" : "从 DB 读取全部数据…", false);
    $view.innerHTML = "";
    try {
      var outAllDb = await fetchJson("/api/browse/default", { method: "GET" });
      if (!outAllDb.res.ok || !outAllDb.data.ok) {
        setStatus(
          outAllDb.data.error || browseHttpFailHint(outAllDb.res.status, "加载"),
          true,
        );
        return;
      }
      lastDbCatalogLoadedPaths = null;
      rememberDbCatalogLoad("all", []);
      renderPayload(outAllDb.data);
      closeDbCatalogPopover();
    } catch (eAllDb) {
      setStatus("加载请求异常：" + (eAllDb.message || String(eAllDb)), true);
    } finally {
      finishBrowseLoad();
    }
  }

  function mount() {
    if (disposed || mounted) return;
    mounted = true;
  listen($file, "change", function () {
    refreshYamlPickFromInput();
    var fl = $file.files;
    if (fl && fl.length === 1) {
      uploadYamlFiles([fl[0]]).catch(function (e) {
        setStatus("加载失败：" + (e.message || String(e)), true);
      });
    }
  });

  if ($btnYamlPickAll) {
    listen($btnYamlPickAll, "click", function () {
      setYamlPickCheckAll(true);
    });
  }
  if ($btnYamlPickNone) {
    listen($btnYamlPickNone, "click", function () {
      setYamlPickCheckAll(false);
    });
  }
  if ($btnYamlPickLoad) {
    listen($btnYamlPickLoad, "click", function () {
      var chosen = collectCheckedYamlFiles();
      if (!chosen.length) {
        setStatus("请至少勾选一个 YAML 文件", true);
        return;
      }
      uploadYamlFiles(chosen).catch(function (e) {
        setStatus("加载失败：" + (e.message || String(e)), true);
      });
    });
  }

  listen($btnCfg, "click", function () {
    loadServerConfig()
      .then(function () {
        renderDbCatalogList();
      })
      .catch(function (e) {
        setStatus("读取配置失败：" + e, true);
      });
  });

  if ($chkEdit) {
    listen($chkEdit, "change", function () {
      if (!$chkEdit.checked && $enumEditorPanel) {
        $enumEditorPanel.hidden = true;
      }
      if (lastBrowsePayload && lastBrowsePayload.ok) {
        renderPayload(lastBrowsePayload, true);
      } else {
        syncSaveToolbar();
      }
    });
  }

  listen($btnDefault, "click", function (ev) {
    ev.stopPropagation();
    toggleDbYearbookPopoverFromPrimaryBtn().catch(function (e) {
      setStatus("数据浮层：" + (e.message || String(e)), true);
    });
  });

  if ($btnDbCatalogAll) {
    listen($btnDbCatalogAll, "click", function () {
      setDbCatalogCheckAll(true);
    });
  }
  if ($btnDbCatalogNone) {
    listen($btnDbCatalogNone, "click", function () {
      setDbCatalogCheckAll(false);
    });
  }
  if ($btnDbCatalogRun) {
    listen($btnDbCatalogRun, "click", function () {
      Promise.resolve(runDbCatalogLoadFromUi()).catch(function (e) {
        setStatus("加载失败：" + (e.message || String(e)), true);
      });
    });
  }

  relocateConfigPanelToCollectionDetailTab();
  bindDbCatalogPopoverDismissOnce();
  bindSheetAddRowOnce();

  loadServerConfig()
    .then(async function () {
      renderDbCatalogList();
      var restored = await autoRestoreLastDbCatalogLoad();
      if (!restored && $view && !String($view.innerHTML || "").trim()) {
        paintDbCatalogWelcomeViewport();
      }
    })
    .catch(function (e) {
      setStatus("读取配置失败：" + e, true);
      renderDbCatalogList();
      if ($view && !String($view.innerHTML || "").trim()) {
        paintDbCatalogWelcomeViewport();
      }
    });


  /** 从往返缓存恢复时，整页状态含筛选框；强制清空列筛并重绘 */
  listen(window, "pageshow", function (ev) {
    if (!ev.persisted) return;
    persistedSheetFilters = Object.create(null);
    if (lastBrowsePayload && lastBrowsePayload.ok) {
      renderPayload(lastBrowsePayload, true);
    }
  });
  }

  function handleAppearanceChange() {
    persistedSheetFilters = Object.create(null);
    if (lastBrowsePayload && lastBrowsePayload.ok) renderPayload(lastBrowsePayload, true);
  }

  function getFeatureContext() {
    return {
      config: { enum_options: browseEnumOptions, enum_labels: browseEnumLabels },
      arrSlice: arrSlice,
      browseEnumDisplay: browseEnumDisplay,
      browseEnumOptions: function () { return browseEnumOptions; },
      browseHttpFailHint: browseHttpFailHint,
      collectionView: context.collectionView,
      defaultEnumValue: defaultEnumValue,
      enumSectionTh: enumSectionTh,
      esc: esc,
      fetchJson: fetchJson,
      loadServerConfig: loadServerConfig,
      setStatus: setStatus,
      syncSaveToolbar: syncSaveToolbar,
    };
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    persistentListeners.splice(0).forEach(function (remove) { remove(); });
    if (sheetColumnFitRaf) cancelAnimationFrame(sheetColumnFitRaf);
    if (sheetFilterReposRaf) cancelAnimationFrame(sheetFilterReposRaf);
    closePressEditor();
    closeDbCatalogPopover();
  }

  return { mount: mount, dispose: dispose, getFeatureContext: getFeatureContext,
    handleAppearanceChange: handleAppearanceChange, closeDbCatalogPopover: closeDbCatalogPopover,
    syncSaveToolbar: syncSaveToolbar };
  }

  window.NimdaCollectionTableController = { create: createTableController };
})();
