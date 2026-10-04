"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = path.resolve(__dirname, "../frontend/new-work-dialog.js");
const ENUM_FIELDS = path.resolve(__dirname, "../../../framework/frontend/src/common/enum-fields.js");

function setup(extra = {}) {
  let document;
  class Element {
    constructor(tag) {
      this.tagName = tag;
      this.children = [];
      this.attributes = {};
      this.listeners = {};
      this.value = "";
      this.hidden = false;
      this.disabled = false;
      this.textContent = "";
      this.className = "";
    }
    get value() { return this._value || ""; }
    set value(value) { this._value = this.tagName !== "select" || this.children.some(option => option.value === String(value)) ? String(value) : ""; }
    appendChild(child) { this.children.push(child); child.parent = this; return child; }
    remove() {
      if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this);
      this.parent = null;
    }
    setAttribute(name, value) { this.attributes[name] = value; }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
    async emit(name, target = this) {
      const event = { target, prevented: false, preventDefault() { this.prevented = true; } };
      await Promise.all((this.listeners[name] || []).map((callback) => callback(event)));
      return event;
    }
    focus() { document.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; this.emit("close"); }
    get isConnected() { return this === document.body || Boolean(this.parent && this.parent.isConnected); }
    get elements() { return flatten(this).filter((node) => ["input", "select", "button"].includes(node.tagName)); }
  }
  function flatten(node) { return [node, ...node.children.flatMap(flatten)]; }
  document = { createElement(tag) { return new Element(tag); } };
  document.body = new Element("body");
  const trigger = document.body.appendChild(new Element("button"));
  trigger.focus();
  const window = { document };
  const context = vm.createContext({ window, document, console });
  vm.runInContext(fs.readFileSync(ENUM_FIELDS, "utf8"), context, { filename: ENUM_FIELDS });
  vm.runInContext(fs.readFileSync(SOURCE, "utf8"), context, { filename: SOURCE });
  const submissions = [];
  const closes = [];
  const options = {
    enumOptions: { domain: ["animation"], country: ["japan", "korea"], release_type: ["tv"], press_format: ["BDRip"], press_group: ["VCB"], markers: ["complete"] },
    defaults: { domain: "animation", country: "japan", release_type: "tv" },
    targetLabel: "data/2026.yaml",
    enumLabel(key, value) { return key === "country" && value === "japan" ? "日本" : value; },
    onSubmit(patch) { submissions.push(JSON.parse(JSON.stringify(patch))); },
    onClose(reason) { closes.push(reason); },
    ...extra,
  };
  const dialog = window.NimdaNewWorkDialog.open(options);
  function nodes() { return flatten(dialog); }
  function field(name) { return nodes().find((node) => node.name === name); }
  function button(text) { return nodes().find((node) => node.tagName === "button" && node.textContent === text); }
  return {
    document, dialog, trigger, submissions, closes, field, button, nodes,
    submit() { return nodes().find((node) => node.tagName === "form").emit("submit"); },
    click(text) { return button(text).emit("click"); },
    error() { return nodes().find((node) => node.className === "new-work-error"); },
    discardPanel() { return nodes().find((node) => node.className === "new-work-discard"); },
    reopen() { return window.NimdaNewWorkDialog.open(options); },
  };
}

test("opening a new-work dialog does not create a row and blank name cannot submit", async () => {
  const app = setup();
  assert.equal(app.dialog.open, true);
  assert.equal(app.document.activeElement, app.field("name"));
  assert.equal(app.submissions.length, 0);
  assert.equal(app.dialog.attributes["aria-labelledby"], app.nodes().find((node) => node.tagName === "h2").id);
  await app.submit();
  assert.equal(app.submissions.length, 0);
  assert.match(app.error().textContent, /请填写作品名/);
  assert.equal(app.field("name").attributes["aria-invalid"], "true");
});

test("a minimal work preserves enum defaults, has no blank presses, and restores trigger focus", async () => {
  const app = setup();
  app.field("name").value = "  新作品 <script>  ";
  await app.submit();
  assert.deepEqual(app.submissions, [{ name: "新作品 <script>", domain: "animation", country: "japan", release_type: "tv", date: { start: "", end: "" }, path: "", markers: [], collectioned_ordered: [] }]);
  assert.equal(app.dialog.open, false);
  assert.equal(app.document.activeElement, app.trigger);
  assert.deepEqual(app.closes, ["submit"]);
});

