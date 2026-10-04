(function () {
  "use strict";

  // Enum values and labels come only from the shared /api/config payload.
  function rawEnumOptSlug(entry) {
    if (entry == null) return "";
    if (typeof entry === "object" && !Array.isArray(entry)) {
      return entry.value != null ? String(entry.value).trim() : "";
    }
    return String(entry).trim();
  }
  function values(enumOptions, key) {
    var source = enumOptions && Object.prototype.hasOwnProperty.call(enumOptions, key) ? enumOptions[key] : null;
    var seen = Object.create(null), output = [];
    (Array.isArray(source) ? source : []).forEach(function (entry) {
      var value = rawEnumOptSlug(entry);
      if (!value || seen[value]) return;
      seen[value] = true; output.push(value);
    });
    return output;
  }
  function choices(enumOptions, enumLabels, key, current) {
    var options = values(enumOptions, key);
    // Preserve the exact existing value, including legacy spellings, without
    // silently selecting a configured replacement or changing the user's draft.
    var existing = current == null ? "" : String(current);
    if (existing && options.indexOf(existing) < 0) options.unshift(existing);
    var labels = enumLabels && Object.prototype.hasOwnProperty.call(enumLabels, key) ? enumLabels[key] : null;
    return options.map(function (value) {
      var label = labels && typeof labels === "object" && Object.prototype.hasOwnProperty.call(labels, value) ? labels[value] : null;
      label = label == null ? "" : String(label).trim();
      return { value: value, label: label || value };
    });
  }
  function fieldMode(key) {
    if (["domain", "country", "release_type"].indexOf(key) >= 0) return "select";
    if (["press_format", "press_group", "markers"].indexOf(key) >= 0) return "datalist";
    return "text";
  }
  window.NimdaEnumFields = { rawEnumOptSlug: rawEnumOptSlug, values: values, choices: choices, fieldMode: fieldMode };
})();
