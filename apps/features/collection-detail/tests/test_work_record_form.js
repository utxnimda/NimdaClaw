"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const window = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../frontend/work-record-form.js"), "utf8"), { window });
const adapter = window.NimdaWorkRecordForm;
const plain = value => JSON.parse(JSON.stringify(value));
const attr = (record, kind) => record.attributes.find(row => row.type === kind);
const collection = record => attr(record, "collection-type").data;

function record() {
  return { vendor: { id: "external", flags: ["keep"] }, attributes: [
    { type: "name", data: "Original", description: "name metadata" },
    { type: "country", data: "japan", external: 5 },
    { type: "date", data: { start: "20050101", end: "20050331", precision: "day" }, description: "date metadata" },
    { type: "collection-type", description: "collection metadata", data: {
      domain: "animation", release_type: "tv", path: "U:/Original", markers: ["collected"], extra: { source: "legacy" },
      collectioned: [
        { press_format: "BDRip", press_group: "VCB", press_path: "Original_BDRip", codec: "AVC", vendor: { slot: 1 } },
        { press_format: "1080p", press_group: "", press_path: "Original_1080p", codec: "HEVC", vendor: { slot: 2 } }
      ],
      continuations: [{ title: "Bonus", metadata: { untouched: true }, collectioned: [
        { press_format: "DVDRip", press_group: "Jsum", press_path: "Extras", codec: "MPEG2" }
      ] }]
    } },
    { type: "custom", data: { nested: [1, { keep: true }] }, description: "unknown attribute" }
  ] };
}

test("full record form round trip preserves extensions and continuation records", () => {
  const before = record(), original = plain(before), form = adapter.fromRecord(before);
  assert.deepEqual(Object.keys(form).sort(), ["name", "country", "domain", "release_type", "date", "path", "markers", "collectioned_ordered"].sort());
  assert.equal(form.collectioned_ordered.length, 2);
  assert.ok(form.collectioned_ordered.every(row => row.segment === "main"));
  assert.equal(form.collectioned_ordered[0].codec, "AVC");
  assert.deepEqual(plain(adapter.applyPatch(before, form)), original);
  assert.deepEqual(before, original);
});

test("form projections and resulting full records do not alias the input", () => {
  const before = record(), form = adapter.fromRecord(before);
  form.markers.push("changed"); form.collectioned_ordered[0].vendor.slot = 99;
  assert.deepEqual(collection(before).markers, ["collected"]);
  assert.equal(collection(before).collectioned[0].vendor.slot, 1);
  const result = adapter.applyPatch(before, form);
  result.vendor.flags.push("different"); collection(result).collectioned[0].vendor.slot = 100;
  assert.deepEqual(before.vendor.flags, ["keep"]);
  assert.equal(form.collectioned_ordered[0].vendor.slot, 99);
});

test("known field changes retain attribute metadata, date extensions and unrelated fields", () => {
  const before = record();
  const result = adapter.applyPatch(before, { name: "Changed", country: "korea", date: { start: "20060401" },
    domain: "series", release_type: "movie", path: "S:/Changed", markers: ["updated"] });
  assert.equal(attr(result, "name").data, "Changed");
  assert.equal(attr(result, "name").description, "name metadata");
  assert.equal(attr(result, "country").external, 5);
  assert.deepEqual(plain(attr(result, "date").data), { start: "20060401", end: "20050331", precision: "day" });
  assert.equal(attr(result, "collection-type").description, "collection metadata");
  assert.deepEqual(plain(collection(result).extra), collection(before).extra);
  assert.deepEqual(plain(collection(result).continuations), collection(before).continuations);
  assert.deepEqual(plain(collection(result).collectioned), collection(before).collectioned);
  assert.deepEqual(plain(attr(result, "custom")), attr(before, "custom"));
});

