(function () {
  "use strict";

  var THEME_KEY = "jp-tv-browse-theme";
  var FONT_KEY = "jp-tv-browse-font";
  var THEMES = ["midnight", "paper", "forest", "rose", "contrast", "ocean", "sunset", "slate", "sakura"];
  var FONTS = ["reference", "system", "yahei", "song", "mono"];

  function create(options) {
    options = options || {};
    var doc = options.document || window.document;
    var mounted = false;
    var listeners = [];

    function read(key) {
      return window.NimdaCommon.readPreference(key, options.storage);
    }
    function save(key, value) {
      return window.NimdaCommon.writePreference(key, value, options.storage);
    }
    function applyTheme(value) {
      var theme = THEMES.indexOf(value) >= 0 ? value : "midnight";
      if (theme === "midnight") doc.documentElement.removeAttribute("data-theme");
      else doc.documentElement.setAttribute("data-theme", theme);
      save(THEME_KEY, theme);
      var select = doc.getElementById("theme-select");
      if (select && select.value !== theme) select.value = theme;
      return theme;
    }
    function applyFont(value) {
      var font = FONTS.indexOf(value) >= 0 ? value : "reference";
      doc.documentElement.setAttribute("data-font", font);
      save(FONT_KEY, font);
      var select = doc.getElementById("font-select");
      if (select && select.value !== font) select.value = font;
      return font;
    }
    function bind(id, callback) {
      var select = doc.getElementById(id);
      if (!select) return;
      select.addEventListener("change", callback);
      listeners.push(function () { select.removeEventListener("change", callback); });
    }
    function mount() {
      if (mounted) return;
      mounted = true;
      applyTheme(read(THEME_KEY));
      applyFont(read(FONT_KEY));
      bind("theme-select", function () {
        var value = applyTheme(doc.getElementById("theme-select").value);
        if (typeof options.onThemeChanged === "function") options.onThemeChanged(value);
      });
      bind("font-select", function () {
        var value = applyFont(doc.getElementById("font-select").value);
        if (typeof options.onFontChanged === "function") options.onFontChanged(value);
      });
    }
    function dispose() {
      listeners.splice(0).forEach(function (remove) { remove(); });
      mounted = false;
    }
    return { mount: mount, dispose: dispose, applyTheme: applyTheme, applyFont: applyFont };
  }

  window.NimdaAppearance = { create: create };
})();
