(function () {
  var root = (window.JpTvBrowseFeatureRegistry =
    window.JpTvBrowseFeatureRegistry || {
      features: [],
      register: function (feature) {
        this.features.push(feature);
      },
    });

  var collectionRecordsPayload = null;
  var collectionRecordsBound = false;
  var featureCtx = null;

  function ctx() {
    return featureCtx || {};
  }

  function arrSlice() {
    return (ctx().arrSlice || Array.prototype.slice);
  }

  function esc(s) {
    return ctx().esc ? ctx().esc(s) : String(s);
  }

  function defaultEnumValue(enumKey, fallback) {
    return ctx().defaultEnumValue
      ? ctx().defaultEnumValue(enumKey, fallback)
      : fallback || "";
  }

  function browseEnumOptions() {
    return ctx().browseEnumOptions ? ctx().browseEnumOptions() : {};
  }

  function browseEnumDisplay(enumKey, rawVal) {
    return ctx().browseEnumDisplay
      ? ctx().browseEnumDisplay(enumKey, rawVal)
      : String(rawVal || "");
  }

  function enumSectionTh(enumKey, fallback) {
    return ctx().enumSectionTh ? ctx().enumSectionTh(enumKey, fallback) : fallback;
  }

  function collectionView() {
    return ctx().collectionView || document.getElementById("collection-info-view");
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
    var enumOptions = browseEnumOptions();
    var opts = Array.isArray(enumOptions[enumKey]) ? enumOptions[enumKey].slice() : [];
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
    var view = collectionView();
    if (!view) return;
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
    view.innerHTML = h;
  }

  async function loadCollectionRecords() {
    var view = collectionView();
    if (!view) return;
    if (ctx().loadServerConfig) {
      await ctx().loadServerConfig().catch(function () {});
    }
    ctx().setStatus && ctx().setStatus("收集情况读取中...", false);
    view.innerHTML = "";
    var out = await ctx().fetchJson("/api/collection-info", { method: "GET" });
    if (!out.res.ok || !out.data || !out.data.ok) {
      ctx().setStatus &&
        ctx().setStatus(
          (out.data && out.data.error) ||
            ctx().browseHttpFailHint(out.res.status, "收集情况读取"),
          true,
        );
      return;
    }
    renderCollectionRecords(out.data);
    ctx().setStatus && ctx().setStatus("", false);
  }

  function gatherCollectionRecordsFromView() {
    var view = collectionView();
    if (!view) return [];
    var items = arrSlice().call(view.querySelectorAll(".collection-record-item"));
    var out = [];
    var i;
    for (i = 0; i < items.length; i++) {
      var el = items[i];
      function field(name) {
        var node = el.querySelector('[data-collection-field="' + name + '"]');
        return node ? String(node.value || "").trim() : "";
      }
      var years = arrSlice()
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
      ctx().setStatus && ctx().setStatus("没有可保存的收集情况。", true);
      return;
    }
    ctx().setStatus && ctx().setStatus("收集情况保存中...", false);
    var out = await ctx().fetchJson("/api/collection-info", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({ records: records }),
    });
    if (!out.res.ok || !out.data || !out.data.ok) {
      ctx().setStatus &&
        ctx().setStatus(
          (out.data && out.data.error) ||
            ctx().browseHttpFailHint(out.res.status, "收集情况保存"),
          true,
        );
      return;
    }
    collectionRecordsPayload.records = out.data.records || records;
    ctx().setStatus && ctx().setStatus("收集情况已保存。", false);
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
    var view = collectionView();
    if (collectionRecordsBound || !view) return;
    collectionRecordsBound = true;
    view.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || !t.closest) return;
      var btn = t.closest("[data-collection-action]");
      if (!btn || !view.contains(btn)) return;
      var action = btn.getAttribute("data-collection-action");
      if (action === "reload") {
        loadCollectionRecords().catch(function (e) {
          ctx().setStatus &&
            ctx().setStatus("收集情况读取失败：" + (e.message || String(e)), true);
        });
        return;
      }
      if (action === "save") {
        saveCollectionRecords().catch(function (e) {
          ctx().setStatus &&
            ctx().setStatus("收集情况保存失败：" + (e.message || String(e)), true);
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

  root.register({
    id: "collection-info",
    label: "收集情况",
    tabId: "tab-collection-info",
    viewId: "collection-info-view",
    order: 20,
    init: function (nextCtx) {
      featureCtx = nextCtx;
      bindCollectionRecordsOnce();
    },
    activate: function (nextCtx) {
      featureCtx = nextCtx;
      loadCollectionRecords().catch(function (e) {
        ctx().setStatus && ctx().setStatus("收集情况加载失败：" + (e.message || String(e)), true);
      });
    },
    refreshAfterConfig: function (nextCtx) {
      featureCtx = nextCtx;
      if (collectionRecordsPayload) renderCollectionRecords(collectionRecordsPayload);
    },
  });
})();