test("removing, reordering and editing rows keeps metadata with the submitted row", () => {
  const before = record(), patch = adapter.fromRecord(before);
  patch.collectioned_ordered.reverse(); patch.collectioned_ordered[0].press_group = "New group";
  let result = adapter.applyPatch(before, patch);
  assert.equal(collection(result).collectioned[0].codec, "HEVC");
  assert.equal(collection(result).collectioned[0].vendor.slot, 2);
  assert.equal(collection(result).collectioned[0].press_group, "New group");
  assert.equal(collection(result).collectioned[1].codec, "AVC");
  patch.collectioned_ordered.splice(1, 1);
  patch.collectioned_ordered.push({ press_format: "720p", press_group: "", press_path: "New", segment: "main", custom: { added: true } });
  result = adapter.applyPatch(before, patch);
  assert.equal(collection(result).collectioned.length, 2);
  assert.equal(collection(result).collectioned[0].vendor.slot, 2);
  assert.deepEqual(plain(collection(result).collectioned[1]), { press_format: "720p", press_group: "", press_path: "New", custom: { added: true } });
  assert.deepEqual(plain(collection(result).continuations), collection(before).continuations);
  assert.ok(patch.collectioned_ordered.every(row => row.segment === "main"));
});

test("clearing main rows never clears advanced continuations", () => {
  const before = record(), result = adapter.applyPatch(before, { collectioned_ordered: [] });
  assert.deepEqual(plain(collection(result).collectioned), []);
  assert.deepEqual(plain(collection(result).continuations), collection(before).continuations);
});

test("partial patches do not synthesize edits to missing fields or normalize values", () => {
  const before = record();
  assert.deepEqual(plain(adapter.applyPatch(before, {})), before);
  const result = adapter.applyPatch(before, { name: "  Kept verbatim  ", date: { start: "2006-04-01" } });
  assert.equal(attr(result, "name").data, "  Kept verbatim  ");
  assert.equal(attr(result, "date").data.start, "2006-04-01");
  assert.deepEqual(plain(collection(result)), collection(before));
});

test("new records can fill absent known attributes without deleting extension attributes", () => {
  const before = { extra: 8, attributes: [{ type: "custom", data: "keep" }] };
  const defaults = adapter.fromRecord(before);
  assert.equal(defaults.name, ""); assert.deepEqual(plain(defaults.collectioned_ordered), []);
  const result = adapter.applyPatch(before, { name: "New", country: "japan", date: { start: "", end: "" },
    domain: "animation", release_type: "tv", path: "U:/New", markers: [], collectioned_ordered: [] });
  assert.equal(result.extra, 8); assert.equal(attr(result, "custom").data, "keep");
  assert.equal(attr(result, "name").data, "New"); assert.equal(collection(result).path, "U:/New");
  assert.equal(before.attributes.length, 1);
});

test("non-main rows are rejected instead of overwriting or flattening continuations", () => {
  for (const segment of ["continuation", "", null, 1]) {
    const before = record(), original = plain(before);
    assert.throws(() => adapter.applyPatch(before, { collectioned_ordered: [{ press_format: "BDRip", press_group: "", segment }] }), /main/);
    assert.deepEqual(before, original);
  }
});

test("malformed patch structures are rejected without changing the record", () => {
  const bad = [null, [], { name: null }, { date: null }, { date: [] }, { date: { start: 20050101 } },
    { path: {} }, { markers: "bad" }, { markers: [null] }, { collectioned_ordered: null },
    { collectioned_ordered: [null] }, { collectioned_ordered: [{ press_group: {} }] }];
  for (const patch of bad) {
    const before = record(), original = plain(before);
    assert.throws(() => adapter.applyPatch(before, patch)); assert.deepEqual(before, original);
  }
});

test("malformed raw structures are rejected instead of causing accidental field replacement", () => {
  const malformed = [null, [], {}, { attributes: null }, { attributes: [null] },
    { attributes: [{ type: "name", data: 1 }] }, { attributes: [{ type: "date", data: null }] },
    { attributes: [{ type: "collection-type", data: { collectioned: [null] } }] },
    { attributes: [{ type: "collection-type", data: { continuations: [null] } }] }];
  const duplicate = record(); duplicate.attributes.push({ type: "name", data: "Other" }); malformed.push(duplicate);
  for (const raw of malformed) {
    assert.throws(() => adapter.fromRecord(raw)); assert.throws(() => adapter.applyPatch(raw, {}));
  }
});

test("non-JSON extension values are not silently discarded or converted", () => {
  for (const value of [undefined, Infinity, NaN, () => {}]) {
    const raw = record(); raw.vendor.invalid = value;
    assert.throws(() => adapter.fromRecord(raw), /JSON/); assert.throws(() => adapter.applyPatch(raw, {}), /JSON/);
  }
  const cyclic = record(); cyclic.vendor.self = cyclic;
  assert.throws(() => adapter.fromRecord(cyclic), /JSON/);
});
