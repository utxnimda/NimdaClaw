(function (global) {
  "use strict";

  // Form fields are a projection, not a replacement for the complete DB record.
  // In particular, the shared new-work form edits only the main press rows.
  var own = function (value, key) { return Object.prototype.hasOwnProperty.call(value, key); };
  var object = function (value) { return value !== null && typeof value === "object" && !Array.isArray(value); };

  function clone(value) {
    try {
      return JSON.parse(JSON.stringify(value, function (_key, item) {
        if (typeof item === "undefined" || typeof item === "function" || typeof item === "symbol" ||
            typeof item === "number" && !Number.isFinite(item)) throw new Error("Not JSON data");
        return item;
      }));
    } catch (_error) { throw new Error("作品信息必须是有限、无循环的 JSON 数据"); }
  }
  function requireObject(value, label) {
    if (!object(value)) throw new Error(label + " 必须是对象");
  }
  function stringFields(value, keys, label) {
    keys.forEach(function (key) {
      if (own(value, key) && typeof value[key] !== "string") throw new Error(label + "." + key + " 必须是字符串");
    });
  }
  function markers(value, label) {
    if (!Array.isArray(value) || value.some(function (item) { return typeof item !== "string"; })) {
      throw new Error(label + " 必须是字符串数组");
    }
  }
  function pressRows(value, label, formRows) {
    if (!Array.isArray(value)) throw new Error(label + " 必须是对象数组");
    value.forEach(function (row, index) {
      requireObject(row, label + "[" + index + "]");
      stringFields(row, ["press_format", "press_group", "press_path"], label + "[" + index + "]");
      if (formRows && own(row, "segment") && row.segment !== "main") {
        throw new Error(label + " 只支持 main 压制行，不能修改 continuation");
      }
    });
  }
  function attributes(record) {
    requireObject(record, "record");
    if (!Array.isArray(record.attributes)) throw new Error("record.attributes 必须是对象数组");
    var found = Object.create(null);
    record.attributes.forEach(function (attr, index) {
      requireObject(attr, "attributes[" + index + "]");
      if (typeof attr.type !== "string") throw new Error("attributes.type 必须是字符串");
      if (["name", "country", "date", "collection-type"].indexOf(attr.type) < 0) return;
      if (own(found, attr.type)) throw new Error("作品记录存在重复属性：" + attr.type);
      found[attr.type] = attr;
      if (attr.type === "name" || attr.type === "country") {
        if (typeof attr.data !== "string") throw new Error(attr.type + ".data 必须是字符串");
        return;
      }
      requireObject(attr.data, attr.type + ".data");
      if (attr.type === "date") { stringFields(attr.data, ["start", "end"], "date.data"); return; }
      stringFields(attr.data, ["domain", "release_type", "path"], "collection-type.data");
      if (own(attr.data, "markers")) markers(attr.data.markers, "collection-type.data.markers");
      if (own(attr.data, "collectioned")) pressRows(attr.data.collectioned, "collectioned", false);
      if (own(attr.data, "continuations")) {
        if (!Array.isArray(attr.data.continuations)) throw new Error("continuations 必须是对象数组");
        attr.data.continuations.forEach(function (part, position) {
          requireObject(part, "continuations[" + position + "]");
          if (own(part, "collectioned")) pressRows(part.collectioned, "continuations.collectioned", false);
        });
      }
    });
    return found;
  }
  function ensure(record, found, kind, fallback) {
    if (!found[kind]) {
      found[kind] = { type: kind, data: fallback };
      record.attributes.push(found[kind]);
    }
    return found[kind].data;
  }

  function fromRecord(record) {
    var safe = clone(record), found = attributes(safe);
    var date = found.date ? found.date.data : {}, collection = found["collection-type"] ? found["collection-type"].data : {};
    return {
      name: found.name ? found.name.data : "", country: found.country ? found.country.data : "",
      domain: collection.domain || "", release_type: collection.release_type || "",
      date: { start: date.start || "", end: date.end || "" }, path: collection.path || "",
      markers: collection.markers || [],
      collectioned_ordered: (collection.collectioned || []).map(function (row) {
        row.segment = "main";
        return row;
      })
    };
  }

  function applyPatch(record, patch) {
    requireObject(patch, "patch");
    stringFields(patch, ["name", "country", "domain", "release_type", "path"], "patch");
    if (own(patch, "date")) { requireObject(patch.date, "patch.date"); stringFields(patch.date, ["start", "end"], "patch.date"); }
    if (own(patch, "markers")) markers(patch.markers, "patch.markers");
    if (own(patch, "collectioned_ordered")) pressRows(patch.collectioned_ordered, "collectioned_ordered", true);
    var next = clone(record), found = attributes(next);
    ["name", "country"].forEach(function (kind) {
      if (!own(patch, kind)) return;
      ensure(next, found, kind, ""); found[kind].data = patch[kind];
    });
    if (own(patch, "date")) {
      var dates = ensure(next, found, "date", {});
      ["start", "end"].forEach(function (field) { if (own(patch.date, field)) dates[field] = patch.date[field]; });
    }
    ["domain", "release_type", "path", "markers"].forEach(function (field) {
      if (own(patch, field)) ensure(next, found, "collection-type", {})[field] = clone(patch[field]);
    });
    if (own(patch, "collectioned_ordered")) {
      ensure(next, found, "collection-type", {}).collectioned = clone(patch.collectioned_ordered).map(function (row) {
        // `segment` belongs to the flat form protocol, not a raw main row.
        // All other row metadata travels with its row across reorder/removal.
        delete row.segment;
        return row;
      });
    }
    return next;
  }

  global.NimdaWorkRecordForm = { fromRecord: fromRecord, applyPatch: applyPatch };
})(window);
