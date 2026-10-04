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
  function displayDate(value) {
    return String(value == null ? "" : value).replace(/^([0-9Xx]{4})([0-9Xx]{2})([0-9Xx]{2})$/, "$1-$2-$3");
  }
  function copy(value) { return JSON.parse(JSON.stringify(value)); }

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
    var initialData = options.initialData || {};
    var initialPresses = Array.isArray(initialData.collectioned_ordered) ? initialData.collectioned_ordered : [];
    initialPresses.forEach(function (press) {
      if (!press || typeof press !== "object" || Array.isArray(press)) throw new Error("压制初始数据必须是对象。");
      if (press.segment && press.segment !== "main") throw new Error("此表单仅编辑主收集；补充收集请使用高级编辑。");
    });
    var submitLabel = options.submitLabel == null ? "加入待保存列表" : String(options.submitLabel);
    var enumFields = window.NimdaEnumFields;
    function enumChoices(key, current) {
      return enumFields.choices(enums, options.enumLabels || {}, key, current).map(function (choice) {
        return { value: choice.value, label: options.enumLabel ? options.enumLabel(key, choice.value) || choice.label : choice.label };
      });
    }
    var prefix = "new-work-" + (++nextId) + "-";
    var trigger = document.activeElement;
    var dialog = el("dialog", "new-work-dialog");
    var form = el("form", "new-work-form");
    form.noValidate = true;
    var header = el("header", "new-work-header");
    var heading = el("h2", "", options.title == null ? "新增作品" : String(options.title));
    heading.id = prefix + "title";
    dialog.setAttribute("aria-labelledby", heading.id);
    var description = el("p", "new-work-hint", options.description == null ? "填写后加入待保存列表，再点击「保存到 YAML」写入数据库。取消不会新增空白行。" : String(options.description));
    description.id = prefix + "description";
    description.hidden = !!options.hideHints || !description.textContent;
    if (!description.hidden) dialog.setAttribute("aria-describedby", description.id);
    header.appendChild(heading);
    header.appendChild(description);
    var targetLabel = el("p", "new-work-target", "写入位置：" + (options.targetLabel || "按国家和开播年份归档"));
    targetLabel.hidden = !!options.hideHints;
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
      var enumKey = opts.enumKey || key;
      var mode = enumFields.fieldMode(enumKey);
      var label = el("label", "new-work-field" + (opts.wide ? " new-work-field-wide" : ""));
      var caption = el("span", "", title);
      var input = el(mode === "select" ? "select" : "input");
      input.id = prefix + key;
      input.name = key;
      label.htmlFor = input.id;
      if (mode !== "select") input.type = "text";
      input.autocomplete = "off";
      if (opts.placeholder) input.placeholder = opts.placeholder;
      if (opts.required) {
        input.required = true;
        input.setAttribute("aria-required", "true");
      }
      if (mode === "select") {
        var hasInitial = Object.prototype.hasOwnProperty.call(initialData, key);
        var initialValue = hasInitial ? String(initialData[key] == null ? "" : initialData[key]) : defaults[key];
        var choices = enumChoices(enumKey, initialValue);
        if (hasInitial && !initialValue) {
          var placeholder = el("option", "", "请选择"); placeholder.value = ""; input.appendChild(placeholder);
        }
        choices.forEach(function (entry) {
          var choice = el("option", "", entry.label);
          choice.value = entry.value;
          input.appendChild(choice);
        });
        if (!choices.length && !(hasInitial && !initialValue)) {
          var empty = el("option", "", "暂无选项，请先配置枚举");
          empty.value = "";
          input.appendChild(empty);
        }
        input.value = hasInitial ? initialValue : defaults[key] || (choices[0] && choices[0].value) || "";
      } else if (mode === "datalist") {
        input.setAttribute("list", prefix + enumKey + "-options");
      }
      label.appendChild(caption);
      label.appendChild(input);
      if (opts.hint && !options.hideHints) label.appendChild(el("small", "new-work-hint", opts.hint));
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
    fields.domain = field(basicGrid, "domain", "品类 *", { required: true });
    fields.country = field(basicGrid, "country", "国家 / 地区 *", { required: true });
    fields.release_type = field(basicGrid, "release_type", "播出类型 *", { required: true });
    fields.start = field(basicGrid, "start", "开播日期", { placeholder: "YYYY-MM-DD" });
    fields.end = field(basicGrid, "end", "完结日期", { placeholder: "YYYY-MM-DD" });
    if (!options.hideHints) basics.appendChild(el("p", "new-work-hint", "日期可留空；未知日期可填 2007-00-00 或 XXXX-XX-XX。存储时自动转换为八位格式。"));

    var collection = section("收集信息", true);
    fields.path = field(collection, "path", "作品根目录", {
      placeholder: "U:\\作品名", hint: "可填写完整路径（如 U:\\作品名）或已有相对目录；尚未收集可留空。"
    });
    if (!options.hideHints) collection.appendChild(el("p", "new-work-hint", "压制信息支持选择现有值或直接输入，可添加多个版本。留空的压制行不会保存。"));
    ["press_format", "press_group", "markers"].forEach(function (key) {
      var list = el("datalist");
      list.id = prefix + key + "-options";
      enumChoices(key).forEach(function (entry) {
        var choice = el("option");
        choice.value = entry.value;
        choice.label = entry.label;
        list.appendChild(choice);
      });
      collection.appendChild(list);
    });
    var presses = el("div", "new-work-presses");
    collection.appendChild(presses);
    function addPressRow(initialPress) {
      var row = el("div", "new-work-press-row");
      var number = ++nextId;
      var rowFields = {
        element: row,
        original: copy(initialPress || {}),
        format: field(row, "press-format-" + number, "压制格式", { enumKey: "press_format", placeholder: "BDRip / 1080p" }),
        group: field(row, "press-group-" + number, "压制 / 字幕组", { enumKey: "press_group", placeholder: "可选择或手填" }),
        path: field(row, "press-path-" + number, "压制目录", { placeholder: "相对于作品根目录，可留空" }),
      };
      if (initialPress) {
        rowFields.format.value = String(initialPress.press_format == null ? "" : initialPress.press_format);
        rowFields.group.value = String(initialPress.press_group == null ? "" : initialPress.press_group);
        rowFields.path.value = String(initialPress.press_path == null ? "" : initialPress.press_path);
      }
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
    if (initialPresses.length) initialPresses.forEach(addPressRow); else addPressRow();
    fields.markers = field(collection, "markers", "标记", {
      placeholder: "多个标记以逗号分隔", hint: "可从已有标记选择，也可以输入新标记。"
    });
    ["name", "path"].forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(initialData, key)) fields[key].value = String(initialData[key] == null ? "" : initialData[key]);
    });
    var initialDates = initialData.date || {};
    fields.start.value = displayDate(initialDates.start); fields.end.value = displayDate(initialDates.end);
    if (Object.prototype.hasOwnProperty.call(initialData, "markers")) {
      fields.markers.value = Array.isArray(initialData.markers) ? initialData.markers.join(", ") : String(initialData.markers == null ? "" : initialData.markers);
    }
    // Existing marker names may contain the form's separators or duplicates.
    // Keep their exact values unless this field's displayed text is changed.
    var originalMarkers = Array.isArray(initialData.markers) ? copy(initialData.markers) : null;
    var initialMarkerText = fields.markers.value;

    var footer = el("footer", "new-work-footer");
    var error = el("p", "new-work-error");
    error.setAttribute("role", "alert");
    error.hidden = true;
    footer.appendChild(error);
    var discardPanel = el("div", "new-work-discard");
    discardPanel.hidden = true;
    discardPanel.setAttribute("role", "alert");
    discardPanel.appendChild(el("span", "", options.initialData ? "修改尚未提交，确定放弃？" : "已填写内容尚未加入列表，确定放弃？"));
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
    var submit = el("button", "new-work-submit", submitLabel);
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
      if (options.hideHints) return;
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
      if (!data.name) return reject(options.initialData ? "请填写作品名。" : "请填写作品名后再加入列表。", fields.name);
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
        ordered.push(Object.assign(copy(row.original), { press_format: format, press_group: group, press_path: path, segment: "main" }));
      }
      var markers = originalMarkers !== null && fields.markers.value === initialMarkerText ? copy(originalMarkers) :
        data.markers.split(/[,，;；\n]+/).map(function (value) { return value.trim(); })
          .filter(function (value, index, all) { return value && all.indexOf(value) === index; });
      busy = true;
      Array.prototype.forEach.call(form.elements, function (input) { input.disabled = true; });
      submit.textContent = options.submitLabel == null ? "正在加入…" : "正在提交…";
      try {
        await options.onSubmit({ name: data.name, domain: data.domain, country: data.country,
          release_type: data.release_type, date: dates, path: cleanCopiedPath(data.path),
          markers: markers, collectioned_ordered: ordered });
        close("submit");
      } catch (exception) {
        reject(exception && exception.message ? exception.message : options.submitLabel == null ? "加入失败，填写内容已保留，请重试。" : "提交失败，填写内容已保留，请重试。");
      } finally {
        busy = false;
        if (!closed) {
          Array.prototype.forEach.call(form.elements, function (input) { input.disabled = false; });
          submit.textContent = submitLabel;
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
