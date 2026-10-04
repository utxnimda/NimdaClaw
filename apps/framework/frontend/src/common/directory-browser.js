(function (global) {
  "use strict";

  const common = global.NimdaCommon = global.NimdaCommon || {};
  const statuses = { unchanged: "未变化", moved: "移动", mixed: "部分变化", contents_changed: "内容变化", added: "新增", retained: "保留" };
  const canonical = value => String(value || "").replace(/\\/g, "/").replace(/\/{2,}/g, "/").replace(/\/$/, "").toLowerCase();
  const bounded = (value, fallback, min, max) => Number.isFinite(Number(value)) ? Math.max(min, Math.min(max, Math.floor(Number(value)))) : fallback;
  const count = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;

  /**
   * Read-only, feature-neutral directory navigation. State belongs to the caller
   * and can be reused across mounts; no feature render or filesystem API is used.
   * mount(host, { roots, state, title?, pageSize?, branchPageSize?, maxTreeNodes? })
   * returns { update(options), destroy() }.
   */
  function mount(host, initial) {
    if (!host || typeof host.appendChild !== "function") throw new TypeError("DirectoryBrowser requires a host element");
    const document = host.ownerDocument || global.document || (typeof globalThis.document !== "undefined" ? globalThis.document : null);
    if (!document || !document.createElement) throw new TypeError("DirectoryBrowser requires a document");
    let options = Object.assign({}, initial || {}), state = options.state || {}, destroyed = false;
    let entries = new Map(), roots = [], expanded = new Set(), controls = new Map();
    let treeScroller = null, contentScroller = null, section = null, tableHeading = null;
    let sampleTreeRow = null, sampleFileRow = null, observer = null;
    let treeCapacity = bounded(state.treePageCapacity, 10, 1, 2000), contentCapacity = bounded(state.contentPageSize, 10, 1, 200), measuring = false;
    const view = document.defaultView || global;

    function element(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = String(text);
      return node;
    }
    function append(parent, node) { parent.appendChild(node); return node; }
    function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
    function button(text, action, key, className) {
      const node = element("button", className || "nimda-directory-tool", text);
      node.type = "button";
      node.addEventListener("click", event => { event.preventDefault(); if (!destroyed && !node.disabled) action(); });
      if (key) { controls.set(key, node); node.setAttribute("data-directory-control", key); }
      return node;
    }
    function syncExpanded() { state.expandedPaths = [...expanded].map(key => entries.get(key)).filter(Boolean).map(entry => entry.node.path); }
    function index() {
      entries = new Map(); roots = [];
      const pending = [];
      for (const node of Array.isArray(options.roots) ? options.roots : []) if (node && node.kind === "directory") pending.push({ node, parent: null });
      for (let cursor = 0; cursor < pending.length; cursor++) {
        const value = pending[cursor], node = value.node, key = canonical(node.path);
        if (entries.has(key)) continue;
        const entry = { node, key, parent: value.parent, directories: [], children: Array.isArray(node.children) ? node.children : [] };
        entries.set(key, entry);
        if (value.parent) value.parent.directories.push(entry); else roots.push(entry);
        for (const child of entry.children) if (child && child.kind === "directory") pending.push({ node: child, parent: entry });
      }
      const supplied = state.expandedPaths;
      const paths = Array.isArray(supplied) ? supplied : supplied && typeof supplied.values === "function" ? [...supplied.values()] : null;
      expanded = new Set((paths === null ? roots.map(entry => entry.node.path) : paths).map(canonical).filter(key => entries.has(key)));
      syncExpanded();
      let selected = entries.get(canonical(state.selectedPath));
      if (!selected) selected = roots[0];
      state.selectedPath = selected ? selected.node.path : "";
      state.page = bounded(state.page, 0, 0, Number.MAX_SAFE_INTEGER);
      state.treePage = bounded(state.treePage, 0, 0, Number.MAX_SAFE_INTEGER);
    }
    function redraw(focusKey) { render(false, focusKey); }
    function select(entry, focusKey) {
      if (canonical(state.selectedPath) !== entry.key) {
        state.selectedPath = entry.node.path; state.page = 0;
      }
      redraw(focusKey);
    }
    function toggle(entry) {
      if (expanded.has(entry.key)) expanded.delete(entry.key); else expanded.add(entry.key);
      state.treePageStart = entry.node.path;
      syncExpanded(); redraw("toggle:" + entry.key);
    }
    function folderIcon(open) {
      const icon = element("span", "nimda-directory-icon is-folder" + (open ? " is-open" : ""));
      icon.setAttribute("aria-hidden", "true"); return icon;
    }
    function treePages(capacity) {
      const visible = [], positions = new Map(), pending = roots.slice().reverse().map(entry => ({ entry, depth: 0, root: entry }));
      while (pending.length) {
        const item = pending.pop(); positions.set(item.entry.key, visible.length); visible.push(item);
        if (expanded.has(item.entry.key)) for (let index = item.entry.directories.length - 1; index >= 0; index--) pending.push({ entry: item.entry.directories[index], depth: item.depth + 1, root: item.root });
      }
      const pages = [];
      for (let start = 0; start < visible.length;) {
        const first = visible[start], room = capacity - 1, context = [];
        if (first.depth && room) {
          if (first.depth <= room) {
            let parent = first.entry.parent;
            while (parent) { context.unshift({ entry: parent, context: true }); parent = parent.parent; }
          } else {
            // A very deep path cannot consume the entire page. Keep the root
            // and immediate parent, with a literal full-path breadcrumb/title.
            if (room > 1 && first.entry.parent !== first.root) context.push({ entry: first.root, context: true });
            context.push({ entry: first.entry.parent, context: true, compressed: true });
          }
        }
        const end = Math.min(visible.length, start + capacity - context.length);
        pages.push({ start, end, context }); start = end;
      }
      if (!pages.length) pages.push({ start: 0, end: 0, context: [] });
      const anchor = positions.get(canonical(state.treePageStart));
      if (anchor !== undefined) {
        let low = 0, high = pages.length - 1;
        while (low < high) { const middle = Math.floor((low + high + 1) / 2); if (pages[middle].start <= anchor) low = middle; else high = middle - 1; }
        state.treePage = low;
      }
      state.treePage = Math.min(state.treePage, pages.length - 1);
      const page = pages[state.treePage], first = visible[page.start];
      state.treePageStart = first ? first.entry.node.path : "";
      return { visible, pages, page, first };
    }
    function tree() {
      const aside = element("aside", "nimda-directory-tree"); aside.setAttribute("aria-label", "目录结构");
      const toolbar = append(aside, element("div", "nimda-directory-tree-toolbar"));
      append(toolbar, element("strong", "", "目录结构"));
      const actions = append(toolbar, element("div", "nimda-directory-actions"));
      append(actions, button("展开全部", () => { expanded = new Set(entries.keys()); syncExpanded(); redraw("expand-all"); }, "expand-all"));
      append(actions, button("折叠全部", () => { expanded.clear(); state.treePage = 0; state.treePageStart = ""; syncExpanded(); redraw("collapse-all"); }, "collapse-all"));
      const capacity = Math.min(treeCapacity, bounded(options.branchPageSize, 60, 1, 200), bounded(options.maxTreeNodes, 400, 1, 2000));
      state.treePageCapacity = capacity;
      const model = treePages(capacity), header = append(aside, element("div", "nimda-directory-tree-header"));
      const contextPath = model.first && model.first.entry.parent ? model.first.entry.parent.node.path : "";
      const breadcrumb = append(header, element("span", "nimda-directory-tree-context", contextPath ? "父级：" + contextPath : "名称")); breadcrumb.title = contextPath || "目录名称";
      append(header, element("span", "", "文件数"));
      treeScroller = append(aside, element("div", "nimda-directory-tree-scroll"));
      treeScroller.setAttribute("role", "tree"); treeScroller.setAttribute("aria-label", "仅目录树");
      treeScroller.setAttribute("data-directory-page-capacity", String(capacity));
      const shown = new Map(), rows = model.page.context.concat(model.visible.slice(model.page.start, model.page.end));
      for (const item of rows) {
        const entry = item.entry;
        let parent = entry.parent;
        while (parent && !shown.has(parent.key)) parent = parent.parent;
        const parentView = parent && shown.get(parent.key), depth = parentView ? parentView.depth + 1 : 0;
        const wrapper = append(parentView ? parentView.group : treeScroller, element("div", "nimda-directory-tree-branch"));
        wrapper.setAttribute("role", "treeitem"); wrapper.setAttribute("aria-level", String(depth + 1));
        const row = append(wrapper, element("div", "nimda-directory-tree-row" + (entry.key === canonical(state.selectedPath) ? " is-selected" : "")));
        if (!sampleTreeRow) sampleTreeRow = row;
        wrapper.setAttribute("aria-selected", String(entry.key === canonical(state.selectedPath)));
        row.setAttribute("data-directory-path", entry.node.path);
        if (item.context) row.setAttribute("data-directory-context", "true");
        row.style.paddingLeft = Math.min(depth, 4) * 14 + 6 + "px";
        const isOpen = expanded.has(entry.key), canToggle = entry.directories.length > 0 || !entry.parent;
        if (canToggle) {
          wrapper.setAttribute("aria-expanded", String(isOpen));
          const control = append(row, button(isOpen ? "−" : "+", () => toggle(entry), "toggle:" + entry.key, "nimda-directory-toggle"));
          control.setAttribute("aria-label", (isOpen ? "折叠 " : "展开 ") + entry.node.name);
          control.setAttribute("aria-expanded", String(isOpen));
        } else append(row, element("span", "nimda-directory-toggle-spacer"));
        const choose = append(row, button("", () => select(entry, "select:" + entry.key), "select:" + entry.key, "nimda-directory-select"));
        append(choose, folderIcon(isOpen));
        const name = append(choose, element("span", "nimda-directory-name", (item.compressed ? "… / " : "") + entry.node.name)); name.title = entry.node.path || "";
        choose.setAttribute("aria-label", "浏览 " + entry.node.name);
        append(row, element("span", "nimda-directory-tree-count", count(entry.node.fileCount)));
        const group = append(wrapper, element("div", "nimda-directory-tree-children")); group.setAttribute("role", "group");
        shown.set(entry.key, { group, depth });
      }
      if (!roots.length) append(treeScroller, element("p", "nimda-directory-empty", options.emptyText || "暂无目录。"));
      const pager = append(aside, element("div", "nimda-directory-pager nimda-directory-tree-pager"));
      const previous = append(pager, button("上一页目录", () => { state.treePage--; state.treePageStart = ""; redraw("tree-previous"); }, "tree-previous")); previous.disabled = state.treePage === 0;
      append(pager, element("span", "", (state.treePage + 1) + " / " + model.pages.length));
      const next = append(pager, button("下一页目录", () => { state.treePage++; state.treePageStart = ""; redraw("tree-next"); }, "tree-next")); next.disabled = state.treePage >= model.pages.length - 1;
      return aside;
    }
    function content() {
      const pane = element("section", "nimda-directory-content"); pane.setAttribute("aria-label", "当前目录内容");
      const selected = entries.get(canonical(state.selectedPath));
      const header = append(pane, element("div", "nimda-directory-content-header"));
      const up = append(header, button("上级目录", () => { if (selected && selected.parent) select(selected.parent, "up"); }, "up"));
      up.disabled = !selected || !selected.parent;
      const location = append(header, element("div", "nimda-directory-location"));
      const path = append(location, element("strong", "nimda-directory-current-path", selected ? selected.node.path : "未选择目录")); path.title = selected ? selected.node.path : "";
      const items = selected ? selected.children.filter(node => node && (node.kind === "file" || node.kind === "directory")) : [];
      const folders = selected ? selected.directories.length : 0;
      append(location, element("span", "nimda-directory-current-stats", items.length + " 项 · " + folders + " 文件夹 · " + (items.length - folders) + " 文件"));
      contentScroller = append(pane, element("div", "nimda-directory-content-scroll"));
      const table = append(contentScroller, element("table", "nimda-directory-table"));
      tableHeading = append(table, element("thead"));
      const heading = append(tableHeading, element("tr"));
      for (const label of ["名称", "类型", "状态", "文件数"]) append(heading, element("th", "", label));
      const body = append(table, element("tbody")), pageSize = Math.min(contentCapacity, bounded(options.pageSize, 100, 1, 200));
      contentScroller.setAttribute("data-directory-page-capacity", String(pageSize));
      if (state.contentPageSize && state.contentPageSize !== pageSize) state.page = Math.floor(state.page * state.contentPageSize / pageSize);
      state.contentPageSize = pageSize;
      const pages = Math.max(1, Math.ceil(items.length / pageSize)); state.page = Math.min(state.page, pages - 1);
      for (const node of items.slice(state.page * pageSize, (state.page + 1) * pageSize)) {
        const row = append(body, element("tr", "nimda-directory-file-row")); row.setAttribute("data-directory-entry", node.path || "");
        if (!sampleFileRow) sampleFileRow = row;
        const cell = append(row, element("td", "nimda-directory-entry-name"));
        if (node.kind === "directory") {
          const entry = entries.get(canonical(node.path));
          const open = append(cell, button("", () => { if (entry) select(entry, "up"); }, "enter:" + canonical(node.path), "nimda-directory-enter"));
          append(open, folderIcon(false)); append(open, element("span", "nimda-directory-entry-text", node.name)); open.title = node.name || "";
          open.disabled = !entry;
        } else {
          const label = append(cell, element("span", "nimda-directory-entry-label"));
          const icon = append(label, element("span", "nimda-directory-icon is-file")); icon.setAttribute("aria-hidden", "true");
          const name = append(label, element("span", "nimda-directory-entry-text", node.name)); name.title = node.name || "";
        }
        append(row, element("td", "", node.kind === "directory" ? "文件夹" : "文件"));
        const statusText = statuses[node.status] || (node.changed ? "变化" : "未变化");
        const status = append(row, element("td", "nimda-directory-entry-status", statusText)); status.title = statusText;
        append(row, element("td", "nimda-directory-entry-count", node.kind === "file" ? 1 : count(node.fileCount)));
      }
      if (!items.length) {
        const cell = append(append(body, element("tr")), element("td", "nimda-directory-empty", selected ? "此目录为空。" : options.emptyText || "暂无目录。")); cell.setAttribute("colspan", "4");
      }
      const pager = append(pane, element("div", "nimda-directory-pager"));
      const previous = append(pager, button("上一页", () => { state.page--; redraw("previous"); }, "previous")); previous.disabled = state.page === 0;
      append(pager, element("span", "", "第 " + (state.page + 1) + " / " + pages + " 页 · 共 " + items.length + " 项"));
      const next = append(pager, button("下一页", () => { state.page++; redraw("next"); }, "next")); next.disabled = state.page >= pages - 1;
      return pane;
    }
    function height(node, fallback) {
      const value = node && typeof node.getBoundingClientRect === "function" ? node.getBoundingClientRect().height : node && node.clientHeight;
      return value > 0 ? value : fallback;
    }
    function measure() {
      if (destroyed || measuring) return;
      const treeHeight = treeScroller && treeScroller.clientHeight, contentHeight = contentScroller && contentScroller.clientHeight;
      // A hidden feature has no measurable height. Keep the last valid capacity
      // (initially ten rows) until it is visible again instead of jumping pages.
      const nextTree = treeHeight > 0 ? Math.max(1, Math.floor((treeHeight - 1) / height(sampleTreeRow, 26))) : treeCapacity;
      const nextContent = contentHeight > 0 ? Math.max(1, Math.floor((contentHeight - height(tableHeading, 26) - 1) / height(sampleFileRow, 28))) : contentCapacity;
      if (nextTree === treeCapacity && nextContent === contentCapacity) return;
      treeCapacity = nextTree; contentCapacity = nextContent;
      measuring = true;
      try { render(); } finally { measuring = false; }
    }
    function render(capture, focusKey) {
      if (destroyed) return;
      if (capture !== false) {
        if (!focusKey) for (const [key, control] of controls) if (control === document.activeElement) { focusKey = key; break; }
      }
      controls = new Map(); sampleTreeRow = sampleFileRow = null;
      section = element("section", "nimda-directory-browser"); section.setAttribute("aria-label", options.title || "目录浏览器");
      if (options.title) append(section, element("div", "nimda-directory-browser-title", options.title));
      const split = append(section, element("div", "nimda-directory-split"));
      append(split, tree()); append(split, content());
      clear(host); append(host, section);
      const focused = controls.get(focusKey);
      if (focused && !focused.disabled && typeof focused.focus === "function") focused.focus({ preventScroll: true });
      if (observer) { observer.disconnect(); observer.observe(treeScroller); observer.observe(contentScroller); }
      measure();
    }
    const Observer = view.ResizeObserver || (typeof globalThis.ResizeObserver === "function" ? globalThis.ResizeObserver : null);
    if (Observer) observer = new Observer(measure);
    else if (typeof view.addEventListener === "function") view.addEventListener("resize", measure);
    index(); render(false);
    return {
      update(next) {
        if (destroyed) return;
        options = Object.assign({}, options, next || {});
        if (next && next.state && next.state !== state) {
          state = next.state; treeScroller = contentScroller = null;
          treeCapacity = bounded(state.treePageCapacity, 10, 1, 2000); contentCapacity = bounded(state.contentPageSize, 10, 1, 200);
        }
        index(); render();
      },
      destroy() {
        if (destroyed) return;
        destroyed = true;
        if (observer) observer.disconnect();
        else if (typeof view.removeEventListener === "function") view.removeEventListener("resize", measure);
        if (section && section.parentNode === host) host.removeChild(section);
        treeScroller = contentScroller = section = null; controls.clear(); entries.clear();
      }
    };
  }

  common.DirectoryBrowser = { mount };
})(window);
