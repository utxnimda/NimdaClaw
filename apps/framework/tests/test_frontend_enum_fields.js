"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const filename = path.resolve(__dirname, "../frontend/src/common/enum-fields.js");
const context = vm.createContext({ window: {} });
vm.runInContext(fs.readFileSync(filename, "utf8"), context, { filename });
const enums = context.window.NimdaEnumFields;
const plain = (value) => JSON.parse(JSON.stringify(value));

function deepFreeze(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}

test("the helper exposes the four shared enum operations without a DOM", () => {
  for (const name of ["rawEnumOptSlug", "values", "choices", "fieldMode"]) {
    assert.equal(typeof enums[name], "function", name);
  }
});

test("raw slugs trim strings and stringify primitive and array entries", () => {
  for (const [entry, expected] of [
    [null, ""], [undefined, ""], ["", ""], ["   ", ""],
    ["  JP  ", "JP"], [0, "0"], [false, "false"], [true, "true"],
    [[], ""], [["JP"], "JP"], [["JP", "US"], "JP,US"],
  ]) {
    assert.equal(enums.rawEnumOptSlug(entry), expected);
  }
});

test("object options use only value, never their label or legacy-looking keys", () => {
  for (const [entry, expected] of [
    [{ value: "  JP  ", label: "Japan" }, "JP"],
    [{ value: 0 }, "0"], [{ value: false }, "false"],
    [{ value: null, label: "Japan" }, ""], [{ value: undefined }, ""],
    [{ value: "  ", label: "Japan" }, ""], [{}, ""],
    [{ label: "Japan", slug: "JP", name: "JP", id: "JP" }, ""],
  ]) {
    assert.equal(enums.rawEnumOptSlug(entry), expected);
  }
});

test("values tolerate absent or malformed configuration without inventing defaults", () => {
  for (const options of [undefined, null, {}, { country: null }, { country: "JP" }, { country: {} }]) {
    assert.deepEqual(plain(enums.values(options, "country")), []);
    assert.deepEqual(plain(enums.choices(options, undefined, "country", "")), []);
  }
  assert.deepEqual(plain(enums.values(Object.create({ country: ["JP"] }), "country")), []);
});

test("values normalize strings and objects, drop empty slugs and deduplicate in source order", () => {
  const options = { country: [
    "  JP ", { value: "US", label: "United States" }, "JP", { value: " US " },
    null, undefined, "", "  ", {}, { label: "Unknown" }, { value: "" },
    0, { value: 0 }, false, "false", "GB",
  ] };
  assert.deepEqual(plain(enums.values(options, "country")), ["JP", "US", "0", "false", "GB"]);
});

test("prototype-looking slugs remain valid values and cannot corrupt deduplication", () => {
  const slugs = ["__proto__", "constructor", "toString", "hasOwnProperty"];
  const options = { country: [...slugs, ...slugs.map((value) => ({ value }))] };
  assert.deepEqual(plain(enums.values(options, "country")), slugs);
  assert.deepEqual(plain(enums.values(options, "country")), slugs);
  assert.equal(Object.getPrototypeOf(options), Object.prototype);
});

test("own __proto__ enum keys and label keys work without reading inherited configuration", () => {
  const options = JSON.parse('{"__proto__":["__proto__","constructor","__proto__"]}');
  const labels = JSON.parse('{"__proto__":{"__proto__":" Prototype ","constructor":" Constructor "}}');
  assert.deepEqual(plain(enums.values(options, "__proto__")), ["__proto__", "constructor"]);
  assert.deepEqual(plain(enums.choices(options, labels, "__proto__", "")), [
    { value: "__proto__", label: "Prototype" },
    { value: "constructor", label: "Constructor" },
  ]);
  assert.deepEqual(plain(enums.values({}, "__proto__")), []);
  assert.deepEqual(plain(enums.choices({ country: ["__proto__", "constructor", "toString"] }, {}, "country", "")), [
    { value: "__proto__", label: "__proto__" },
    { value: "constructor", label: "constructor" },
    { value: "toString", label: "toString" },
  ]);
});

