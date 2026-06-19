(function () {
  const THEME_KEY = "jp-tv-browse-theme";
  const FONT_KEY = "jp-tv-browse-font";
  const THEME_IDS = [
    "midnight",
    "paper",
    "forest",
    "rose",
    "contrast",
    "ocean",
    "sunset",
    "slate",
    "sakura",
  ];
  const FONT_IDS = ["reference", "system", "yahei", "song", "mono"];

  function applyTheme(id) {
    const t = THEME_IDS.indexOf(id) >= 0 ? id : "midnight";
    if (t === "midnight") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", t);
    }
    try {
      localStorage.setItem(THEME_KEY, t);
    } catch (e) {}
    const sel = document.getElementById("theme-select");
    if (sel && sel.value !== t) {
      sel.value = t;
    }
  }

  (function applyStoredThemeEarly() {
    try {
      var st = localStorage.getItem(THEME_KEY);
      applyTheme(st && THEME_IDS.indexOf(st) >= 0 ? st : "midnight");
    } catch (e) {
      applyTheme("midnight");
    }
  })();

  function applyFont(id) {
    const f = FONT_IDS.indexOf(id) >= 0 ? id : "reference";
    document.documentElement.setAttribute("data-font", f);
    try {
      localStorage.setItem(FONT_KEY, f);
    } catch (e) {}
    const sel = document.getElementById("font-select");
    if (sel && sel.value !== f) {
      sel.value = f;
    }
  }

  (function applyStoredFontEarly() {
    try {
      var st = localStorage.getItem(FONT_KEY);
      applyFont(st && FONT_IDS.indexOf(st) >= 0 ? st : "reference");
    } catch (e) {
      applyFont("reference");
    }
  })();

  function initTheme() {
    const sel = document.getElementById("theme-select");
    if (sel) {
      sel.addEventListener("change", function () {
        applyTheme(sel.value);
        persistedSheetFilters = Object.create(null);
        if (lastBrowsePayload && lastBrowsePayload.ok) {
          renderPayload(lastBrowsePayload, true);
        }
      });
    }
  }

  function initFont() {
    const sel = document.getElementById("font-select");
    if (sel) {
      sel.addEventListener("change", function () {
        applyFont(sel.value);
      });
      applyFont(sel.value);
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
  const $status = document.getElementById("status-line");
  const $view = document.getElementById("viewport");
  const $collectionView = document.getElementById("collection-info-view");
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
  var activeAppTab = "collection-detail";
  var appTabsBound = false;
  var appFeaturesInitialized = false;
  var appFeatureConfigById = Object.create(null);

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
    document.addEventListener("click", function (ev) {
      if (!$dbCatalogPopover || $dbCatalogPopover.hidden) return;
      if ($dbCatalogAnchor && $dbCatalogAnchor.contains(ev.target)) return;
      closeDbCatalogPopover();
    });
    document.addEventListener("keydown", function (evk) {
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

  /** 表格列排序：null 表示按数据文件 + 文件内 index_in_file；同一列再次点击切换升序/降序 */
  let sheetSortKey = null;
  let sheetSortDir = 1;
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
        return nzForSort(d.start || "");
      case "date_end":
        return nzForSort(d.end || "");
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

  /** 空缺排后：empty 视作小于非空时在 asc 中会沉底 — 需在比较里单独处理 */
  function compareFlatPack(a, b) {
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
      '<th scope="col" class="sheet-sort-col sheet-th-combo">' +
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
    $view.addEventListener("click", function (ev) {
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
    window.addEventListener("resize", scheduleSheetFilterPopoverRepos);
    window.addEventListener(
      "scroll",
      scheduleSheetFilterPopoverRepos,
      true,
    );
    document.addEventListener("click", function (evDoc) {
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
    document.addEventListener("keydown", function (evK) {
      if (evK.key !== "Escape") return;
      closeAllSheetFilterPopovers();
    });
  }

  function bindSheetSortingOnce() {
    if (sheetSortEventsBound) return;
    sheetSortEventsBound = true;
    $view.addEventListener("click", function (ev) {
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
    $view.addEventListener("keydown", function (ev) {
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
    return { fm: pick(FMT), gp: pick(GRP) };
  }

  function setStatus(msg, isErr) {
    $status.textContent = msg || "";
    $status.className = "status" + (isErr ? " err" : "");
  }

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /** name / date / 作品名等在编辑模式下的输入框 */
  function renderSheetScalarField(fieldKey, rawVal, rowIndexNum, yamlRelEsc) {
    yamlRelEsc = yamlRelEsc == null ? "" : String(yamlRelEsc);
    var cur = rawVal == null ? "" : String(rawVal);
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
      esc(cur) +
      "</span>"
    );
    var clsCombined = esc("sheet-field-input sheet-field-scalar sheet-inp-" + fieldKey);
    return (
      '<input type="text" class="' +
      clsCombined +
      '" autocomplete="off" spellcheck="false" data-sheet-iif="' +
      sid +
      '" data-yaml-rel="' +
      yamlRelEsc +
      '" data-field="' +
      esc(fieldKey) +
      '" value="' +
      esc(cur) +
      '" />'
    );
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
    var v = rawVal == null ? "" : String(rawVal).trim();
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

  function currentYearCatalogRelForNewRow() {
    var y = new Date().getFullYear();
    return "[JP][TVInfo][" + String(y) + "].yaml";
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

  function ensurePayloadForNewRow() {
    if (
      lastBrowsePayload &&
      lastBrowsePayload.ok &&
      lastBrowsePayload.save &&
      lastBrowsePayload.save.enabled
    ) {
      return true;
    }
    if (!hasWritableDbConfigForNewRow()) return false;
    var rel = currentYearCatalogRelForNewRow();
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
        help: "新增行会保存到当前日期所在年份的数据文件。",
      },
    };
    lastDbCatalogLoadedPaths = null;
    deletedSheetRows = Object.create(null);
    return true;
  }

  function addPayloadRow() {
    if (!ensurePayloadForNewRow()) return false;
    var rel = currentYearCatalogRelForNewRow();
    var row = {
      _isNew: true,
      index_in_file: nextSyntheticIndexForRel(rel),
      yaml_source_rel: rel,
      domain: defaultEnumValue("domain", "animation"),
      release_type: defaultEnumValue("release_type", "tv"),
      country: defaultEnumValue("country", "japan"),
      name: "",
      path: "",
      date: { start: "", end: "" },
      markers: [],
      collectioned_ordered: [],
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

  function renderPressLinkEditButton(rowIndexNum, yamlRelEsc, sourceOrd, pressPath) {
    var path = pressPath == null ? "" : String(pressPath).trim();
    var label = path ? "已连" : "未连";
    var title = path ? "连接路径：" + path : "未配置连接路径";
    if (!sheetEditMode) {
      return (
        '<span class="press-link-edit-btn press-link-state' +
        (path ? " is-linked" : " is-empty") +
        '" title="' +
        esc(title) +
        '">' +
        esc(label) +
        "</span>"
      );
    }
    return (
      '<button type="button" class="press-link-edit-btn press-link-state' +
      (path ? " is-linked" : " is-empty") +
      '" title="' +
      esc(title + "。点击编辑") +
      '" aria-label="编辑连接路径" data-action="edit-press-link" data-sheet-iif="' +
      esc(String(Number(rowIndexNum))) +
      '" data-yaml-rel="' +
      yamlRelEsc +
      '" data-source-ord="' +
      esc(String(sourceOrd)) +
      '">' +
      esc(label) +
      "</button>"
    );
  }

  function pressPathTooltipText(pressPath) {
    var path = pressPath == null ? "" : String(pressPath).trim();
    return path ? "连接：" + path : "无连接";
  }

  function pressAggregateLinkEditAttrs(rowIndexNum, yamlRelEsc, sourceOrd) {
    if (!sheetEditMode) return "";
    return (
      ' data-action="edit-press-link" data-sheet-iif="' +
      esc(String(Number(rowIndexNum))) +
      '" data-yaml-rel="' +
      yamlRelEsc +
      '" data-source-ord="' +
      esc(String(sourceOrd)) +
      '"'
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

  function enumValueTuplesForEditor(enumKey, currentValue) {
    var opts = browseEnumOptions[enumKey];
    var out = [];
    var seen = Object.create(null);
    var i;
    if (Array.isArray(opts)) {
      for (i = 0; i < opts.length; i++) {
        var v = rawEnumOptSlug(opts[i]);
        if (!v || seen[v]) continue;
        seen[v] = true;
        out.push({ value: v, label: browseEnumDisplay(enumKey, v) });
      }
    }
    var cur = currentValue == null ? "" : String(currentValue).trim();
    if (cur && !seen[cur]) {
      out.unshift({ value: cur, label: browseEnumDisplay(enumKey, cur) });
    }
    return out;
  }

  function enumSelectForPressEditor(enumKey, currentValue, id, disabled) {
    var cur = currentValue == null ? "" : String(currentValue).trim();
    var vals = enumValueTuplesForEditor(enumKey, cur);
    if (!cur && vals.length) cur = vals[0].value;
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
    var curGrp = item[GRP] || defaultEnumValue(GRP, "");
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

  function editPayloadPressLink(yamlRel, indexInFile, sourceOrd) {
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
    var curWorkPath = typeof hit.row.path === "string" ? hit.row.path : "";
    var nextWorkPath = window.prompt("作品父路径（相对媒体根目录）", curWorkPath);
    if (nextWorkPath === null) return true;
    var curPressPath =
      typeof ordered[ord].press_path === "string" ? ordered[ord].press_path : "";
    var nextPressPath = window.prompt("压制路径（相对作品父路径）", curPressPath);
    if (nextPressPath === null) return true;
    hit.row.path = String(nextWorkPath || "").trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    ordered[ord].press_path = String(nextPressPath || "").trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
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
    var cur = trigger.getAttribute("data-current-value") || "";
    var opts = browseEnumOptions[enumKey];
    if (!Array.isArray(opts) || !opts.length) return "";
    var vals = opts.slice();
    var seen = {};
    var i;
    for (i = 0; i < vals.length; i++) seen[String(vals[i])] = true;
    if (cur && !seen[cur]) vals.unshift(cur);
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
      var v = String(vals[i]);
      h +=
        '<option value="' +
        esc(v) +
        '"' +
        (v === cur ? " selected" : "") +
        ">" +
        esc(browseEnumDisplay(enumKey, v)) +
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
      '" />';
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
    $view.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest || !sheetEditMode) return;
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
    $view.addEventListener("keydown", function (ev) {
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
    $view.addEventListener("change", function (ev) {
      var t = ev.target;
      if (!t || !t.matches || !t.matches("select.sheet-inline-select")) return;
      if (updatePayloadEnumFieldFromEl(t, t.value)) {
        renderPayload(lastBrowsePayload, true);
      }
    });
    $view.addEventListener(
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
        out.push(deletedSheetRows[k]);
      }
    }
    return out;
  }

  function bindSheetEditActionsOnce() {
    if (sheetEditActionsBound || !$view) return;
    sheetEditActionsBound = true;
    $view.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var btn = t.closest("[data-action]");
      if (!btn || !$view.contains(btn)) return;
      var action = btn.getAttribute("data-action");
      if (
        action !== "delete-row" &&
        action !== "delete-press-item" &&
        action !== "edit-press-link" &&
        action !== "edit-press-item" &&
        action !== "add-press-item"
      )
        return;
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
      if (action === "edit-press-link") {
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
    $btnAddRow.addEventListener("click", function () {
      if (!sheetEditMode) {
        setStatus("请先勾选「编辑模式」。", true);
        return;
      }
      var rel = addPayloadRow();
      if (!rel) {
        setStatus("当前数据不可新增行；请先通过「加载DB数据」载入可写 DB 数据。", true);
        return;
      }
      renderPayload(lastBrowsePayload, true);
      setStatus("已新增 1 行到 " + rel + "，保存后写入 YAML。", false);
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
      $btnEnumEditor.addEventListener("click", function () {
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
      $btnEnumClose.addEventListener("click", function () {
        if ($enumEditorPanel) $enumEditorPanel.hidden = true;
      });
    }
    if ($enumEditorPanel) {
      $enumEditorPanel.addEventListener("input", function (ev) {
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
      $enumEditorPanel.addEventListener("click", function (ev) {
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
      $btnEnumSave.addEventListener("click", function () {
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
      var s = localStorage.getItem(LS_KEY_PRESS_SPLIT_VISIBLE);
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
      localStorage.setItem(
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
    blob.date_start = filterHaystackPiece(d.start || "");
    blob.date_end = filterHaystackPiece(d.end || "");
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
    var fkList = Object.keys(active);
    var hasFilter = fkList.length > 0;
    var ri;
    for (ri = 0; ri < rows.length; ri++) {
      var tr = rows[ri];
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
    if (!hint) return;
    if (!hasFilter || !total) {
      hint.textContent = "";
      return;
    }
    hint.textContent =
      "筛选后可见 " +
      vis +
      " / " +
      total +
      " 行（列标题右侧 ▼：枚举勾选任一；文本为包含；清空该列即取消条件）";
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
    $view.addEventListener("input", onFilter);
    $view.addEventListener("change", onFilter);
  }

  function bindPressFmtVisibilityOnce() {
    if (pressFmtVisBound) return;
    pressFmtVisBound = true;
    $view.addEventListener("change", function (evPv) {
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
        partsCt.push(pressPathTooltipText(itCell.press_path));
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
            pressPathTooltipText(itCell.press_path),
        );
      }

      h +=
        '<span class="' +
        baseCls +
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
        partsCtAg.push(pressPathTooltipText(itCellAg.press_path));
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
            pressPathTooltipText(itCellAg.press_path),
        );
      }

      var tintAg = pressFormatTintKey(fmSlugP);
      var slotAg = pressFmtAggHueSlot(fmSlugP);
      var splitOn = sp.splitOn;
      var aggKindCls = sheetEditMode ? " pill-press-agg-ed" : " pill-press-agg-plain";
      var baseClsAg =
        "pill pill-pair pill-press-agg fmt-agg-s" +
        slotAg +
        aggKindCls;

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
      '" role="table"><thead><tr>' +
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
        '" data-sheet-iif="' +
        esc(iifStr) +
        '" data-yaml-rel="' +
        yrAttr +
        '" data-sheet-fblob="' +
        fbAttr +
        '">';
      h +=
        '<td class="col-tax" title="' +
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
        '<td class="col-tax">' +
        renderEnumSelect(
          "release_type",
          r.release_type,
          "",
          enumRowAttr + ' data-field="release_type"',
        ) +
        "</td>";
      if (!sheetEditMode) {
        h +=
          '<td class="col-date">' + esc(d.start || "") + "</td>" +
          '<td class="col-date">' + esc(d.end || "") + "</td>";
      } else {
        h +=
          '<td class="col-date">' +
          renderSheetScalarField("date_start", d.start || "", r.index_in_file, yrAttr) +
          "</td>" +
          '<td class="col-date">' +
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
        "<td>" +
        renderEnumSelect(
          "country",
          r.country,
          "",
          enumRowAttr + ' data-field="country"',
        ) +
        '</td><td class="col-name" title="' +
        esc(String(r.name || "")) +
        '">' +
        renderSheetScalarField("name", r.name || "", r.index_in_file, yrAttr) +
        rowDeleteInline +
        "</td>";
      h +=
        '<td class="cell-tags">' +
        renderMarkerSelects(r.markers || [], r.index_in_file, yrAttr) +
        "</td>" +
        '<td class="cell-tags cell-tags-press cell-press-aggregate">' +
        renderPressAggregateColumnCell(
          r,
          fmCols,
          visMap,
          r.index_in_file,
          yrAttr,
        ) +
        "</td>";
      for (fchi = 0; fchi < fmColsVisible.length; fchi++) {
        h +=
          '<td class="cell-tags cell-tags-press">' +
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

  function syncSaveToolbar() {
    if (!$btnSaveYaml) return;
    sheetEditMode = $chkEdit ? !!$chkEdit.checked : false;
    if (activeAppTab !== "collection-detail") {
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
      $btnAddRow.disabled = !sheetEditMode || !addAllowedWithoutPayload;
      $btnAddRow.title = addAllowedWithoutPayload
        ? "新增一行到当前日期所在年份的 DB 数据文件"
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
      $btnAddRow.disabled = !sheetEditMode || !allow;
      $btnAddRow.title = allow
        ? "新增一行到当前日期所在年份的 DB 数据文件"
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
        if (!tr) continue;

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
          rows.push(patch);
        }
      }
    }
    return { rows: rows, new_rows: newRows };
  }

  function bindBrowseSaveOnce() {
    if (browseSaveBound || !$btnSaveYaml) return;
    browseSaveBound = true;
    $btnSaveYaml.addEventListener("click", function () {
      void doSaveBrowseYaml();
    });
  }

  async function doSaveBrowseYaml() {
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
      setStatus(
        "已保存 · " + msgHist + " · 已刷新列表。",
        false,
      );
      if (newRows.length) {
        lastDbCatalogLoadedPaths = null;
      }
      await reloadBrowseAfterSave().catch(function () {});
    } catch (err2) {
      setStatus("保存请求异常：" + (err2.message || String(err2)), true);
    }
  }

  /** @param preserveSheetSort 为 true 时保留当前排序列与升降序（表头排序、勾选编辑模式重绘时使用） */
  function renderPayload(data, preserveSheetSort) {
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
    applySheetFilters();
    syncSaveToolbar();
  }

  function setAppFeatureConfig(appConfig) {
    var next = Object.create(null);
    var features =
      appConfig && Array.isArray(appConfig.features) ? appConfig.features : [];
    var i;
    for (i = 0; i < features.length; i++) {
      var item = features[i];
      if (!item || typeof item !== "object") continue;
      var id = String(item.id || "").trim();
      if (!id) continue;
      var cfg = {};
      if (item.label != null && String(item.label).trim() !== "") {
        cfg.label = String(item.label).trim();
      }
      var ord = Number(item.order);
      if (Number.isFinite(ord)) cfg.order = ord;
      next[id] = cfg;
    }
    appFeatureConfigById = next;
  }

  function mergeAppFeatureConfig(feature) {
    var cfg = appFeatureConfigById[feature.id] || {};
    var out = {};
    var k;
    for (k in feature) {
      if (Object.prototype.hasOwnProperty.call(feature, k)) out[k] = feature[k];
    }
    if (cfg.label) out.label = cfg.label;
    if (cfg.order != null) out.order = cfg.order;
    return out;
  }

  function registeredAppFeatures() {
    var reg = window.JpTvBrowseFeatureRegistry;
    var rawList = reg && Array.isArray(reg.features) ? reg.features : [];
    var list = [];
    var i;
    for (i = 0; i < rawList.length; i++) {
      if (rawList[i] && rawList[i].id) list.push(mergeAppFeatureConfig(rawList[i]));
    }
    list.sort(function (a, b) {
      return (Number(a.order) || 0) - (Number(b.order) || 0);
    });
    return list;
  }

  function appFeatureById(id) {
    var list = registeredAppFeatures();
    var i;
    for (i = 0; i < list.length; i++) {
      if (list[i].id === id) return list[i];
    }
    return null;
  }

  function appFeatureContext() {
    return {
      arrSlice: arrSlice,
      browseEnumDisplay: browseEnumDisplay,
      browseEnumOptions: function () {
        return browseEnumOptions;
      },
      browseHttpFailHint: browseHttpFailHint,
      collectionView: $collectionView,
      defaultEnumValue: defaultEnumValue,
      enumSectionTh: enumSectionTh,
      esc: esc,
      fetchJson: fetchJson,
      loadServerConfig: loadServerConfig,
      setStatus: setStatus,
      syncSaveToolbar: syncSaveToolbar,
    };
  }

  function initAppFeaturesOnce() {
    if (appFeaturesInitialized) return;
    appFeaturesInitialized = true;
    var list = registeredAppFeatures();
    var featureCtx = appFeatureContext();
    var i;
    for (i = 0; i < list.length; i++) {
      if (typeof list[i].init === "function") list[i].init(featureCtx);
    }
  }

  function refreshActiveFeatureAfterConfig() {
    var ft = appFeatureById(activeAppTab);
    if (ft && typeof ft.refreshAfterConfig === "function") {
      ft.refreshAfterConfig(appFeatureContext());
    }
  }

  function setTabButtonState() {
    var features = registeredAppFeatures();
    var tabsNav = document.querySelector(".app-tabs");
    var i;
    for (i = 0; i < features.length; i++) {
      var ft = features[i];
      var on = ft.id === activeAppTab;
      var btn = document.getElementById(ft.tabId);
      var viewEl = document.getElementById(ft.viewId);
      if (btn) {
        btn.classList.toggle("is-active", on);
        btn.classList.toggle("on", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
        if (ft.label) btn.textContent = ft.label;
        if (tabsNav && btn.parentElement === tabsNav) tabsNav.appendChild(btn);
      }
      if (viewEl) viewEl.hidden = !on;
    }
    document.body.setAttribute("data-active-tab", activeAppTab);
    syncSaveToolbar();
  }

  function setActiveTab(tabName) {
    initAppFeaturesOnce();
    var featureNext = appFeatureById(tabName) ? tabName : "collection-detail";
    if (activeAppTab === featureNext) return;
    var prevFeature = appFeatureById(activeAppTab);
    var nextFeature = appFeatureById(featureNext);
    var featureCtx = appFeatureContext();
    activeAppTab = featureNext;
    closeDbCatalogPopover();
    setTabButtonState();
    if (prevFeature && typeof prevFeature.deactivate === "function") {
      prevFeature.deactivate(featureCtx);
    }
    if (nextFeature && typeof nextFeature.activate === "function") {
      nextFeature.activate(featureCtx);
    }
  }

  function bindAppTabsOnce() {
    if (appTabsBound) return;
    appTabsBound = true;
    initAppFeaturesOnce();
    var features = registeredAppFeatures();
    var i;
    for (i = 0; i < features.length; i++) {
      (function (ft) {
        var btn = document.getElementById(ft.tabId);
        if (!btn) return;
        btn.addEventListener("click", function () {
          setActiveTab(ft.id);
        });
      })(features[i]);
    }
    if (!appFeatureById(activeAppTab)) {
      activeAppTab = features.length ? features[0].id : "collection-detail";
    }
    setTabButtonState();
  }

  function collectionDefaultRecord() {
    return {
      domain: defaultEnumValue("domain", "animation"),
      country: defaultEnumValue("country", "japan"),
      release_type: defaultEnumValue("release_type", "tv"),
      completed_years: [],
    };
  }

  function normalizeCollectionRecordForUi(raw) {
    var base = collectionDefaultRecord();
    raw = raw && typeof raw === "object" ? raw : {};
    return {
      domain: String(raw.domain || base.domain).trim() || base.domain,
      country: String(raw.country || base.country).trim() || base.country,
      release_type: String(raw.release_type || base.release_type).trim() || base.release_type,
      completed_years: Array.isArray(raw.completed_years)
        ? raw.completed_years.map(function (x) {
            return String(x || "").trim().toUpperCase();
          }).filter(Boolean)
        : [],
    };
  }

  function collectionRecordsForUi() {
    var records =
      collectionRecordsPayload && Array.isArray(collectionRecordsPayload.records)
        ? collectionRecordsPayload.records
        : [];
    if (!records.length) return [collectionDefaultRecord()];
    return records.map(normalizeCollectionRecordForUi);
  }

  function collectionYearsForUi() {
    var years =
      collectionRecordsPayload && Array.isArray(collectionRecordsPayload.years)
        ? collectionRecordsPayload.years
        : [];
    return years.filter(function (it) {
      return it && typeof it === "object" && String(it.key || "").trim();
    });
  }

  function renderCollectionEnumSelect(enumKey, cur, idx) {
    var opts = Array.isArray(browseEnumOptions[enumKey])
      ? browseEnumOptions[enumKey].slice()
      : [];
    var val = String(cur || "").trim();
    var seen = {};
    var i;
    for (i = 0; i < opts.length; i++) seen[String(opts[i])] = true;
    if (val && !seen[val]) opts.unshift(val);
    if (!opts.length && val) opts.push(val);
    var h =
      '<select class="collection-select" data-collection-field="' +
      esc(enumKey) +
      '" data-record-index="' +
      esc(String(idx)) +
      '">';
    for (i = 0; i < opts.length; i++) {
      var ov = String(opts[i]);
      h +=
        '<option value="' +
        esc(ov) +
        '"' +
        (ov === val ? " selected" : "") +
        ">" +
        esc(browseEnumDisplay(enumKey, ov)) +
        "</option>";
    }
    h += "</select>";
    return h;
  }

  function renderCollectionRecord(rec, idx, years, total) {
    var checked = {};
    var cy = Array.isArray(rec.completed_years) ? rec.completed_years : [];
    var i;
    for (i = 0; i < cy.length; i++) checked[String(cy[i]).toUpperCase()] = true;

    var h =
      '<div class="collection-record-item" data-record-index="' +
      esc(String(idx)) +
      '">' +
      '<div class="collection-record-item-head">' +
      '<h3 class="collection-record-item-title">情况 ' +
      esc(String(idx + 1)) +
      "</h3>";
    if (total > 1) {
      h +=
        '<button type="button" class="collection-record-delete" data-collection-action="delete-record" data-record-index="' +
        esc(String(idx)) +
        '">删除情况</button>';
    }
    h +=
      "</div>" +
      '<div class="collection-records-fields">' +
      '<label class="collection-field"><span>' +
      esc(enumSectionTh("domain", "归类")) +
      "</span>" +
      renderCollectionEnumSelect("domain", rec.domain, idx) +
      "</label>" +
      '<label class="collection-field"><span>' +
      esc(enumSectionTh("country", "国家")) +
      "</span>" +
      renderCollectionEnumSelect("country", rec.country, idx) +
      "</label>" +
      '<label class="collection-field"><span>' +
      esc(enumSectionTh("release_type", "发行类型")) +
      "</span>" +
      renderCollectionEnumSelect("release_type", rec.release_type, idx) +
      "</label>" +
      "</div>" +
      '<p class="collection-year-title">收集完成年份</p>';

    if (!years.length) {
      h += '<p class="collection-records-empty">没有扫描到可勾选年份。</p>';
    } else {
      h += '<div class="collection-year-grid">';
      for (i = 0; i < years.length; i++) {
        var y = years[i];
        var key = String(y.key || "").toUpperCase();
        var lab = String(y.label || key);
        h +=
          '<label class="collection-year-chip">' +
          '<input type="checkbox" class="collection-year-cb" data-year-key="' +
          esc(key) +
          '" data-record-index="' +
          esc(String(idx)) +
          '"' +
          (checked[key] ? " checked" : "") +
          " />" +
          "<span>" +
          esc(lab) +
          "</span></label>";
      }
      h += "</div>";
    }
    h += "</div>";
    return h;
  }

  function renderCollectionRecords(data) {
    if (!$collectionView) return;
    collectionRecordsPayload = data && typeof data === "object" ? data : {};
    var records = collectionRecordsForUi();
    var years = collectionYearsForUi();
    collectionRecordsPayload.records = records;
    var h =
      '<section class="collection-records-panel">' +
      '<div class="collection-records-head">' +
      "<div>" +
      '<h2 class="collection-records-title">收集情况</h2>' +
      '<p class="collection-records-meta">' +
      esc(collectionRecordsPayload.path || "collection-info.yaml") +
      "</p>" +
      "</div>" +
      '<div class="collection-records-actions">' +
      '<button type="button" class="btn secondary sm" data-collection-action="reload">重读情况</button>' +
      '<button type="button" class="btn secondary sm" data-collection-action="add-record">新增情况</button>' +
      '<button type="button" class="btn sm" data-collection-action="save">保存情况</button>' +
      "</div>" +
      "</div>";
    if (collectionRecordsPayload.warning) {
      h +=
        '<p class="collection-records-warning">' +
        esc(String(collectionRecordsPayload.warning)) +
        "</p>";
    }
    var i;
    for (i = 0; i < records.length; i++) {
      h += renderCollectionRecord(records[i], i, years, records.length);
    }
    h += "</section>";
    $collectionView.innerHTML = h;
  }

  async function loadCollectionRecords() {
    if (!$collectionView) return;
    await loadServerConfig().catch(function () {});
    setStatus("收集情况读取中...", false);
    $collectionView.innerHTML = "";
    var out = await fetchJson("/api/collection-info", { method: "GET" });
    if (!out.res.ok || !out.data || !out.data.ok) {
      setStatus(
        (out.data && out.data.error) || browseHttpFailHint(out.res.status, "收集情况读取"),
        true,
      );
      return;
    }
    renderCollectionRecords(out.data);
    setStatus("", false);
  }

  function gatherCollectionRecordsFromView() {
    if (!$collectionView) return [];
    var items = arrSlice.call($collectionView.querySelectorAll(".collection-record-item"));
    var out = [];
    var i;
    for (i = 0; i < items.length; i++) {
      var el = items[i];
      function field(name) {
        var node = el.querySelector('[data-collection-field="' + name + '"]');
        return node ? String(node.value || "").trim() : "";
      }
      var years = arrSlice
        .call(el.querySelectorAll("input.collection-year-cb:checked"))
        .map(function (cb) {
          return String(cb.getAttribute("data-year-key") || "").trim().toUpperCase();
        })
        .filter(Boolean);
      out.push({
        domain: field("domain"),
        country: field("country"),
        release_type: field("release_type"),
        completed_years: years,
      });
    }
    return out;
  }

  async function saveCollectionRecords() {
    var records = gatherCollectionRecordsFromView();
    if (!records.length) {
      setStatus("没有可保存的收集情况。", true);
      return;
    }
    setStatus("收集情况保存中...", false);
    var out = await fetchJson("/api/collection-info", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ records: records }),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      setStatus(
        (out.data && out.data.error) || browseHttpFailHint(out.res.status, "收集情况保存"),
        true,
      );
      return;
    }
    collectionRecordsPayload.records = out.data.records || records;
    setStatus("收集情况已保存。", false);
    await loadCollectionRecords().catch(function () {});
  }

  function addCollectionRecord() {
    if (!collectionRecordsPayload || typeof collectionRecordsPayload !== "object") {
      collectionRecordsPayload = { records: [] };
    }
    var records = gatherCollectionRecordsFromView();
    if (!records.length) records = collectionRecordsForUi();
    records.push(collectionDefaultRecord());
    collectionRecordsPayload.records = records;
    renderCollectionRecords(collectionRecordsPayload);
  }

  function deleteCollectionRecordAt(idx) {
    var records = gatherCollectionRecordsFromView();
    if (!records.length) records = collectionRecordsForUi();
    if (records.length <= 1) return;
    if (idx < 0 || idx >= records.length) return;
    records.splice(idx, 1);
    collectionRecordsPayload.records = records;
    renderCollectionRecords(collectionRecordsPayload);
  }

  function bindCollectionRecordsOnce() {
    if (collectionRecordsBound || !$collectionView) return;
    collectionRecordsBound = true;
    $collectionView.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var btn = t.closest("[data-collection-action]");
      if (!btn || !$collectionView.contains(btn)) return;
      var action = btn.getAttribute("data-collection-action");
      if (action === "reload") {
        loadCollectionRecords().catch(function (e) {
          setStatus("收集情况读取失败：" + (e.message || String(e)), true);
        });
        return;
      }
      if (action === "save") {
        saveCollectionRecords().catch(function (e) {
          setStatus("收集情况保存失败：" + (e.message || String(e)), true);
        });
        return;
      }
      if (action === "add-record") {
        addCollectionRecord();
        return;
      }
      if (action === "delete-record") {
        var idx = parseInt(btn.getAttribute("data-record-index"), 10);
        deleteCollectionRecordAt(idx);
      }
    });
  }

  async function fetchJson(url, opts) {
    const res = await fetch(url, opts || {});
    const data = await res.json().catch(function () {
      return {};
    });
    return { res, data };
  }

  /** HTTP 405/404 常为旧版服务端把 /api/* 交给静态目录处理；提示升级 browse 后端 */
  function browseHttpFailHint(status, verb) {
    var lab = verb || "加载";
    if (status === 405 || status === 404) {
      return (
        lab +
        "失败（HTTP " +
        status +
        "）：多为服务端过旧或未挂载 /api/browse/catalog；请在工程目录对 work-catalog-yaml 执行 pip install -e \".[web]\"（或等价方式）后重启 work-catalog jp-tv browse。"
      );
    }
    return lab + "失败（HTTP " + status + "）";
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
  }

  async function loadServerConfig() {
    const out = await fetchJson("/api/config");
    const c = out.data;
    lastServerConfig = c && typeof c === "object" ? c : null;
    setBrowseEnumOptions(c.enum_options || {});
    setBrowseEnumLabels(c.enum_labels || {});
    setBrowseEnumSectionLabels(c.enum_section_labels || {});
    setAppFeatureConfig(c.app || {});
    setTabButtonState();
    document.getElementById("cfg-used-path").value = c.config_used || "";
    document.getElementById("cfg-fs-root").value = (c.paths && c.paths.filesystem_root) || "";
    var crls = (c.paths && c.paths.catalog_yaml_relpaths) || [];
    lastCatalogYamlRels = Array.isArray(crls) ? crls.slice() : [];
    document.getElementById("cfg-history-root").value =
      (c.paths && c.paths.history_root) || "";
    var linkCfg = c.link_index || {};
    var linkMedia = document.getElementById("cfg-link-media-root");
    var linkShortcut = document.getElementById("cfg-link-shortcut-root");
    var linkLayout = document.getElementById("cfg-link-layout");
    var linkName = document.getElementById("cfg-link-name");
    if (linkMedia) linkMedia.value = linkCfg.media_root || "";
    if (linkShortcut) linkShortcut.value = linkCfg.shortcut_root || "";
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
    refreshActiveFeatureAfterConfig();
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
      localStorage.setItem(LS_KEY_LAST_DB_CATALOG_LOAD, JSON.stringify(rec));
    } catch (_) {}
  }

  function clearRememberedDbCatalogLoad() {
    try {
      localStorage.removeItem(LS_KEY_LAST_DB_CATALOG_LOAD);
    } catch (_) {}
  }

  function readRememberedDbCatalogLoad() {
    var raw = "";
    try {
      raw = localStorage.getItem(LS_KEY_LAST_DB_CATALOG_LOAD) || "";
    } catch (_) {
      return null;
    }
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
    }
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
          return;
        }
        renderPayload(outSub.data);
        return;
      }
      setStatus("从 DB 重新读取数据中…", false);
      var outAll = await fetchJson("/api/browse/default", { method: "GET" });
      if (!outAll.res.ok || !outAll.data.ok) {
        setStatus(outAll.data.error || browseHttpFailHint(outAll.res.status, "刷新"), true);
        return;
      }
      renderPayload(outAll.data);
    } catch (errRf) {
      setStatus("刷新请求异常：" + (errRf.message || String(errRf)), true);
    }
  }

  /** 与服务端 GET /api/browse/default 对齐：整库载入，保存刷新走整库 */
  async function loadAllDbCatalogFromDefault(opts) {
    opts = opts || {};
    if (!$btnDefault || $btnDefault.disabled) {
      setStatus("当前配置无法从 DB 载入数据。", true);
      return;
    }
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
    }
  }

  $file.addEventListener("change", function () {
    refreshYamlPickFromInput();
    var fl = $file.files;
    if (fl && fl.length === 1) {
      uploadYamlFiles([fl[0]]).catch(function (e) {
        setStatus("加载失败：" + (e.message || String(e)), true);
      });
    }
  });

  if ($btnYamlPickAll) {
    $btnYamlPickAll.addEventListener("click", function () {
      setYamlPickCheckAll(true);
    });
  }
  if ($btnYamlPickNone) {
    $btnYamlPickNone.addEventListener("click", function () {
      setYamlPickCheckAll(false);
    });
  }
  if ($btnYamlPickLoad) {
    $btnYamlPickLoad.addEventListener("click", function () {
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

  $btnCfg.addEventListener("click", function () {
    loadServerConfig()
      .then(function () {
        renderDbCatalogList();
      })
      .catch(function (e) {
        setStatus("读取配置失败：" + e, true);
      });
  });

  if ($chkEdit) {
    $chkEdit.addEventListener("change", function () {
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

  $btnDefault.addEventListener("click", function (ev) {
    ev.stopPropagation();
    toggleDbYearbookPopoverFromPrimaryBtn().catch(function (e) {
      setStatus("数据浮层：" + (e.message || String(e)), true);
    });
  });

  if ($btnDbCatalogAll) {
    $btnDbCatalogAll.addEventListener("click", function () {
      setDbCatalogCheckAll(true);
    });
  }
  if ($btnDbCatalogNone) {
    $btnDbCatalogNone.addEventListener("click", function () {
      setDbCatalogCheckAll(false);
    });
  }
  if ($btnDbCatalogRun) {
    $btnDbCatalogRun.addEventListener("click", function () {
      Promise.resolve(runDbCatalogLoadFromUi()).catch(function (e) {
        setStatus("加载失败：" + (e.message || String(e)), true);
      });
    });
  }

  relocateConfigPanelToCollectionDetailTab();
  bindDbCatalogPopoverDismissOnce();
  bindAppTabsOnce();
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

  initTheme();
  initFont();

  /** 从往返缓存恢复时，整页状态含筛选框；强制清空列筛并重绘 */
  window.addEventListener("pageshow", function (ev) {
    if (!ev.persisted) return;
    persistedSheetFilters = Object.create(null);
    if (lastBrowsePayload && lastBrowsePayload.ok) {
      renderPayload(lastBrowsePayload, true);
    }
  });
})();