test("new-work enum controls use shared modes, configured labels, and compatible object choices", () => {
  const app = setup({
    enumOptions: {
      domain: [{ value: "animation" }, "animation", "tv-series"],
      country: [{ value: "japan" }, "korea"], release_type: ["tv", "movie"],
      press_format: [{ value: "BDRip" }, "1080p"], press_group: ["VCB"], markers: ["complete"],
    },
    enumLabels: { country: { japan: "日本", korea: "韩国" }, press_format: { BDRip: "蓝光压制" } },
    enumLabel: null,
  });
  for (const key of ["domain", "country", "release_type"]) assert.equal(app.field(key).tagName, "select");
  assert.deepEqual(app.field("domain").children.map(node => node.value), ["animation", "tv-series"]);
  assert.deepEqual(app.field("country").children.map(node => [node.value, node.textContent]), [["japan", "日本"], ["korea", "韩国"]]);
  const format = app.nodes().find(node => (node.name || "").startsWith("press-format-"));
  assert.equal(format.tagName, "input");
  const list = app.nodes().find(node => node.id === format.attributes.list);
  assert.equal(list.tagName, "datalist");
  assert.deepEqual(list.children.map(node => [node.value, node.label]), [["BDRip", "蓝光压制"], ["1080p", "1080p"]]);
  assert.ok(app.field("markers").attributes.list);
});

test("unknown enum defaults stay selectable and the shared form still accepts an empty press group", async () => {
  const app = setup({ defaults: { domain: "historical-domain", country: "japan", release_type: "tv" } });
  assert.equal(app.field("domain").value, "historical-domain");
  assert.equal(app.field("domain").children[0].value, "historical-domain");
  app.field("name").value = "无压制组作品";
  const format = app.nodes().find(node => (node.name || "").startsWith("press-format-"));
  const group = app.nodes().find(node => (node.name || "").startsWith("press-group-"));
  assert.ok(group.attributes.list);
  format.value = "CustomFormat";
  await app.submit();
  assert.equal(app.submissions[0].collectioned_ordered[0].press_format, "CustomFormat");
  assert.equal(app.submissions[0].collectioned_ordered[0].press_group, "");
});

test("dates are stored compactly, preserving unknown values and rejecting shape and reversed complete dates", async () => {
  const app = setup();
  app.field("name").value = "新作品";
  app.field("start").value = "2026/09/20";
  await app.submit();
  assert.match(app.error().textContent, /YYYY-MM-DD/);
  app.field("start").value = "2026-??-??";
  await app.submit();
  assert.match(app.error().textContent, /YYYY-MM-DD/);
  assert.equal(app.submissions.length, 0, "question-mark dates must not enter the pending list");
  app.field("start").value = "2026-09-20";
  app.field("end").value = "2026-09-19";
  await app.submit();
  assert.match(app.error().textContent, /不能早于/);
  app.field("start").value = "2007-00-00";
  app.field("end").value = "xxxx-xx-xx";
  await app.submit();
  assert.deepEqual(app.submissions[0].date, { start: "20070000", end: "XXXXXXXX" });
  const complete = setup();
  complete.field("name").value = "完整日期";
  complete.field("start").value = "2024-02-29";
  complete.field("end").value = "20240920";
  await complete.submit();
  assert.deepEqual(complete.submissions[0].date, { start: "20240229", end: "20240920" });
});

test("press rows support custom entries, multiple versions, optional paths, and unique markers", async () => {
  const app = setup();
  app.field("name").value = "新作品";
  app.field("path").value = " 自定义作品目录 ";
  app.field("markers").value = "complete，custom;complete";
  let pressFields = app.nodes().filter((node) => /^press-/.test(node.name || ""));
  pressFields[2].value = "_BDRip(VCB)";
  await app.submit();
  assert.match(app.error().textContent, /补充压制格式/);
  pressFields[0].value = "BDRip";
  pressFields[1].value = "VCB";
  await app.click("+ 添加压制版本");
  pressFields = app.nodes().filter((node) => /^press-/.test(node.name || ""));
  pressFields[3].value = "CustomFormat";
  pressFields[4].value = "MyGroup";
  await app.submit();
  assert.deepEqual(app.submissions[0].markers, ["complete", "custom"]);
  assert.equal(app.submissions[0].path, "自定义作品目录");
  assert.deepEqual(app.submissions[0].collectioned_ordered, [
    { press_format: "BDRip", press_group: "VCB", press_path: "_BDRip(VCB)", segment: "main" },
    { press_format: "CustomFormat", press_group: "MyGroup", press_path: "", segment: "main" },
  ]);
});

