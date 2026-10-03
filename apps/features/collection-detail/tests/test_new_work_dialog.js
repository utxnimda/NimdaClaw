"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const SOURCE = path.resolve(__dirname, "../frontend/new-work-dialog.js");

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
  vm.runInNewContext(fs.readFileSync(SOURCE, "utf8"), { window, document, console }, { filename: SOURCE });
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
