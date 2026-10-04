(function () {
  "use strict";

  // Framework-only primitives: no feature names, catalog fields or disk rules.
  function ensureFeatureRegistry(target) {
    var registry = target.JpTvBrowseFeatureRegistry || {};
    if (!Array.isArray(registry.features)) registry.features = [];
    registry.register = function (feature) {
      if (!feature || !feature.id) return;
      var features = Array.isArray(this.features) ? this.features : (this.features = []);
      for (var i = 0; i < features.length; i++) {
        if (features[i] && features[i].id === feature.id) {
          if (features[i] !== feature && typeof features[i].dispose === "function") {
            try { features[i].dispose(); } catch (_error) {}
          }
          features[i] = feature;
          return;
        }
      }
      features.push(feature);
    };
    target.JpTvBrowseFeatureRegistry = registry;
    return registry;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function readPreference(key, storage) {
    try { return (storage || window.localStorage).getItem(key); }
    catch (_error) { return null; }
  }

  function writePreference(key, value, storage) {
    try {
      var target = storage || window.localStorage;
      if (value === null) target.removeItem(key);
      else target.setItem(key, value);
      return true;
    } catch (_error) { return false; }
  }

  async function fetchJson(url, options) {
    if (window.NimdaOperationCenter) return window.NimdaOperationCenter.fetchJson(url, options);
    var res = await window.fetch(url, options || {});
    var data = await res.json().catch(function () { return {}; });
    return { res: res, data: data };
  }

  function httpErrorHint(status, label) {
    var message = (label || "加载") + "失败（HTTP " + status + "）";
    if (status === 404 || status === 405) {
      message += "：服务接口不可用，请确认使用的是最新应用并重新启动。";
    }
    return message;
  }

  window.NimdaCommon = Object.assign(window.NimdaCommon || {}, {
    ensureFeatureRegistry: ensureFeatureRegistry,
    escapeHtml: escapeHtml,
    readPreference: readPreference,
    writePreference: writePreference,
    fetchJson: fetchJson,
    httpErrorHint: httpErrorHint,
  });
})();