test("cancel and Escape do not submit; nonempty drafts need explicit discard", async () => {
  const empty = setup();
  await empty.click("取消");
  assert.equal(empty.submissions.length, 0);
  assert.equal(empty.dialog.open, false);
  const app = setup();
  app.field("name").value = "保留草稿";
  const event = await app.dialog.emit("cancel");
  assert.equal(event.prevented, true);
  assert.equal(app.dialog.open, true);
  assert.equal(app.discardPanel().hidden, false);
  await app.click("继续填写");
  assert.equal(app.field("name").value, "保留草稿");
  assert.equal(app.discardPanel().hidden, true);
  await app.click("取消");
  await app.click("放弃填写");
  assert.equal(app.submissions.length, 0);
  assert.deepEqual(app.closes, ["discard"]);
  assert.equal(app.document.activeElement, app.trigger);
});

test("submission exceptions retain values and enable retry, without duplicate modal instances", async () => {
  let attempts = 0;
  const app = setup({ onSubmit() { attempts++; if (attempts === 1) throw new Error("DB 已重新载入"); } });
  app.field("name").value = "保留内容";
  assert.equal(app.reopen(), app.dialog);
  assert.equal(app.document.body.children.filter((node) => node.tagName === "dialog").length, 1);
  await app.submit();
  assert.match(app.error().textContent, /DB 已重新载入/);
  assert.equal(app.field("name").value, "保留内容");
  assert.equal(app.dialog.open, true);
  assert.equal(app.button("加入待保存列表").disabled, false);
  await app.submit();
  assert.equal(attempts, 2);
  assert.equal(app.dialog.open, false);
});

test("pending async submission disables controls and ignores repeated submit and cancellation", async () => {
  let release;
  let count = 0;
  const app = setup({ onSubmit() { count++; return new Promise((resolve) => { release = resolve; }); } });
  app.field("name").value = "异步新增";
  const pending = app.submit();
  assert.equal(app.field("name").disabled, true);
  await app.submit();
  await app.dialog.emit("cancel");
  assert.equal(count, 1);
  assert.equal(app.dialog.open, true);
  release();
  await pending;
  assert.equal(app.dialog.open, false);
});

test("removing a press row preserves other fields and an untouched extra row does not make a dirty draft", async () => {
  const app = setup();
  await app.click("+ 添加压制版本");
  await app.click("移除");
  assert.equal(app.nodes().filter((node) => node.className === "new-work-press-row").length, 1);
  await app.click("取消");
  assert.equal(app.dialog.open, false);
  assert.equal(app.discardPanel().hidden, true);
});

test("country and broadcast year update the visible destination before submission", async () => {
  const app = setup({ targetLabelForDraft(draft) {
    const code = draft.country === "korea" ? "KR" : "JP";
    const year = /^\d{4}/.test(draft.start || "") && !draft.start.startsWith("0000") ? draft.start.slice(0, 4) : String(new Date().getFullYear());
    return `[${code}][TVInfo][${year}].yaml`;
  } });
  const target = () => app.nodes().find((node) => node.className === "new-work-target").textContent;
  assert.match(target(), /\[JP\]/);
  const form = app.nodes().find((node) => node.tagName === "form");
  app.field("country").value = "korea";
  app.field("start").value = "2026-09-21";
  await form.emit("change", app.field("country"));
  assert.equal(target(), "写入位置：[KR][TVInfo][2026].yaml");
  app.field("start").value = "2025-01-03";
  await form.emit("change", app.field("start"));
  assert.equal(target(), "写入位置：[KR][TVInfo][2025].yaml");
  app.field("start").value = "XXXX-XX-XX";
  await form.emit("change", app.field("start"));
  assert.equal(target(), `写入位置：[KR][TVInfo][${new Date().getFullYear()}].yaml`);
  assert.equal(app.submissions.length, 0);
});

test("copied paths remove invisible direction controls and wrapping quotes while retaining meaningful spaces", async () => {
  const app = setup();
  app.field("name").value = "路径测试";
  app.field("path").value = '\u202a"G:\\Video\\电视剧\\作品  名 A"\u202c';
  const fields = app.nodes().filter((node) => /^press-/.test(node.name || ""));
  fields[0].value = "BDRip";
  fields[1].value = "VCB";
  fields[2].value = '\u2066"作品  名 A_BDRip(VCB)\\_Disc"\u2069';
  await app.submit();
  assert.equal(app.submissions[0].path, "G:\\Video\\电视剧\\作品  名 A");
  assert.equal(app.submissions[0].collectioned_ordered[0].press_path, "作品  名 A_BDRip(VCB)\\_Disc");
  assert.doesNotMatch(JSON.stringify(app.submissions[0]), /[\u202a\u202c\u2066\u2069]/);
});

