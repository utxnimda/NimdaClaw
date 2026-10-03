(function () {
  "use strict";

  var active = null;
  var nextId = 0;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function values(options, key) {
    return (Array.isArray(options[key]) ? options[key] : []).map(function (value) {
      return String(value == null ? "" : value).trim();
    }).filter(function (value, index, all) { return value && all.indexOf(value) === index; });
  }

  function compactDate(value, label) {
    var raw = value.trim().toUpperCase();
    if (!raw) return "";
    if (!/^(?:[0-9X]{8}|[0-9X]{4}-[0-9X]{2}-[0-9X]{2})$/.test(raw)) {
      throw new Error(label + "请填写 YYYY-MM-DD，可留空；未知部分可使用 00 或 X。");
    }
    return raw.replace(/-/g, "");
  }

  function isCompleteDate(value) {
    return /^[0-9]{8}$/.test(value) && value.slice(0, 4) !== "0000" &&
      value.slice(4, 6) !== "00" && value.slice(6, 8) !== "00";
  }

  function cleanCopiedPath(value) {
    var raw = String(value || "");
    for (;;) {
      var clean = raw.trim().replace(/^[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]+|[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]+$/g, "").trim();
      if (clean.length >= 2 && clean[0] === '"' && clean[clean.length - 1] === '"') clean = clean.slice(1, -1);
      if (clean === raw) return clean;
      raw = clean;
    }
  }

  function open(options) {
    if (active) {
      active.name.focus();
      return active.dialog;
    }
    options = options || {};
    if (typeof options.onSubmit !== "function") throw new Error("新增作品缺少提交处理函数。");
    var enums = options.enumOptions || {};
    var defaults = options.defaults || {};
    var prefix = "new-work-" + (++nextId) + "-";
    var trigger = document.activeElement;
    var dialog = el("dialog", "new-work-dialog");
    var form = el("form", "new-work-form");
    form.noValidate = true;
    var header = el("header", "new-work-header");
    var heading = el("h2", "", "新增作品");
    heading.id = prefix + "title";
    dialog.setAttribute("aria-labelledby", heading.id);
    dialog.setAttribute("aria-describedby", prefix + "description");
    var description = el("p", "new-work-hint", "填写后加入待保存列表，再点击「保存到 YAML」写入数据库。取消不会新增空白行。");
    description.id = prefix + "description";
    header.appendChild(heading);
    header.appendChild(description);
    var targetLabel = el("p", "new-work-target", "写入位置：" + (options.targetLabel || "按国家和开播年份归档"));
    header.appendChild(targetLabel);
    form.appendChild(header);
    var body = el("div", "new-work-body");
    form.appendChild(body);
    var fields = {};
    var pressRows = [];
    var busy = false;
    var closed = false;
    var closeReason = "cancel";

    function field(parent, key, title, opts) {
      opts = opts || {};
      var label = el("label", "new-work-field" + (opts.wide ? " new-work-field-wide" : ""));
      var caption = el("span", "", title);
      var input = el(opts.select ? "select" : "input");
      input.id = prefix + key;
      input.name = key;
      label.htmlFor = input.id;
      if (!opts.select) input.type = "text";
      input.autocomplete = "off";
      if (opts.placeholder) input.placeholder = opts.placeholder;
      if (opts.required) {
        input.required = true;
        input.setAttribute("aria-required", "true");
      }
      if (opts.select) {
        var choices = values(enums, key);
        if (defaults[key] && choices.indexOf(defaults[key]) === -1) choices.unshift(defaults[key]);
        choices.forEach(function (value) {
          var choice = el("option", "", options.enumLabel ? options.enumLabel(key, value) || value : value);
          choice.value = value;
          input.appendChild(choice);
        });
        if (!choices.length) {
          var empty = el("option", "", "暂无选项，请先配置枚举");
          empty.value = "";
          input.appendChild(empty);
        }
        input.value = defaults[key] || choices[0] || "";
      } else if (opts.list) {
        input.setAttribute("list", prefix + opts.list + "-options");
      }
      label.appendChild(caption);
      label.appendChild(input);
      if (opts.hint) label.appendChild(el("small", "new-work-hint", opts.hint));
      parent.appendChild(label);
      return input;
    }

    function section(title, optional) {
      var area = el("section", "new-work-section");
      var sectionHeading = el("h3", "", title);
      if (optional) sectionHeading.appendChild(el("span", "new-work-optional", "选填"));
      area.appendChild(sectionHeading);
      body.appendChild(area);
      return area;
    }

    var basics = section("基本信息");
    var basicGrid = el("div", "new-work-grid");
    basics.appendChild(basicGrid);
    fields.name = field(basicGrid, "name", "作品名 *", { required: true, wide: true, placeholder: "填写作品名称" });
    fields.domain = field(basicGrid, "domain", "品类 *", { select: true, required: true });
    fields.country = field(basicGrid, "country", "国家 / 地区 *", { select: true, required: true });
    fields.release_type = field(basicGrid, "release_type", "播出类型 *", { select: true, required: true });
    fields.start = field(basicGrid, "start", "开播日期", { placeholder: "YYYY-MM-DD" });
    fields.end = field(basicGrid, "end", "完结日期", { placeholder: "YYYY-MM-DD" });
    basics.appendChild(el("p", "new-work-hint", "日期可留空；未知日期可填 2007-00-00 或 XXXX-XX-XX。存储时自动转换为八位格式。"));

    var collection = section("收集信息", true);
    fields.path = field(collection, "path", "作品根目录", {
      placeholder: "U:\\作品名", hint: "可填写完整路径（如 U:\\作品名）或已有相对目录；尚未收集可留空。"
    });
    collection.appendChild(el("p", "new-work-hint", "压制信息支持选择现有值或直接输入，可添加多个版本。留空的压制行不会保存。"));
    ["press_format", "press_group", "markers"].forEach(function (key) {
      var list = el("datalist");
      list.id = prefix + key + "-options";
      values(enums, key).forEach(function (value) {
        var choice = el("option");
        choice.value = value;
        if (options.enumLabel) choice.label = options.enumLabel(key, value) || value;
        list.appendChild(choice);
      });
      collection.appendChild(list);
    });
    var presses = el("div", "new-work-presses");
    collection.appendChild(presses);
    function addPressRow() {
      var row = el("div", "new-work-press-row");
      var number = ++nextId;
      var rowFields = {
        element: row,
        format: field(row, "press-format-" + number, "压制格式", { list: "press_format", placeholder: "BDRip / 1080p" }),
        group: field(row, "press-group-" + number, "压制 / 字幕组", { list: "press_group", placeholder: "可选择或手填" }),
        path: field(row, "press-path-" + number, "压制目录", { placeholder: "相对于作品根目录，可留空" }),
      };
      var remove = el("button", "new-work-remove", "移除");
      remove.type = "button";
      remove.setAttribute("aria-label", "移除压制信息第 " + (pressRows.length + 1) + " 行");
      remove.addEventListener("click", function () {
        if (busy) return;
        pressRows.splice(pressRows.indexOf(rowFields), 1);
        row.remove();
        addPress.focus();
      });
      row.appendChild(remove);
      presses.appendChild(row);
      pressRows.push(rowFields);
      return rowFields.format;
    }
    var addPress = el("button", "new-work-add-press", "+ 添加压制版本");
    addPress.type = "button";
    addPress.addEventListener("click", function () { if (!busy) addPressRow().focus(); });
    collection.appendChild(addPress);
    addPressRow();
    fields.markers = field(collection, "markers", "标记", {
      list: "markers", placeholder: "多个标记以逗号分隔", hint: "可从已有标记选择，也可以输入新标记。"
    });

    var footer = el("footer", "new-work-footer");
    var error = el("p", "new-work-error");
    error.setAttribute("role", "alert");
    error.hidden = true;
    footer.appendChild(error);
    var discardPanel = el("div", "new-work-discard");
    discardPanel.hidden = true;
    discardPanel.setAttribute("role", "alert");
    discardPanel.appendChild(el("span", "", "已填写内容尚未加入列表，确定放弃？"));
    var keep = el("button", "", "继续填写");
    keep.type = "button";
    var discard = el("button", "new-work-discard-confirm", "放弃填写");
    discard.type = "button";
    discardPanel.appendChild(keep);
    discardPanel.appendChild(discard);
    footer.appendChild(discardPanel);
    var actions = el("div", "new-work-actions");
    var cancel = el("button", "", "取消");
    cancel.type = "button";
    var submit = el("button", "new-work-submit", "加入待保存列表");
    submit.type = "submit";
    actions.appendChild(cancel);
    actions.appendChild(submit);
    footer.appendChild(actions);
    form.appendChild(footer);
    dialog.appendChild(form);

    function draft() {
      var output = {};
      Object.keys(fields).forEach(function (key) { output[key] = fields[key].value.trim(); });
      output.presses = pressRows.map(function (row) {
        return [row.format.value.trim(), row.group.value.trim(), row.path.value.trim()];
      }).filter(function (row) { return row.some(Boolean); });
      return output;
    }
    var initial = JSON.stringify(draft());
    function refreshTargetLabel() {
      if (typeof options.targetLabelForDraft !== "function") return;
      try { targetLabel.textContent = "写入位置：" + options.targetLabelForDraft(draft()); }
      catch (exception) { targetLabel.textContent = exception.message; }
    }
    refreshTargetLabel();
    form.addEventListener("change", refreshTargetLabel);
    function finishClose() {
      if (closed) return;
      closed = true;
      dialog.remove();
      active = null;
      if (trigger && trigger.isConnected !== false && typeof trigger.focus === "function") trigger.focus();
      if (typeof options.onClose === "function") options.onClose(closeReason);
    }
    function close(reason) {
      closeReason = reason;
      dialog.close();
      finishClose();
    }
    function cancelDraft() {
      if (busy) return;
      if (JSON.stringify(draft()) === initial) close("cancel");
      else {
        discardPanel.hidden = false;
        keep.focus();
      }
    }
    cancel.addEventListener("click", cancelDraft);
    discard.addEventListener("click", function () { if (!busy) close("discard"); });
    keep.addEventListener("click", function () { discardPanel.hidden = true; fields.name.focus(); });
    dialog.addEventListener("cancel", function (event) { event.preventDefault(); cancelDraft(); });
    dialog.addEventListener("close", finishClose);

    function reject(message, input) {
      error.textContent = message;
      error.hidden = false;
      if (input) {
        input.setAttribute("aria-invalid", "true");
        input.focus();
      }
      return false;
    }
    form.addEventListener("input", function (event) {
      if (event.target && event.target.removeAttribute) event.target.removeAttribute("aria-invalid");
      error.hidden = true;
      discardPanel.hidden = true;
      refreshTargetLabel();
    });
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (busy) return;
      error.hidden = true;
      discardPanel.hidden = true;
      var data = draft();
      if (!data.name) return reject("请填写作品名后再加入列表。", fields.name);
      for (var key of ["domain", "country", "release_type"]) {
        if (!data[key]) return reject("请先选择品类、国家 / 地区和播出类型。", fields[key]);
      }
      var dates = {};
      for (var dateKey of ["start", "end"]) {
        try { dates[dateKey] = compactDate(data[dateKey], dateKey === "start" ? "开播日期" : "完结日期"); }
        catch (exception) { return reject(exception.message, fields[dateKey]); }
      }
      if (isCompleteDate(dates.start) && isCompleteDate(dates.end) && dates.end < dates.start) {
        return reject("完结日期不能早于开播日期。", fields.end);
      }
      var ordered = [];
      for (var row of pressRows) {
        var format = row.format.value.trim();
        var group = row.group.value.trim();
        var path = cleanCopiedPath(row.path.value);
        if (!format && !group && !path) continue;
        if (!format) return reject("填写压制信息时，请补充压制格式；未收集时可移除该行。", row.format);
        ordered.push({ press_format: format, press_group: group, press_path: path, segment: "main" });
      }
      var markers = data.markers.split(/[,，;；\n]+/).map(function (value) { return value.trim(); })
        .filter(function (value, index, all) { return value && all.indexOf(value) === index; });
      busy = true;
      Array.prototype.forEach.call(form.elements, function (input) { input.disabled = true; });
      submit.textContent = "正在加入…";
      try {
        await options.onSubmit({ name: data.name, domain: data.domain, country: data.country,
          release_type: data.release_type, date: dates, path: cleanCopiedPath(data.path),
          markers: markers, collectioned_ordered: ordered });
        close("submit");
      } catch (exception) {
        reject(exception && exception.message ? exception.message : "加入失败，填写内容已保留，请重试。");
      } finally {
        busy = false;
        if (!closed) {
          Array.prototype.forEach.call(form.elements, function (input) { input.disabled = false; });
          submit.textContent = "加入待保存列表";
        }
      }
    });
    document.body.appendChild(dialog);
    active = { dialog: dialog, name: fields.name };
    try {
      dialog.showModal();
      fields.name.focus();
    } catch (exception) {
      dialog.remove();
      active = null;
      throw exception;
    }
    return dialog;
  }

  window.NimdaNewWorkDialog = { open: open };
})();