test("choices use trimmed configured labels and fall back to the slug for empty labels", () => {
  const options = { country: ["JP", "US", "GB", "DE", "FR", "ZERO", "FALSE"] };
  const labels = { country: { JP: "  Japan  ", US: " ", GB: null, DE: undefined, ZERO: 0, FALSE: false } };
  assert.deepEqual(plain(enums.choices(options, labels, "country", null)), [
    { value: "JP", label: "Japan" },
    { value: "US", label: "US" },
    { value: "GB", label: "GB" },
    { value: "DE", label: "DE" },
    { value: "FR", label: "FR" },
    { value: "ZERO", label: "0" },
    { value: "FALSE", label: "false" },
  ]);
});

test("choices do not read inherited labels and tolerate missing or malformed label maps", () => {
  const options = { country: ["JP"] };
  const expected = [{ value: "JP", label: "JP" }];
  for (const labels of [
    undefined, null, {}, { country: null }, { country: "Japan" },
    Object.create({ country: { JP: "Japan" } }),
    { country: Object.create({ JP: "Japan" }) },
  ]) {
    assert.deepEqual(plain(enums.choices(options, labels, "country", "")), expected);
  }
});

test("unknown historical current values are preserved verbatim even with no configured options", () => {
  for (const current of ["Legacy Country", "  JP  ", "   ", "__proto__"]) {
    assert.deepEqual(plain(enums.choices({}, {}, "country", current)), [{ value: current, label: current }]);
  }
  assert.deepEqual(plain(enums.choices(null, null, "country", 0)), [{ value: "0", label: "0" }]);
  assert.deepEqual(plain(enums.choices(null, null, "country", false)), [{ value: "false", label: "false" }]);
});

test("unknown current values are prepended without replacing or rewriting configured choices", () => {
  const options = { country: ["JP", "US"] };
  const labels = { country: { "  JP  ": " Historical Japan ", JP: "Japan" } };
  assert.deepEqual(plain(enums.choices(options, labels, "country", "  JP  ")), [
    { value: "  JP  ", label: "Historical Japan" },
    { value: "JP", label: "Japan" },
    { value: "US", label: "US" },
  ]);
});

test("an already-configured current value is neither duplicated nor moved to the front", () => {
  const expected = [{ value: "JP", label: "JP" }, { value: "US", label: "US" }];
  for (const current of ["US", "", undefined, null]) {
    assert.deepEqual(plain(enums.choices({ country: ["JP", "US", "US"] }, {}, "country", current)), expected);
  }
});

test("normalizing values and preserving current values never mutate caller configuration", () => {
  const options = deepFreeze({ country: ["  JP ", { value: " US ", label: "Ignored entry label" }, "JP"] });
  const labels = deepFreeze({ country: { JP: " Japan ", US: " United States " } });
  const beforeOptions = JSON.stringify(options);
  const beforeLabels = JSON.stringify(labels);
  const values = enums.values(options, "country");
  const choices = enums.choices(options, labels, "country", "Legacy");
  values.push("Another");
  choices[1].label = "Changed output";
  choices.push({ value: "Another", label: "Another" });
  assert.equal(JSON.stringify(options), beforeOptions);
  assert.equal(JSON.stringify(labels), beforeLabels);
  assert.deepEqual(plain(enums.values(options, "country")), ["JP", "US"]);
  assert.deepEqual(plain(enums.choices(options, labels, "country", "Legacy")), [
    { value: "Legacy", label: "Legacy" },
    { value: "JP", label: "Japan" },
    { value: "US", label: "United States" },
  ]);
});

test("field modes distinguish closed enums, editable suggestions and ordinary text", () => {
  for (const key of ["domain", "country", "release_type"]) assert.equal(enums.fieldMode(key), "select", key);
  for (const key of ["press_format", "press_group", "markers"]) assert.equal(enums.fieldMode(key), "datalist", key);
  for (const key of ["title", "DOMAIN", "country ", "__proto__", "constructor", "", null, undefined]) {
    assert.equal(enums.fieldMode(key), "text");
  }
});