function initialWork() {
  return { name: "预填作品", domain: "historical-domain", country: "korea", release_type: "tv", date: { start: "20260921", end: "XXXXXXXX" },
    path: "U:/预填作品", markers: ["complete", "custom"], collectioned_ordered: [
      { press_format: "BDRip", press_group: "", press_path: "BD", segment: "main", external: { id: "first", tags: [1, 2] } },
      { press_format: "CustomFormat", press_group: "GroupB", press_path: "WEB", segment: "main", external: { id: "second", tags: [3] } },
    ] };
}

test("initialData prefills basic fields, display dates and ordered main presses without losing empty groups or metadata", async () => {
  const initialData = initialWork(), before = JSON.stringify(initialData), app = setup({ initialData });
  assert.equal(app.field("name").value, initialData.name); assert.equal(app.field("country").value, "korea");
  assert.equal(app.field("domain").value, "historical-domain");
  assert.equal(app.field("domain").children[0].value, "historical-domain");
  assert.equal(app.field("start").value, "2026-09-21"); assert.equal(app.field("end").value, "XXXX-XX-XX");
  assert.equal(app.field("path").value, initialData.path); assert.equal(app.field("markers").value, "complete, custom");
  const presses = app.nodes().filter(node => /^press-/.test(node.name || ""));
  assert.deepEqual(presses.map(node => node.value), ["BDRip", "", "BD", "CustomFormat", "GroupB", "WEB"]);
  await app.submit(); assert.deepEqual(app.submissions[0], initialData); assert.equal(JSON.stringify(initialData), before);
  assert.notEqual(app.submissions[0].collectioned_ordered[0].external, initialData.collectioned_ordered[0].external);
});

test("editing only a name preserves original marker separators, duplicates and whitespace with or without hints", async () => {
  for (const hideHints of [false, true]) {
    const initialData = initialWork();
    initialData.markers = ["Blu-ray, Remaster", "A;B", "重复", "重复", "换\n行", "  保留空格  "];
    const original = initialData.markers.slice(), app = setup({ initialData, hideHints });
    app.field("name").value = "只修改作品名";
    await app.submit();
    assert.equal(app.submissions[0].name, "只修改作品名");
    assert.deepEqual(app.submissions[0].markers, original);
    assert.deepEqual(initialData.markers, original);
    assert.notEqual(app.submissions[0].markers, initialData.markers);
  }
});

test("explicit marker edits keep normal separator parsing, deduplication and clearing", async () => {
  for (const hideHints of [false, true]) {
    for (const value of ["new，other;new\nthird； new", ""]) {
      const initialData = initialWork(); initialData.markers = ["A,B", "A,B", "C;D"];
      const app = setup({ initialData, hideHints }); app.field("markers").value = value;
      await app.submit();
      assert.deepEqual(app.submissions[0].markers, value ? ["new", "other", "third"] : []);
      assert.deepEqual(initialData.markers, ["A,B", "A,B", "C;D"]);
    }
  }
});

test("restoring initial marker text and retrying a rejected callback preserve the original marker snapshot", async () => {
  const initialData = initialWork(); initialData.markers = ["A,B", "C;D", "C;D"];
  const attempts = [], app = setup({ initialData, hideHints: true, onSubmit(patch) {
    attempts.push(JSON.parse(JSON.stringify(patch.markers)));
    if (attempts.length === 1) { patch.markers.pop(); throw Error("草稿已变化"); }
  } });
  const initialText = app.field("markers").value;
  app.field("markers").value = "temporary";
  app.field("markers").value = initialText;
  initialData.markers.push("external mutation after opening");
  await app.submit(); assert.equal(app.dialog.open, true);
  await app.submit();
  assert.deepEqual(attempts, [["A,B", "C;D", "C;D"], ["A,B", "C;D", "C;D"]]);
});

test("removing and adding prefilled press rows keeps extension metadata attached to its own row", async () => {
  const initialData = initialWork(), app = setup({ initialData });
  await app.click("移除");
  let pressFields = app.nodes().filter(node => /^press-/.test(node.name || "")); pressFields[0].value = "EditedSecond"; pressFields[1].value = "";
  await app.click("+ 添加压制版本");
  pressFields = app.nodes().filter(node => /^press-/.test(node.name || "")); pressFields[3].value = "AddedThird"; pressFields[5].value = "Third";
  await app.submit();
  assert.deepEqual(app.submissions[0].collectioned_ordered, [
    { press_format: "EditedSecond", press_group: "", press_path: "WEB", segment: "main", external: { id: "second", tags: [3] } },
    { press_format: "AddedThird", press_group: "", press_path: "Third", segment: "main" },
  ]);
  assert.equal(initialData.collectioned_ordered[0].external.id, "first"); assert.equal(initialData.collectioned_ordered[1].press_format, "CustomFormat");
});

