(function (global) {
  "use strict";

  var API = "/api/catalog-library/";
  var array = function (value) { return Array.isArray(value) ? value : []; };
  var text = function (value) { return String(value == null ? "" : value); };
  var copy = function (value) { return JSON.parse(JSON.stringify(value)); };
  var errorText = function (error) { return error && error.message ? error.message : text(error); };
  function attribute(record, kind, fallback) {
    var row = array(record && record.attributes).find(function (item) { return item && item.type === kind; });
    return row ? row.data : fallback;
  }
  function template() {
    return { attributes: [{ type: "name", data: "" }, { type: "country", data: "japan" },
      { type: "date", data: { start: "", end: "" } },
      { type: "collection-type", data: { domain: "animation", release_type: "tv", collectioned: [], markers: [] } }] };
  }
  function localCover(value) {
    // Only the dedicated local asset route is allowed. Never contact a provider
    // from an image element, including protocol-relative or encoded URLs.
    var raw = text(value);
    return /^\/api\/catalog-library\/assets\/[0-9a-f]{64}\.(?:jpg|png|gif|webp)$/.test(raw) ? raw : "";
  }
  function selector(item) {
    if (item && item.id) return item.ref ? { id: item.id, ref: copy(item.ref) } : { id: item.id };
    if (item && item.ref) return { ref: copy(item.ref) };
    throw new Error("作品缺少本地引用，请刷新作品库。");
  }
  function dateLabel(value) { return text(value).replace(/^([0-9X]{4})([0-9X]{2})([0-9X]{2})$/i, "$1-$2-$3"); }
  function fieldLabel(key) {
    return ({ name: "作品名", title: "作品名", name_cn: "中文名", aliases: "别名", summary: "简介", cover: "封面", cover_url: "封面",
      date: "日期", "date.start": "开始日期", "date.end": "结束日期", chapters: "章节", episodes: "章节", tags: "来源标签", relations: "作品关系" })[key] || key;
  }
  function displayValue(value) { return value == null || value === "" ? "未填写" : typeof value === "object" ? JSON.stringify(value, null, 2) : text(value); }
  function effectiveProviderDifference(difference, selected) {
    var after = difference.after, changed = difference.changed !== false, selectable = difference.selectable !== false && difference.allowed !== false;
    if (difference.field === "metadata.aliases") {
      if (selected.indexOf("name") >= 0 && Array.isArray(difference.after_when_name_selected)) after = difference.after_when_name_selected;
      changed = JSON.stringify(difference.before) !== JSON.stringify(after);
      selectable = difference.allowed !== false && Array.isArray(after) && after.length > 0;
    }
    return { after: after, changed: changed, selectable: selectable };
  }
  function episodeRange(startValue, endValue) {
    var startText = text(startValue).trim(), endText = text(endValue).trim();
    if (!startText && !endText) return null;
    if (!startText || !endText) throw new Error("限定正片集数时，请同时填写起始集和结束集；全部留空表示完整条目。");
    var start = Number(startText), end = Number(endText), decimal = /^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
    if (!decimal.test(startText) || !decimal.test(endText) || !Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < 0) throw new Error("起止集数必须为不小于 0 的有限数值。");
    if (start > end) throw new Error("起始集不能大于结束集。");
    return { start: start, end: end };
  }
  function providerState() {
    return { query: "", loading: false, results: [], preview: null, selected: [], error: "", guidance: "", rangeStart: "", rangeEnd: "", rangeOpen: false, mode: "full" };
  }
  function sourceRows(detail) {
    var item = detail && (detail.item || detail.work || detail) || {}, record = detail && detail.record || item.record || {};
    return array(detail && detail.sources || item.sources || record.source_refs || record.sources);
  }
  function bangumiBindings(detail) {
    // Each association has its own chapter scope, even for the same subject ID.
    return sourceRows(detail).filter(function (source) { return source.provider === "bangumi" && (source.subject_id || source.external_id); });
  }
  function bindingRange(source) {
    var range = source.episode_range;
    if (range == null && !source.scope) return null;
    if (!range || typeof range !== "object" || range.start == null || range.end == null || text(range.start).trim() === "" || text(range.end).trim() === "") {
      throw new Error("此来源的章节范围缺失或无效，请在来源页手动核对起止集数后重新预览；不会自动改为完整条目。");
    }
    var parsed = episodeRange(range.start, range.end);
    if (source.scope) {
      var scope = /^episodes:(\d+(?:\.\d+)?(?:e[+-]?\d+)?)-(\d+(?:\.\d+)?(?:e[+-]?\d+)?)$/i.exec(text(source.scope));
      if (!scope || Number(scope[1]) !== parsed.start || Number(scope[2]) !== parsed.end) throw new Error("此来源的范围标识与起止集数不一致，请手动核对后重新预览。");
    }
    return parsed;
  }

  function createController(options) {
    options = options || {};
    var state = { active: false, disposed: false, loaded: false, loading: false, error: "", notice: "", busy: false, resourceOpening: false, searchDraft: "",
      filters: { query: "", domain: "", year: "", classification_id: "", page: 1, page_size: 24, sort: "name" },
      mode: options.mode === "list" ? "list" : "cards", items: [], total: 0, facets: {}, classifications: [], classificationRevision: "", detail: null, detailLoading: false,
      detailError: "", detailSelector: null, detailTab: "overview", identityPreview: null,
      provider: providerState() };
    var browseSerial = 0, detailSerial = 0, providerSerial = 0, lifecycle = 0;
    function changed(reason) { if (!state.disposed && options.onChange) options.onChange(state, reason); }
    function notice(value, error) {
      if (state.disposed) return;
      state.notice = value;
      if (options.setStatus) options.setStatus(value, !!error);
    }
    async function request(route, payload) {
      var response = await options.request(route[0] === "/" ? route : API + route, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}) });
      var data = response && response.data !== undefined ? response.data : response;
      if (!data || data.ok === false || response && response.res && !response.res.ok) throw new Error(data && (data.error || data.message) || "作品库请求失败");
      return data;
    }
    async function browse() {
      if (state.disposed || !state.active) return;
      var serial = ++browseSerial, epoch = lifecycle;
      state.loading = true; state.error = ""; changed("loading");
      try {
        var data = await request("browse", copy(state.filters));
        if (serial !== browseSerial || epoch !== lifecycle || state.disposed) return;
        state.items = array(data.items); state.total = Number(data.total) || 0; state.facets = data.facets || {};
        state.filters.page = Math.max(1, Number(data.page) || state.filters.page); state.loaded = true;
        state.identityMissing = Number(data.legacy_count) || 0; state.classificationRevision = data.classification_revision || "";
        state.classifications = array(state.facets.classifications);
      } catch (error) { if (serial === browseSerial && epoch === lifecycle) state.error = errorText(error); }
      finally { if (serial === browseSerial && epoch === lifecycle) { state.loading = false; changed("browse"); } }
    }
    function setFilters(values) {
      if (state.disposed) return Promise.resolve();
      Object.keys(values).forEach(function (key) { if (Object.prototype.hasOwnProperty.call(state.filters, key)) state.filters[key] = values[key]; });
      if (Object.prototype.hasOwnProperty.call(values, "query")) state.searchDraft = text(values.query);
      if (!Object.prototype.hasOwnProperty.call(values, "page")) state.filters.page = 1;
      return browse();
    }
    function resetProvider() { providerSerial += 1; state.provider = providerState(); }
    async function openDetail(item, preserveTab) {
      var selected = selector(item), serial = ++detailSerial, epoch = lifecycle;
      state.detailSelector = selected; state.detail = null; state.detailLoading = true; state.detailError = "";
      if (!preserveTab) state.detailTab = "overview";
      resetProvider(); changed("detail-loading");
      try {
        var data = await request("detail", selected);
        if (serial !== detailSerial || epoch !== lifecycle || state.disposed) return;
        state.detail = data.detail || data;
      } catch (error) { if (serial === detailSerial && epoch === lifecycle) state.detailError = errorText(error); }
      finally { if (serial === detailSerial && epoch === lifecycle) { state.detailLoading = false; changed("detail"); } }
    }
    function closeDetail() {
      if (state.busy) return;
      detailSerial += 1; state.detailSelector = null; state.detail = null; state.detailLoading = false; state.detailError = ""; resetProvider(); changed("close-detail");
    }
    function currentRef() {
      var detail = state.detail || {}, item = detail.item || detail.work || detail;
      var ref = detail.ref || item.ref;
      if (!ref) throw new Error("作品缺少当前版本引用，请重新加载详情。");
      return copy(ref);
    }
    async function refreshAfterWrite(result, epoch, openCreated) {
      if (epoch !== lifecycle || state.disposed) return;
      var saved = array(result.records)[0];
      if (saved && (state.detailSelector || openCreated)) {
        await openDetail({ id: saved.id || saved.record && saved.record.id, ref: saved.ref }, true);
      } else if (state.detailSelector) {
        // Never retry a legacy positional selector after a write using its old
        // version token; reload the list and let the user explicitly reopen it.
        state.detailSelector = null; state.detail = null; state.detailLoading = false;
      }
      await browse();
    }
    async function mutation(action) {
      if (state.busy || state.disposed) return;
      var epoch = lifecycle; state.busy = true; state.notice = ""; changed("mutation");
      try { return await action(epoch); }
      catch (error) { if (epoch === lifecycle) { notice(errorText(error), true); state.error = errorText(error); } throw error; }
      finally { state.busy = false; if (epoch === lifecycle) changed("mutation"); }
    }
    async function saveRecord(record, ref) {
      return mutation(async function (epoch) {
        var edits = [{ ref: ref || null, record: copy(record) }];
        var preview = await request("edits/preview", { edits: edits });
        var blocked = array(preview.issues).filter(function (issue) { return issue.blocking !== false; });
        if (blocked.length) throw new Error(blocked.map(function (issue) { return issue.message || issue.reason; }).join("；"));
        if (epoch !== lifecycle || state.disposed) throw new Error("页面已变化，请重新打开编辑窗口。");
        var result = await request("edits/apply", { edits: preview.edits || edits });
        if (epoch === lifecycle) {
          notice("作品已保存。"); await refreshAfterWrite(result, epoch, false);
        }
        return result;
      });
    }
    async function previewIdentities() {
      return mutation(async function (epoch) {
        var result = await request("identities/preview", {});
        if (epoch === lifecycle) state.identityPreview = result;
        return result;
      });
    }
    async function applyIdentities() {
      if (!state.identityPreview) throw new Error("请先预览需要初始化的标识。");
      var preview = state.identityPreview;
      return mutation(async function (epoch) {
        var issues = array(preview.issues).filter(function (issue) { return issue.blocking !== false; });
        if (issues.length) throw new Error(issues.map(function (issue) { return issue.message; }).join("；"));
        var result = await request("edits/apply", { edits: preview.edits });
        if (epoch === lifecycle) { state.identityPreview = null; notice("本地标识已初始化。"); await refreshAfterWrite(result, epoch, false); }
        return result;
      });
    }
    async function searchProvider(query) {
      if (!state.detail || !state.active || state.detailLoading || state.busy || state.disposed || state.provider.loading) return;
      var serial = ++providerSerial, epoch = lifecycle;
      state.provider.query = text(query).trim(); state.provider.mode = "full"; state.provider.guidance = ""; state.provider.loading = true; state.provider.error = ""; state.provider.preview = null; state.provider.selected = []; changed("provider");
      try {
        var result = await request("provider/search", { query: state.provider.query });
        if (epoch === lifecycle && serial === providerSerial) state.provider.results = array(result.items || result.results);
      } catch (error) { if (epoch === lifecycle && serial === providerSerial) state.provider.error = errorText(error); }
      finally { if (epoch === lifecycle && serial === providerSerial) { state.provider.loading = false; changed("provider"); } }
    }
    async function previewProvider(subjectId) {
      if (!state.detail || !state.active || state.detailLoading || state.busy || state.disposed || state.provider.loading) return;
      var ref, serial = ++providerSerial, epoch = lifecycle;
      state.provider.mode = "full"; state.provider.guidance = ""; state.provider.loading = true; state.provider.error = ""; state.provider.preview = null; state.provider.selected = []; changed("provider");
      try {
        ref = currentRef();
        var range = episodeRange(state.provider.rangeStart, state.provider.rangeEnd), payload = { ref: ref, subject_id: text(subjectId) };
        if (range) payload.episode_range = range;
        var result = await request("provider/preview", payload);
        if (epoch === lifecycle && serial === providerSerial) state.provider.preview = result;
      } catch (error) { if (epoch === lifecycle && serial === providerSerial) state.provider.error = errorText(error); }
      finally { if (epoch === lifecycle && serial === providerSerial) { state.provider.loading = false; changed("provider"); } }
    }
    async function refreshProvider(binding) {
      if (!state.detail || !state.active || state.detailLoading || state.busy || state.disposed || state.provider.loading) return;
      var bindings = bangumiBindings(state.detail), selected = binding;
      state.detailTab = "sources"; resetProvider();
      if (selected == null && bindings.length === 1) selected = bindings[0];
      if (selected == null) {
        if (!bindings.length) {
          state.provider.query = titleOf(state.detail.item || state.detail.work || state.detail);
          state.provider.guidance = "尚未关联 Bangumi。请先搜索并确认对应条目。";
          changed("provider-search-start");
        } else {
          state.provider.guidance = "已关联多个来源或章节范围，请选择下方要刷新的来源。";
          changed("provider-source-choose");
        }
        return;
      }
      if (bindings.indexOf(selected) < 0) {
        state.provider.error = "来源关联已变化，请重新选择当前作品的来源。"; changed("provider"); return;
      }
      try {
        var range = bindingRange(selected);
        state.provider.rangeStart = range ? text(range.start) : "";
        state.provider.rangeEnd = range ? text(range.end) : "";
        state.provider.rangeOpen = !!range;
      } catch (error) { state.provider.error = errorText(error); changed("provider"); return; }
      return previewProvider(selected.subject_id || selected.external_id);
    }
    async function previewCover(subjectId) {
      if (!state.detail || !state.active || state.detailLoading || state.busy || state.disposed || state.provider.loading) return;
      var serial = ++providerSerial, epoch = lifecycle, payload = { ref: currentRef() };
      if (subjectId != null) payload.subject_id = text(subjectId);
      state.detailTab = "sources"; state.provider.mode = "cover"; state.provider.loading = true;
      state.provider.error = ""; state.provider.preview = null; state.provider.selected = []; changed("provider");
      try {
        var result = await request("provider/cover-preview", payload);
        if (epoch === lifecycle && serial === providerSerial) state.provider.preview = result;
      } catch (error) { if (epoch === lifecycle && serial === providerSerial) state.provider.error = errorText(error); }
      finally { if (epoch === lifecycle && serial === providerSerial) { state.provider.loading = false; changed("provider"); } }
    }
    function setEpisodeRange(start, end) {
      if (state.busy || state.disposed) return;
      var provider = state.provider;
      if (provider.rangeStart === text(start) && provider.rangeEnd === text(end)) return;
      provider.rangeStart = text(start); provider.rangeEnd = text(end);
      // A range is part of the server-signed preview. Changing it must retire
      // both a displayed token and an in-flight preview before another apply.
      providerSerial += 1; provider.preview = null; provider.selected = []; provider.error = ""; provider.loading = false;
      changed("provider-range");
    }
    function selectProviderField(field, selected) {
      if (state.busy || !state.provider.preview) return;
      var difference = array(state.provider.preview.diff).find(function (row) { return row.field === field; });
      if (!difference) return;
      var effective = effectiveProviderDifference(difference, state.provider.selected);
      if (!effective.selectable || !effective.changed) return;
      var fields = state.provider.selected.filter(function (key) { return key !== field; });
      if (selected) fields.push(field);
      var aliases = array(state.provider.preview.diff).find(function (row) { return row.field === "metadata.aliases"; });
      if (aliases) {
        var aliasState = effectiveProviderDifference(aliases, fields);
        if (!aliasState.selectable || !aliasState.changed) fields = fields.filter(function (key) { return key !== "metadata.aliases"; });
      }
      state.provider.selected = fields;
    }
    async function applyProvider(expectedPreview) {
      if (!state.detail || !state.active || state.detailLoading || state.disposed || state.provider.loading) throw new Error("页面或预览已变化，请重新预览后确认。");
      var preview = state.provider.preview, fields = state.provider.selected.slice();
      if (!preview) throw new Error("请先获取来源快照并预览差异。");
      if (expectedPreview && expectedPreview !== preview) throw new Error("当前预览已更新，不能应用旧的预览。");
      if (state.provider.mode === "cover" && fields.indexOf("metadata.cover") < 0) throw new Error("请先勾选封面，再确认更新。");
      return mutation(async function (epoch) {
        var result = await request("provider/apply", { ref: preview.ref, snapshot_id: preview.snapshot_id, preview_token: preview.preview_token, selected_fields: fields });
        if (epoch === lifecycle) {
          var addedAliases = array(result.automatic_aliases_added).filter(function (alias) { return typeof alias === "string" && alias.trim(); });
          var message = fields.length ? "所选来源字段已写入本地作品。" : addedAliases.length ? "来源关联已保存。" : "来源关联已保存，未修改作品字段。";
          if (addedAliases.length) message += "已自动追加别名：" + addedAliases.join("、") + "；已有别名保留并去重。";
          notice(message); await refreshAfterWrite(result, epoch, false);
        }
        return result;
      });
    }
    async function assignClassification(valueId, action) {
      return mutation(async function (epoch) {
        var preview = await request("classification-edits/preview", { refs: [currentRef()], value_id: valueId, action: action || "assign" });
        var issues = array(preview.issues).filter(function (issue) { return issue.blocking !== false; });
        if (issues.length) throw new Error(issues.map(function (issue) { return issue.message; }).join("；"));
        if (epoch !== lifecycle) throw new Error("页面已变化，请重新打开分类页。");
        var result = await request("edits/apply", { edits: preview.edits });
        if (epoch === lifecycle) { notice("分类已保存。"); await refreshAfterWrite(result, epoch, false); }
        return result;
      });
    }
    async function saveClassification(name, id) {
      return mutation(async function (epoch) {
        if (!text(name).trim()) throw new Error("请输入系列名称。");
        var payload = { revision: state.classificationRevision, type: "series", name: text(name).trim() };
        if (id) payload.id = id;
        var result = await request("classifications", payload);
        if (epoch === lifecycle) { notice(id ? "系列名称已保存。" : "系列已创建，可在下方关联当前作品。"); await browse(); }
        return result;
      });
    }
    async function openResource(workPath, pressPath) {
      if (state.resourceOpening || state.busy || state.disposed) return;
      var epoch = lifecycle, selected = detailSerial;
      state.resourceOpening = true; changed("resource");
      try {
        if (!text(workPath).trim() || !text(pressPath).trim()) throw new Error("资源尚未绑定完整目录。");
        var result = await request("/api/collection-detail/press/open", { path: workPath, press_path: pressPath });
        if (epoch === lifecycle && selected === detailSerial) notice("已请求打开资源目录。");
        return result;
      } catch (error) {
        if (epoch === lifecycle && selected === detailSerial) notice(errorText(error), true);
      } finally { state.resourceOpening = false; if (epoch === lifecycle) changed("resource"); }
    }
    function deactivate() {
      state.active = false; lifecycle += 1; browseSerial += 1; detailSerial += 1; providerSerial += 1;
      state.loading = false; state.detailLoading = false; state.provider.loading = false; state.provider.preview = null; state.provider.selected = []; state.identityPreview = null;
    }
    return { state: state, browse: browse, setFilters: setFilters, openDetail: openDetail, closeDetail: closeDetail,
      setSearchDraft: function (value) { if (!state.disposed) state.searchDraft = text(value); },
      currentRef: currentRef, saveRecord: saveRecord, previewIdentities: previewIdentities, applyIdentities: applyIdentities,
      searchProvider: searchProvider, previewProvider: previewProvider, refreshProvider: refreshProvider, previewCover: previewCover, setEpisodeRange: setEpisodeRange, selectProviderField: selectProviderField,
      applyProvider: applyProvider, assignClassification: assignClassification, saveClassification: saveClassification, openResource: openResource,
      setMode: function (mode) { state.mode = mode === "list" ? "list" : "cards"; changed("mode"); },
      setDetailTab: function (tab) { state.detailTab = tab; changed("detail-tab"); },
      activate: function () { state.active = true; if (state.detailSelector && !state.detail) openDetail(state.detailSelector, true); return browse(); },
      deactivate: deactivate, dispose: function () { deactivate(); state.disposed = true; },
      epoch: function () { return lifecycle; } };
  }

  var context = {}, controller = null, view = null, nav = null, ownedView = false, ownedTab = false, ui = {}, returnFocusKey = "";
  function el(tag, cls, value) {
    var node = document.createElement(tag); if (cls) node.className = cls;
    if (value != null) node.textContent = text(value); return node;
  }
  function append(parent) { for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]); return parent; }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function run(task) { Promise.resolve().then(task).catch(function () { /* Controller/form presents errors. */ }); }
  function button(label, action, disabled, cls) {
    var node = el("button", "library-button" + (cls ? " " + cls : ""), label); node.type = "button"; node.disabled = !!disabled;
    node.addEventListener("click", function () { run(action); }); return node;
  }
  function labelField(parent, caption, control) {
    var label = el("label", "library-field"); append(label, el("span", "library-label", caption), control); parent.appendChild(label); return control;
  }
  function badge(value) { return el("span", "library-badge", value); }
  function option(parent, value, caption) { var node = el("option", "", caption); node.value = text(value); parent.appendChild(node); }
  function cover(item) {
    var box = el("div", "library-cover"), url = localCover(item.cover_url || item.cover && item.cover.url);
    var placeholder = el("span", "library-cover-placeholder");
    append(placeholder, el("span", "library-cover-initial", text(item.name || item.title || "作").trim().slice(0, 1)), el("span", "library-cover-caption", "暂无封面"));
    if (item.domain_label || item.domain) placeholder.appendChild(el("span", "library-cover-kind", item.domain_label || item.domain));
    placeholder.setAttribute("aria-hidden", "true"); box.appendChild(placeholder);
    if (url) {
      var image = el("img"); image.src = url; image.alt = titleOf(item) + "封面"; image.loading = "lazy"; image.decoding = "async"; image.width = 160; image.height = 224;
      image.addEventListener("error", function () { image.remove(); }); box.appendChild(image);
    }
    return box;
  }
  function titleOf(item) { return text(item.name || item.title || attribute(item.record, "name", "未命名作品")); }
  function keyOf(item) { return text(item.id || item.ref && item.ref.yaml_source_rel + "#" + item.ref.index_in_file); }
  function renderCard(item) {
    var card = el("article", "library-card"), open = button("", function () {
      returnFocusKey = keyOf(item); controller.openDetail(item);
    }, false, "library-card-link");
    ui.cardButtons[keyOf(item)] = open;
    var info = el("div", "library-card-info"), heading = el("h3", "", titleOf(item));
    var facts = [item.domain_label || item.domain, item.year, item.release_type_label || item.release_type].filter(Boolean).join(" · ");
    append(info, heading, el("p", "library-meta", facts || "未分类"));
    var count = item.resource_count == null ? item.press_count == null ? array(item.resources || item.press).length : item.press_count : item.resource_count;
    info.appendChild(el("p", "library-resource-count", count ? count + " 个资源版本" : "暂无资源"));
    if (item.summary) info.appendChild(el("p", "library-card-summary", item.summary));
    var tags = el("div", "library-badges"); array(item.classifications).slice(0, 3).forEach(function (tag) { tags.appendChild(badge(tag.name || tag.label)); });
    info.appendChild(tags); append(open, cover(item), info); card.appendChild(open); return card;
  }
  function refreshFacets(state) {
    clear(ui.domains);
    var domains = array(state.facets.domains), all = button("全部", function () { controller.setFilters({ domain: "" }); });
    all.setAttribute("aria-pressed", state.filters.domain ? "false" : "true"); ui.domains.appendChild(all);
    domains.forEach(function (row) {
      var value = text(row.value == null ? row.id : row.value);
      var node = button((row.label || row.name || value) + (row.count == null ? "" : " " + row.count), function () { controller.setFilters({ domain: value }); });
      node.setAttribute("aria-pressed", state.filters.domain === value ? "true" : "false"); ui.domains.appendChild(node);
    });
    clear(ui.year); option(ui.year, "", "所有年份"); array(state.facets.years).forEach(function (row) { var value = typeof row === "object" ? row.value || row.year : row; option(ui.year, value, value); }); ui.year.value = state.filters.year;
    clear(ui.series); option(ui.series, "", "所有系列"); array(state.facets.classifications).filter(function (row) { return (row.type || row.kind) === "series"; }).forEach(function (row) { option(ui.series, row.id, (row.name || row.label) + (row.count == null ? "" : " · " + row.count)); }); ui.series.value = state.filters.classification_id;
    clear(ui.yearLinks);
    array(state.facets.years).slice(0, 12).forEach(function (row) {
      var year = text(typeof row === "object" ? row.value || row.year : row);
      var chip = button(year, function () { return controller.setFilters({ year: state.filters.year === year ? "" : year }); }, false, "library-filter-chip");
      chip.setAttribute("aria-pressed", text(state.filters.year) === year ? "true" : "false"); ui.yearLinks.appendChild(chip);
    });
    clear(ui.activeFilters);
    [["query", state.filters.query], ["year", state.filters.year && state.filters.year + " 年"], ["classification_id", (state.classifications.find(function (row) { return row.id === state.filters.classification_id; }) || {}).name]].forEach(function (pair) {
      if (!pair[1]) return;
      var remove = button(pair[1] + " ×", function () { var patch = {}; patch[pair[0]] = ""; return controller.setFilters(patch); }, false, "library-filter-chip");
      remove.setAttribute("aria-label", "清除筛选：" + pair[1]); ui.activeFilters.appendChild(remove);
    });
    ui.resetFilters.disabled = !(state.filters.query || state.filters.domain || state.filters.year || state.filters.classification_id);
  }
  function renderBrowse(state) {
    ui.count.textContent = state.loading && !state.loaded ? "加载中…" : state.total + " 部作品";
    ui.results.setAttribute("aria-busy", state.loading ? "true" : "false");
    // The input is a draft, while filters.query is the last submitted query.
    // Async loading/detail updates must not discard text being typed meanwhile.
    if (ui.search.value !== state.searchDraft) ui.search.value = state.searchDraft;
    ui.sort.value = state.filters.sort;
    ui.refresh.disabled = state.loading || state.busy; ui.add.disabled = state.busy;
    ui.initialize.disabled = state.busy; ui.maintenance.hidden = !state.identityMissing;
    ui.cards.setAttribute("aria-pressed", state.mode === "cards" ? "true" : "false"); ui.list.setAttribute("aria-pressed", state.mode === "list" ? "true" : "false");
    refreshFacets(state); clear(ui.pagination); clear(ui.identity);
    // Keep the current page visible while a new query is running. This avoids
    // recreating every image and moving focus for a loading-only state change.
    if (!state.loading || !state.loaded) { clear(ui.results); ui.cardButtons = Object.create(null);
    if (state.error) {
      var error = el("div", "library-error"); error.setAttribute("role", "alert");
      append(error, el("p", "", state.error), button("重试", function () { controller.browse(); }, state.loading)); ui.results.appendChild(error);
    } else if (state.loading && !state.loaded) ui.results.appendChild(el("p", "library-empty", "正在读取本地作品…"));
    else if (!state.items.length) ui.results.appendChild(el("p", "library-empty", state.filters.query || state.filters.domain || state.filters.year || state.filters.classification_id ? "没有符合筛选条件的作品。" : "本地作品库还没有作品。"));
    else {
      var grid = el("div", "library-grid" + (state.mode === "list" ? " is-list" : "")); state.items.forEach(function (item) { grid.appendChild(renderCard(item)); }); ui.results.appendChild(grid);
    }
    }
    var pages = Math.max(1, Math.ceil(state.total / state.filters.page_size));
    ui.pagination.appendChild(button("‹ 上一页", function () { controller.setFilters({ page: state.filters.page - 1 }); }, state.loading || state.filters.page <= 1));
    var pageNumbers = [1, pages]; for (var page = Math.max(1, state.filters.page - 2); page <= Math.min(pages, state.filters.page + 2); page++) pageNumbers.push(page);
    var previousPage = 0;
    pageNumbers.filter(function (number, index, numbers) { return numbers.indexOf(number) === index; }).sort(function (a, b) { return a - b; }).forEach(function (number) {
      if (previousPage && number - previousPage > 1) ui.pagination.appendChild(el("span", "library-page-gap", "…"));
      var pageButton = button(text(number), function () { controller.setFilters({ page: number }); }, state.loading || number === state.filters.page);
      pageButton.setAttribute("aria-label", "第 " + number + " 页"); if (number === state.filters.page) pageButton.setAttribute("aria-current", "page");
      ui.pagination.appendChild(pageButton); previousPage = number;
    });
    ui.pagination.appendChild(button("下一页 ›", function () { controller.setFilters({ page: state.filters.page + 1 }); }, state.loading || state.filters.page >= pages));
    if (state.identityPreview) {
      var preview = state.identityPreview, count = preview.count == null ? array(preview.edits).length : preview.count;
      array(preview.issues).forEach(function (issue) { ui.identity.appendChild(el("p", "library-error", issue.message || issue.reason)); });
      append(ui.identity, el("p", "", "将为 " + count + " 条记录补充本地稳定标识，不修改作品名称或目录。"),
        button("确认初始化", function () { return controller.applyIdentities(); }, state.busy || !count || array(preview.issues).some(function (issue) { return issue.blocking !== false; }), "is-primary"),
        button("取消", function () { state.identityPreview = null; render(); }, state.busy));
      if (state.identityMissing > count) ui.identity.appendChild(el("p", "library-meta", "每次最多处理 200 条；完成后可继续初始化余下记录。"));
    }
  }
  function workFacts(detail, item) {
    var record = detail.record || item.record || {}, dates = attribute(record, "date", {}), meta = el("dl", "library-facts");
    [["分类", item.domain_label || item.domain], ["类型", item.release_type_label || item.release_type],
      ["开播", dateLabel(item.begin_date || dates.start)], ["完结", dateLabel(item.end_date || dates.end)]].forEach(function (pair) {
      if (pair[1]) append(meta, el("dt", "", pair[0]), el("dd", "", pair[1]));
    });
    return meta;
  }
  function renderOverview(parent, detail, item) {
    var record = detail.record || item.record || {};
    parent.appendChild(el("h2", "library-section-title", "作品简介"));
    var metadata = detail.metadata || record.metadata || {}, summary = metadata.summary || item.summary || attribute(record, "summary", "");
    parent.appendChild(el("p", "library-summary", summary || "暂无简介。"));
    var aliases = array(item.aliases || metadata.aliases); if (aliases.length) parent.appendChild(el("p", "library-meta", "别名：" + aliases.map(function (row) { return typeof row === "object" ? row.name || row.value : row; }).join(" / ")));
    if (record.id || item.id) {
      var identity = el("details", "library-data-identity"); append(identity, el("summary", "", "数据标识"), el("p", "library-path", record.id || item.id)); parent.appendChild(identity);
    }
  }
  function renderResources(parent, detail, item) {
    var rows = array(detail.resources || item.resources || item.press), collection = attribute(detail.record || item.record, "collection-type", {});
    if (collection.path || item.path) append(parent, el("p", "library-meta", "作品目录"), el("p", "library-path", collection.path || item.path));
    if (!rows.length) parent.appendChild(el("p", "library-empty", "尚未绑定本地资源。"));
    rows.forEach(function (row) {
      var box = el("article", "library-resource");
      append(box, el("h3", "", row.label || [row.press_format, row.press_group].filter(Boolean).join(" · ") || "资源版本"),
        el("p", "library-path", row.resolved_path || row.path || row.press_path || "未绑定目录"));
      if (row.continuation_title) box.appendChild(badge(row.continuation_title));
      if (row.state || row.status) box.appendChild(el("p", "library-meta", row.state_label || row.status_label || row.state || row.status));
      if ((collection.path || item.path) && row.press_path) {
        box.appendChild(button("打开目录", function () { return controller.openResource(collection.path || item.path, row.press_path); }, controller.state.busy || controller.state.resourceOpening));
      }
      parent.appendChild(box);
    });
  }
  function renderChapters(parent, detail, item) {
    var rows = array(detail.chapters || item.chapters || (detail.record || {}).chapters);
    if (!rows.length) { parent.appendChild(el("p", "library-empty", "暂无章节信息。")); return; }
    var list = el("ol", "library-chapters"); rows.forEach(function (row, index) {
      var li = el("li", "library-chapter"); append(li, el("span", "library-meta", row.number || row.sort || index + 1), el("span", "", row.name || row.title || row.name_cn || "未命名章节"));
      if (row.air_date || row.date || row.airdate) li.appendChild(el("span", "library-meta", dateLabel(row.air_date || row.date || row.airdate))); list.appendChild(li);
    }); parent.appendChild(list);
  }
  function renderClassifications(parent, detail, item) {
    var tags = array(detail.classifications || item.classifications), current = el("div", "library-badges");
    tags.forEach(function (tag) {
      var chip = el("span", "library-classification-chip"); chip.appendChild(badge(((tag.kind || tag.type) === "series" ? "系列 · " : "") + (tag.name || tag.label)));
      chip.appendChild(button("移除", function () { return controller.assignClassification(tag.value_id || tag.id, "remove"); }, controller.state.busy)); current.appendChild(chip);
    });
    parent.appendChild(current); if (!tags.length) parent.appendChild(el("p", "library-empty", "尚未添加分类或系列标签。"));
    parent.appendChild(el("p", "library-meta", "系列由本地分类标签聚合；不会合并独立作品或资源目录。"));
    var definitions = controller.state.classifications.filter(function (row) { return (row.type || row.kind) === "series"; });
    var available = definitions.filter(function (row) { return !tags.some(function (tag) { return (tag.value_id || tag.id) === row.id; }); });
    var tools = el("div", "library-classification-tools"), select = labelField(tools, "关联已有系列", el("select"));
    option(select, "", "选择系列"); available.forEach(function (row) { option(select, row.id, row.name || row.label); });
    var assign = button("关联作品", function () { return controller.assignClassification(select.value, "assign"); }, true);
    select.addEventListener("change", function () { assign.disabled = !select.value || controller.state.busy; }); tools.appendChild(assign); parent.appendChild(tools);
    var form = el("form", "library-classification-tools"), name = labelField(form, "新建系列", el("input")); name.placeholder = "系列名称"; name.maxLength = 200;
    var create = el("button", "library-button", "创建系列"); create.type = "submit"; create.disabled = controller.state.busy; form.appendChild(create);
    form.addEventListener("submit", function (event) { event.preventDefault(); run(function () { return controller.saveClassification(name.value); }); }); parent.appendChild(form);
    if (definitions.length) {
      var management = el("details", "library-series-management"); management.appendChild(el("summary", "", "重命名系列"));
      var renameForm = el("form", "library-classification-tools"), renameSelect = labelField(renameForm, "系列", el("select")), renameInput = labelField(renameForm, "名称", el("input"));
      definitions.forEach(function (row) { option(renameSelect, row.id, row.name); }); renameInput.value = definitions[0].name; renameInput.maxLength = 200;
      renameSelect.addEventListener("change", function () { var selected = definitions.find(function (row) { return row.id === renameSelect.value; }); renameInput.value = selected ? selected.name : ""; });
      var renameButton = el("button", "library-button", "保存名称"); renameButton.type = "submit"; renameButton.disabled = controller.state.busy; renameForm.appendChild(renameButton);
      renameForm.addEventListener("submit", function (event) { event.preventDefault(); run(function () { return controller.saveClassification(renameInput.value, renameSelect.value); }); }); management.appendChild(renameForm); parent.appendChild(management);
    }
  }
  function renderProvider(parent, state, detail, item) {
    var sources = sourceRows(detail), provider = state.provider;
    if (provider.guidance) { var guidance = el("p", "library-provider-guidance", provider.guidance); guidance.setAttribute("role", "status"); parent.appendChild(guidance); }
    if (sources.length) { var sourcesHeading = el("h3", "library-section-title", "已关联来源"); sourcesHeading.id = "library-source-choices"; sourcesHeading.tabIndex = -1; parent.appendChild(sourcesHeading); }
    sources.forEach(function (source) {
      var row = el("article", "library-resource"); append(row, el("h3", "", source.provider || source.source || "来源"),
        el("p", "library-meta", "条目 " + text(source.subject_id || source.external_id || source.id)));
      if (source.fetched_at || source.updated_at) row.appendChild(el("p", "library-meta", "更新于 " + text(source.updated_at || source.fetched_at))); parent.appendChild(row);
      if (source.episode_range) row.appendChild(badge("正片第 " + source.episode_range.start + "–" + source.episode_range.end + " 集"));
      else if (source.scope) row.appendChild(badge("范围：" + source.scope));
      else row.appendChild(badge("完整条目"));
      if (source.provider === "bangumi" && (source.subject_id || source.external_id)) {
        var actions = el("div", "library-source-actions");
        append(actions, button("刷新此来源数据", function () { return controller.refreshProvider(source); }, state.busy || state.provider.loading),
          button("更新此来源封面", function () { return controller.previewCover(source.subject_id || source.external_id); }, state.busy || state.provider.loading)); row.appendChild(actions);
      }
    });
    var form = el("form", "library-provider-search"), input = el("input");
    input.type = "search"; input.value = provider.query; input.placeholder = "作品名或 Bangumi 条目 ID";
    input.addEventListener("input", function () { provider.query = input.value; });
    input.id = "library-provider-query";
    labelField(form, "查找来源条目", input); var search = el("button", "library-button", "搜索 Bangumi"); search.id = "library-provider-search"; search.disabled = state.busy || provider.loading; search.type = "submit";
    form.addEventListener("submit", function (event) { event.preventDefault(); if (!provider.loading && !state.busy) controller.searchProvider(input.value); });
    form.appendChild(search); parent.appendChild(form);
    var rangeOptions = el("details", "library-provider-range"); rangeOptions.open = provider.rangeOpen || !!provider.rangeStart || !!provider.rangeEnd;
    rangeOptions.appendChild(el("summary", "", "限定正片集数（可选）"));
    rangeOptions.addEventListener("toggle", function () { provider.rangeOpen = rangeOptions.open; });
    var rangeFields = el("div", "library-range-fields"), rangeStart = labelField(rangeFields, "起始集", el("input")), rangeEnd = labelField(rangeFields, "结束集", el("input"));
    // Keep partial numeric input as text (e.g. "12.") until preview validation;
    // number inputs can silently turn invalid/partial values into empty strings.
    [rangeStart, rangeEnd].forEach(function (field) { field.type = "text"; field.inputMode = "decimal"; field.disabled = state.busy; field.placeholder = "留空"; });
    rangeStart.id = "library-episode-start"; rangeStart.value = provider.rangeStart; rangeEnd.id = "library-episode-end"; rangeEnd.value = provider.rangeEnd;
    function rangeChanged() { controller.setEpisodeRange(rangeStart.value, rangeEnd.value); }
    rangeStart.addEventListener("input", rangeChanged); rangeEnd.addEventListener("input", rangeChanged);
    append(rangeOptions, rangeFields, el("p", "library-meta", "两端都留空时包含完整条目章节；限定范围只筛选正片章节，不改变来源作品的基本信息。")); parent.appendChild(rangeOptions);
    parent.appendChild(el("p", "library-meta", "先保存来源快照，再勾选需要写入本地的字段。目录和压制信息不会被来源覆盖。"));
    if (provider.loading) parent.appendChild(el("p", "library-meta", provider.mode === "cover" ? "正在获取封面并保存到本地…" : "正在获取来源数据…"));
    if (provider.error) { var error = el("p", "library-error", provider.error); error.setAttribute("role", "alert"); parent.appendChild(error); }
    provider.results.forEach(function (result) {
      var row = el("div", "library-provider-result"), previewButton = button("预览差异", function () { return controller.previewProvider(result.id); }, state.busy || provider.loading);
      previewButton.id = "library-provider-result-" + text(result.id);
      append(row, el("div", "", (result.name_cn || result.name || result.title || "未命名") + " · #" + result.id), previewButton); parent.appendChild(row);
    });
    if (!provider.preview) return;
    var preview = provider.preview, differences = array(preview.diff || preview.diffs || preview.changes), panel = el("section", "library-sync-preview");
    var syncHeading = el("h3", "", provider.mode === "cover" ? "确认更新封面" : "确认要写入的字段"); syncHeading.id = "library-sync-heading"; syncHeading.tabIndex = -1;
    var range = preview.episode_range, counts = preview.episode_counts || {}, scope = range ? "正片第 " + range.start + "–" + range.end + " 集" : "完整条目 · 全量章节";
    if (provider.mode === "cover") scope = "仅更新本地封面 · 不修改作品信息和章节";
    else if (counts.selected != null && counts.total != null) scope += " · " + counts.selected + " / " + counts.total + " 条章节";
    append(panel, syncHeading, el("p", "library-preview-scope", scope), el("p", "library-meta", provider.mode === "cover" ?
      "确认后更新本地封面，不修改其他字段。" : "勾选后采用对应字段；别名会与已有内容合并去重，其他勾选字段按右侧内容更新。"));
    var aliasNotice = el("p", "library-provider-guidance library-automatic-aliases"); aliasNotice.setAttribute("role", "note"); panel.appendChild(aliasNotice);
    array(preview.warnings).forEach(function (warning) { panel.appendChild(el("p", "library-meta", typeof warning === "string" ? warning : warning.message)); });
    function automaticAliases() {
      return provider.mode === "cover" || preview.cover_only || provider.selected.indexOf("name") >= 0 ? [] :
        array(preview.automatic_aliases).filter(function (alias) { return typeof alias === "string" && alias.trim(); });
    }
    function applyLabel() { return provider.mode === "cover" ? "确认更新封面" : provider.selected.length ? "确认写入所选字段" : automaticAliases().length ? "确认来源并追加别名" : "仅确认来源关联"; }
    var apply = button(applyLabel(), function () { return controller.applyProvider(preview); }, state.busy || provider.mode === "cover" && !provider.selected.length, "is-primary");
    var differenceRows = [];
    function updateSelection() {
      var additions = automaticAliases(); aliasNotice.hidden = !additions.length;
      aliasNotice.textContent = additions.length ? "确认来源时将自动追加别名：" + additions.map(function (alias) { return "「" + alias + "」"; }).join("、") + "。无需勾选“别名”；已有别名会保留并去重。" : "";
      apply.textContent = applyLabel(); apply.disabled = state.busy || provider.mode === "cover" && !provider.selected.length;
      differenceRows.forEach(function (entry) {
        var effective = effectiveProviderDifference(entry.difference, provider.selected);
        entry.choice.checked = provider.selected.indexOf(entry.key) >= 0;
        entry.choice.disabled = state.busy || !effective.selectable || !effective.changed;
        if (entry.aliasAfter) { clear(entry.aliasAfter); entry.aliasAfter.appendChild(diffValue(effective.after)); }
      });
    }
    differences.forEach(function (difference) {
      var key = difference.field || difference.path, row = el("label", "library-diff"), choice = el("input"); choice.type = "checkbox";
      var aliasAfter = key === "metadata.aliases" ? el("div", "library-diff-value library-aliases-after") : null;
      differenceRows.push({ key: key, difference: difference, choice: choice, aliasAfter: aliasAfter });
      choice.addEventListener("change", function () { controller.selectProviderField(key, choice.checked); updateSelection(); });
      append(row, choice, el("strong", "", difference.label || fieldLabel(key)),
        key === "metadata.cover" ? coverDifference(difference.before_url, "当前封面") : diffValue(difference.before), el("span", "library-diff-arrow", "→"),
        key === "metadata.cover" ? coverDifference(difference.after_url, "来源封面") : aliasAfter || diffValue(difference.after));
      if (difference.reason) row.appendChild(el("p", "library-meta", difference.reason)); panel.appendChild(row);
    });
    updateSelection();
    if (!differences.length) panel.appendChild(el("p", "library-empty", "暂无可写入的差异。"));
    panel.appendChild(apply); parent.appendChild(panel);
  }
  function coverDifference(url, caption) {
    var box = el("div", "library-cover-difference");
    append(box, cover({ name: caption, cover_url: url }), el("span", "library-meta", caption)); return box;
  }
  function diffValue(value) {
    var rendered = displayValue(value);
    if (rendered.length <= 360 && (!Array.isArray(value) || value.length <= 5)) return el("pre", "library-diff-value", rendered);
    var details = el("details", "library-diff-value"); append(details, el("summary", "", Array.isArray(value) ? value.length + " 条记录 · 展开查看" : "较长内容 · 展开查看"), el("pre", "library-diff-value", rendered)); return details;
  }
  function renderDetail(state) {
    clear(ui.detail); if (!state.detailSelector) return;
    var toolbar = el("div", "library-detail-toolbar"); toolbar.appendChild(button("← 返回作品库", function () { controller.closeDetail(); }, state.busy)); ui.detail.appendChild(toolbar);
    if (state.detailLoading) { ui.detail.appendChild(el("p", "library-empty", "正在读取作品…")); return; }
    if (state.detailError) {
      var error = el("p", "library-error", state.detailError); error.setAttribute("role", "alert"); append(ui.detail, error, button("重试", function () { controller.openDetail(state.detailSelector, true); })); return;
    }
    if (!state.detail) return;
    var detail = state.detail, item = detail.item || detail.work || detail, heading = el("h1", "", titleOf(item)); heading.tabIndex = -1; ui.detailHeading = heading;
    var hero = el("header", "library-detail-hero"), info = el("div"); append(info, heading, el("p", "library-meta", [item.domain_label || item.domain, item.year].filter(Boolean).join(" · ")));
    var heroActions = el("div", "library-detail-actions"); append(heroActions,
      button("刷新 Bangumi 数据", function () { return controller.refreshProvider(); }, state.busy || state.provider.loading, "is-primary"),
      button("编辑作品", function () { openEditor(detail); }, state.busy));
    append(hero, info, heroActions); ui.detail.appendChild(hero);
    var layout = el("div", "library-detail-layout"), sidebar = el("aside", "library-detail-sidebar"), content = el("div", "library-detail-content");
    sidebar.setAttribute("aria-label", "封面与作品资料"); append(sidebar, cover(item), workFacts(detail, item));
    var bound = bangumiBindings(detail);
    var subjects = bound.map(function (source) { return text(source.subject_id || source.external_id); }).filter(function (id, index, all) { return all.indexOf(id) === index; });
    sidebar.appendChild(button(subjects.length === 1 ? "更新封面" : "匹配 Bangumi 封面", function () {
      if (subjects.length === 1) return controller.previewCover(subjects[0]);
      controller.setDetailTab("sources");
    }, state.busy || state.provider.loading, "library-cover-action"));
    append(layout, sidebar, content); ui.detail.appendChild(layout);
    var tabs = el("nav", "library-detail-tabs"); tabs.setAttribute("aria-label", "作品详情分区"); tabs.setAttribute("role", "tablist");
    var choices = [["overview", "概览"], ["resources", "资源"], ["chapters", "章节"], ["classifications", "分类"], ["sources", "来源"]];
    choices.forEach(function (pair, index) {
      var active = state.detailTab === pair[0], tab = button(pair[1], function () { controller.setDetailTab(pair[0]); }, false);
      tab.id = "library-detail-tab-" + pair[0]; tab.setAttribute("role", "tab"); tab.setAttribute("aria-selected", active ? "true" : "false"); tab.setAttribute("aria-controls", "library-detail-panel"); tab.tabIndex = active ? 0 : -1;
      tab.addEventListener("keydown", function (event) {
        var next = event.key === "ArrowRight" ? (index + 1) % choices.length : event.key === "ArrowLeft" ? (index + choices.length - 1) % choices.length : event.key === "Home" ? 0 : event.key === "End" ? choices.length - 1 : -1;
        if (next < 0) return; event.preventDefault(); controller.setDetailTab(choices[next][0]); document.getElementById("library-detail-tab-" + choices[next][0]).focus();
      }); tabs.appendChild(tab);
    }); content.appendChild(tabs);
    var panel = el("section", "library-detail-panel"); panel.id = "library-detail-panel"; panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", "library-detail-tab-" + state.detailTab); panel.tabIndex = 0;
    if (state.detailTab === "resources") renderResources(panel, detail, item);
    else if (state.detailTab === "chapters") renderChapters(panel, detail, item);
    else if (state.detailTab === "classifications") renderClassifications(panel, detail, item);
    else if (state.detailTab === "sources") renderProvider(panel, state, detail, item);
    else renderOverview(panel, detail, item);
    content.appendChild(panel);
  }
  function render(_state, reason) {
    if (!controller || !view) return; var state = controller.state, focusedElement = document.activeElement, focusedId = focusedElement && focusedElement.id;
    var selectionStart = reason === "provider-range" && focusedElement ? focusedElement.selectionStart : null;
    var selectionEnd = reason === "provider-range" && focusedElement ? focusedElement.selectionEnd : null;
    ui.browse.hidden = !!state.detailSelector; ui.detail.hidden = !state.detailSelector;
    ui.detail.setAttribute("aria-busy", state.detailLoading || state.provider.loading || state.busy ? "true" : "false");
    // Provider typing and detail updates never replace browse controls/cards.
    if (!state.detailSelector || reason === "browse") renderBrowse(state);
    if (state.detailSelector) renderDetail(state);
    ui.notice.textContent = state.notice; ui.notice.hidden = !state.notice;
    if (reason === "detail" && ui.detailHeading) ui.detailHeading.focus();
    if (reason === "detail-tab") { var tab = document.getElementById("library-detail-tab-" + state.detailTab); if (tab) tab.focus(); }
    if (reason === "provider-search-start" || reason === "provider-source-choose") {
      var startTarget = document.getElementById(reason === "provider-search-start" ? "library-provider-query" : "library-source-choices");
      if (startTarget) startTarget.focus();
    }
    if (reason === "provider" || reason === "provider-range") {
      var focusTarget = state.provider.preview && !state.provider.loading ? document.getElementById("library-sync-heading") : focusedId ? document.getElementById(focusedId) : null;
      if (focusTarget && !focusTarget.disabled) {
        focusTarget.focus();
        if (typeof selectionStart === "number" && typeof focusTarget.setSelectionRange === "function") focusTarget.setSelectionRange(selectionStart, selectionEnd);
      }
    }
    if (reason === "close-detail") { var card = ui.cardButtons && ui.cardButtons[returnFocusKey]; if (card) card.focus(); else ui.search.focus(); }
  }
  function openEditor(detail) {
    if (!controller || controller.state.busy) return;
    var epoch = controller.epoch(), currentController = controller, record = detail ? detail.record || (detail.item || {}).record : template();
    if (!record) { if (context.setStatus) context.setStatus("当前作品缺少完整记录，请重新加载详情。", true); return; }
    var ref = detail ? controller.currentRef() : null, config = context.config || {};
    global.NimdaNewWorkDialog.open({ title: detail ? "编辑作品" : "新增作品", submitLabel: "保存到本地作品库", hideHints: true,
      enumOptions: config.enum_options || {}, enumLabels: config.enum_labels || {}, initialData: global.NimdaWorkRecordForm.fromRecord(record),
      onSubmit: async function (patch) {
        if (controller !== currentController || epoch !== controller.epoch() || !controller.state.active) throw new Error("页面已变化，请关闭窗口后重新编辑。");
        return controller.saveRecord(global.NimdaWorkRecordForm.applyPatch(record, patch), ref);
      } });
  }
  function keydown(event) {
    if (event.key === "Escape" && controller && controller.state.active && controller.state.detailSelector && !document.querySelector("dialog[open]")) { event.preventDefault(); controller.closeDetail(); }
  }
  function mount(nextContext) {
    context = nextContext || context; if (controller) return;
    var shell = document.querySelector(".app-shell") || document.body, tabs = document.querySelector(".app-tabs");
    nav = document.getElementById("tab-catalog-library");
    if (!nav && tabs) { nav = el("button", "app-tab", "作品库"); nav.id = "tab-catalog-library"; nav.type = "button"; nav.setAttribute("role", "tab"); tabs.appendChild(nav); ownedTab = true; }
    view = document.getElementById("catalog-library-view");
    if (!view) { view = el("main", "catalog-library-view"); view.id = "catalog-library-view"; view.hidden = true; shell.appendChild(view); ownedView = true; }
    view.classList.add("catalog-library-view"); clear(view);
    controller = createController({ request: function (url, options) { return (context.fetchJson || global.NimdaCommon.fetchJson)(url, options); },
      setStatus: function (value, error) { if (context.setStatus) context.setStatus(value, error); }, onChange: render,
      mode: global.NimdaCommon.readPreference ? global.NimdaCommon.readPreference("nimda.catalog-library.view") : "cards" });
    ui.browse = el("section", "library-browse"); ui.detail = el("section", "library-detail"); ui.detail.hidden = true;
    ui.notice = el("p", "library-notice"); ui.notice.setAttribute("role", "status"); ui.notice.hidden = true;
    var header = el("header", "library-header"), title = el("div"); ui.count = el("span", "library-total", "本地作品");
    append(title, el("h1", "", "我的作品库"), el("p", "library-subtitle", "动画、音乐与影像，收藏在这里。"));
    var actions = el("div", "library-actions");
    ui.maintenance = el("details", "library-maintenance"); ui.maintenance.hidden = true; ui.maintenance.appendChild(el("summary", "", "维护"));
    ui.initialize = button("初始化本地标识…", function () { return controller.previewIdentities(); }); ui.maintenance.appendChild(ui.initialize);
    ui.refresh = button("刷新本地列表", function () { controller.browse(); }); ui.add = button("＋ 新增作品", function () { openEditor(null); }, false, "is-primary");
    append(actions, ui.maintenance, ui.refresh, ui.add); append(header, title, actions); ui.browse.appendChild(header);
    ui.domains = el("nav", "library-domain-nav"); ui.domains.setAttribute("aria-label", "作品品类"); ui.browse.appendChild(ui.domains);
    var columns = el("div", "library-browse-layout"), mainColumn = el("div", "library-browse-main"), sidebar = el("aside", "library-browse-sidebar");
    sidebar.setAttribute("aria-label", "筛选作品"); append(columns, mainColumn, sidebar); ui.browse.appendChild(columns);
    var filters = el("form", "library-filters"); ui.search = el("input"); ui.search.type = "search"; ui.search.placeholder = "搜索本地作品、别名…";
    ui.search.addEventListener("input", function () { controller.setSearchDraft(ui.search.value); });
    labelField(filters, "搜索作品", ui.search); var search = el("button", "library-button", "搜索"); search.type = "submit"; filters.appendChild(search);
    filters.addEventListener("submit", function (event) { event.preventDefault(); controller.setFilters({ query: ui.search.value }); });
    mainColumn.appendChild(filters);
    var yearSection = el("section", "library-sidebar-section"); yearSection.appendChild(el("h2", "library-section-title", "按时间浏览"));
    ui.year = labelField(yearSection, "年份", el("select")); ui.year.addEventListener("change", function () { controller.setFilters({ year: ui.year.value }); });
    ui.yearLinks = el("div", "library-year-links"); yearSection.appendChild(ui.yearLinks); sidebar.appendChild(yearSection);
    var seriesSection = el("section", "library-sidebar-section"); seriesSection.appendChild(el("h2", "library-section-title", "系列与分类"));
    ui.series = labelField(seriesSection, "系列", el("select")); ui.series.addEventListener("change", function () { controller.setFilters({ classification_id: ui.series.value }); }); sidebar.appendChild(seriesSection);
    ui.resetFilters = button("清除筛选", function () { return controller.setFilters({ query: "", domain: "", year: "", classification_id: "" }); }, true, "library-text-button"); sidebar.appendChild(ui.resetFilters);
    ui.activeFilters = el("div", "library-active-filters"); mainColumn.appendChild(ui.activeFilters);
    var layout = el("div", "library-layout-tools"); layout.appendChild(ui.count);
    ui.sort = labelField(layout, "排序", el("select")); [["name", "名称"], ["date_desc", "日期从新到旧"], ["date_asc", "日期从旧到新"], ["series_order", "系列内顺序"]].forEach(function (pair) { option(ui.sort, pair[0], pair[1]); }); ui.sort.addEventListener("change", function () { controller.setFilters({ sort: ui.sort.value }); });
    var modes = el("div", "library-view-modes"); modes.setAttribute("role", "group"); modes.setAttribute("aria-label", "展示方式");
    ui.cards = button("卡片", function () { controller.setMode("cards"); if (global.NimdaCommon.writePreference) global.NimdaCommon.writePreference("nimda.catalog-library.view", "cards"); });
    ui.list = button("列表", function () { controller.setMode("list"); if (global.NimdaCommon.writePreference) global.NimdaCommon.writePreference("nimda.catalog-library.view", "list"); }); append(modes, ui.cards, ui.list); layout.appendChild(modes); mainColumn.appendChild(layout);
    ui.identity = el("section", "library-identity-preview"); ui.results = el("div", "library-results"); ui.pagination = el("nav", "library-pagination"); ui.pagination.setAttribute("aria-label", "作品分页");
    append(mainColumn, ui.identity, ui.results, ui.pagination); append(view, ui.notice, ui.browse, ui.detail); document.addEventListener("keydown", keydown); render();
  }
  function dispose() {
    if (controller) controller.dispose(); controller = null; document.removeEventListener("keydown", keydown);
    if (ownedView && view) view.remove(); else if (view) clear(view);
    if (ownedTab && nav) nav.remove(); view = nav = null; ui = {}; ownedView = ownedTab = false;
  }
  global.NimdaCatalogLibrary = { createController: createController, localCover: localCover, selector: selector, dateLabel: dateLabel, fieldLabel: fieldLabel, episodeRange: episodeRange };
  global.NimdaCommon.ensureFeatureRegistry(global).register({ id: "catalog-library", label: "作品库", tabId: "tab-catalog-library", viewId: "catalog-library-view", order: 5,
    init: mount, activate: function (nextContext) { mount(nextContext); context = nextContext || context; controller.activate(); },
    deactivate: function () { if (controller) controller.deactivate(); },
    refreshAfterConfig: function (nextContext) { context = nextContext || context; if (controller && controller.state.active) controller.browse(); }, dispose: dispose });
})(window);
