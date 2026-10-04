(function () {
  "use strict";

  function ensureRegistry(target) {
    return window.NimdaCommon.ensureFeatureRegistry(target);
  }

  function create(options) {
    options = options || {};
    var doc = options.document || window.document;
    var registry = options.registry || ensureRegistry(window);
    var activeId = options.initialId || "collection-detail";
    var configById = Object.create(null);
    var initialized = false;
    var mounted = false;
    var disposed = false;
    var listeners = [];

    function context() {
      return typeof options.getContext === "function" ? options.getContext() : {};
    }
    function features() {
      var seen = Object.create(null);
      var raw = Array.isArray(registry.features) ? registry.features : [];
      raw.forEach(function (feature) {
        if (!feature || !feature.id) return;
        var item = {};
        Object.keys(feature).forEach(function (key) { item[key] = feature[key]; });
        var config = configById[item.id] || {};
        if (config.label) item.label = config.label;
        if (config.order != null) item.order = config.order;
        seen[item.id] = item;
      });
      return Object.keys(seen).map(function (id) { return seen[id]; })
        .sort(function (left, right) { return (Number(left.order) || 0) - (Number(right.order) || 0); });
    }
    function featureById(id) {
      var list = features();
      for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
      return null;
    }
    function syncTabs() {
      var list = features();
      var nav = doc.querySelector(".app-tabs");
      list.forEach(function (feature) {
        var on = feature.id === activeId;
        var button = doc.getElementById(feature.tabId);
        var view = doc.getElementById(feature.viewId);
        if (button) {
          button.classList.toggle("is-active", on);
          button.classList.toggle("on", on);
          button.setAttribute("aria-selected", on ? "true" : "false");
          if (feature.label) button.textContent = feature.label;
          if (nav && button.parentElement === nav) nav.appendChild(button);
        }
        if (view) view.hidden = !on;
      });
      doc.body.setAttribute("data-active-tab", activeId);
      if (typeof options.onActiveChanged === "function") options.onActiveChanged(activeId);
    }
    function initialize() {
      if (initialized) return;
      initialized = true;
      var current = context();
      features().forEach(function (feature) {
        if (typeof feature.init === "function") feature.init(current);
      });
    }
    function setActive(id) {
      if (disposed) return;
      initialize();
      var list = features();
      var nextId = featureById(id) ? id : (list.length ? list[0].id : "collection-detail");
      if (activeId === nextId) return;
      var previous = featureById(activeId);
      var next = featureById(nextId);
      var current = context();
      if (typeof options.onBeforeTabChange === "function") options.onBeforeTabChange(activeId, nextId);
      activeId = nextId;
      syncTabs();
      if (previous && typeof previous.deactivate === "function") previous.deactivate(current);
      if (next && typeof next.activate === "function") next.activate(current);
    }
    function configure(config) {
      if (disposed) return;
      var next = Object.create(null);
      var list = config && Array.isArray(config.features) ? config.features : [];
      list.forEach(function (item) {
        if (!item || typeof item !== "object") return;
        var id = String(item.id || "").trim();
        if (!id) return;
        var value = {};
        if (item.label != null && String(item.label).trim() !== "") value.label = String(item.label).trim();
        var order = Number(item.order);
        if (Number.isFinite(order)) value.order = order;
        next[id] = value;
      });
      configById = next;
      syncTabs();
    }
    function refresh() {
      if (disposed) return;
      var active = featureById(activeId);
      if (active && typeof active.refreshAfterConfig === "function") active.refreshAfterConfig(context());
    }
    function mount() {
      if (mounted || disposed) return;
      mounted = true;
      initialize();
      var list = features();
      list.forEach(function (feature) {
        var button = doc.getElementById(feature.tabId);
        if (!button) return;
        var click = function () { setActive(feature.id); };
        button.addEventListener("click", click);
        listeners.push(function () { button.removeEventListener("click", click); });
      });
      if (!featureById(activeId)) activeId = list.length ? list[0].id : "collection-detail";
      syncTabs();
    }
    function dispose() {
      if (disposed) return;
      disposed = true;
      listeners.splice(0).forEach(function (remove) { remove(); });
      features().forEach(function (feature) {
        if (typeof feature.dispose === "function") feature.dispose();
      });
    }
    return { mount: mount, dispose: dispose, getActiveId: function () { return activeId; },
      setActive: setActive, configure: configure, refresh: refresh };
  }

  window.NimdaFeatureHost = { create: create, ensureRegistry: ensureRegistry };
})();