test("explicit empty initial enum values stay empty rather than inheriting new-work defaults", async () => {
  const initialData = { ...initialWork(), country: "", domain: "", release_type: "" }, app = setup({ initialData });
  for (const key of ["country", "domain", "release_type"]) { assert.equal(app.field(key).value, ""); assert.equal(app.field(key).children[0].value, ""); }
  await app.submit(); assert.equal(app.submissions.length, 0); assert.match(app.error().textContent, /请先选择/);
  app.field("country").value = "japan"; app.field("domain").value = "animation"; app.field("release_type").value = "tv";
  await app.submit(); assert.equal(app.submissions[0].country, "japan"); assert.equal(initialData.country, "");
});

test("custom dialog labels and hideHints simplify draft editing without hiding errors or discard confirmation", async () => {
  const app = setup({ initialData: initialWork(), title: "编辑整理草稿", description: "仅更新未保存草稿", submitLabel: "应用到整理草稿", targetLabel: "目录整理", hideHints: true });
  assert.equal(app.nodes().find(node => node.tagName === "h2").textContent, "编辑整理草稿");
  assert.ok(app.button("应用到整理草稿")); assert.equal(app.dialog.attributes["aria-describedby"], undefined);
  assert.ok(app.nodes().filter(node => node.className === "new-work-hint" || node.className === "new-work-target").every(node => node.hidden));
  app.field("name").value = ""; await app.submit(); assert.equal(app.error().hidden, false); assert.match(app.error().textContent, /请填写作品名/);
  await app.dialog.emit("cancel"); assert.equal(app.discardPanel().hidden, false); assert.equal(app.dialog.open, true);
  await app.click("继续填写"); assert.equal(app.field("name").value, "");
  await app.click("取消"); await app.click("放弃填写"); assert.equal(app.submissions.length, 0); assert.deepEqual(app.closes, ["discard"]);
  const visible = setup({ initialData: initialWork(), title: "自定义标题", description: "简短说明", targetLabel: "草稿目标" });
  assert.ok(visible.nodes().some(node => node.textContent === "简短说明" && !node.hidden));
  assert.ok(visible.nodes().some(node => node.textContent === "写入位置：草稿目标"));
});

test("cancelling an unchanged prefilled draft needs no discard confirmation and never submits", async () => {
  const app = setup({ initialData: initialWork() });
  await app.dialog.emit("cancel"); assert.equal(app.dialog.open, false); assert.equal(app.discardPanel().hidden, true);
  assert.equal(app.submissions.length, 0); assert.deepEqual(app.closes, ["cancel"]); assert.equal(app.document.activeElement, app.trigger);
});

test("a retry retains the custom submit label and snapshots metadata independently from rejected callbacks", async () => {
  const initialData = initialWork(), attempts = [], app = setup({ initialData, submitLabel: "应用草稿", onSubmit(patch) {
    attempts.push(JSON.parse(JSON.stringify(patch)));
    if (attempts.length === 1) { patch.collectioned_ordered[0].external.id = "mutated-by-callback"; throw Error("草稿版本已变化"); }
  } });
  await app.submit(); assert.equal(app.dialog.open, true); assert.equal(app.button("应用草稿").disabled, false); assert.match(app.error().textContent, /草稿版本已变化/);
  await app.submit(); assert.deepEqual(attempts[0], attempts[1]); assert.equal(initialData.collectioned_ordered[0].external.id, "first");
});

test("prefilled press rows without a segment default to main and continuation input is rejected explicitly", async () => {
  const initialData = initialWork(); delete initialData.collectioned_ordered[0].segment;
  const app = setup({ initialData }); await app.submit(); assert.equal(app.submissions[0].collectioned_ordered[0].segment, "main");
  const unsupported = initialWork(); unsupported.collectioned_ordered[1].segment = "continuation";
  assert.throws(() => setup({ initialData: unsupported }), /仅编辑主收集.*补充收集/);
  assert.throws(() => setup({ initialData: { ...initialWork(), collectioned_ordered: [null] } }), /压制初始数据必须是对象/);
});
