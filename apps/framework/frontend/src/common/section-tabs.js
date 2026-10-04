(function (global) {
  "use strict";

  const common = global.NimdaCommon = global.NimdaCommon || {};
  let sequence = 0;
  const claimedIds = new Set();
  const attribute = (node, name) => typeof node.getAttribute === "function" ? node.getAttribute(name) :
    name === "id" && node.id ? node.id : node.attributes && Object.prototype.hasOwnProperty.call(node.attributes, name) ? node.attributes[name] : null;
  function restoreAttribute(node, name, value) {
    if (value !== null && value !== undefined) node.setAttribute(name, value);
    else if (typeof node.removeAttribute === "function") node.removeAttribute(name);
    else if (node.attributes) delete node.attributes[name];
    if (name === "id") node.id = value || "";
  }

  /** Switch existing caller-owned panels without recreating their contents. */
  function mount(host, options) {
    if (!host || typeof host.appendChild !== "function") throw new TypeError("SectionTabs requires a host element");
    options = options || {};
    const document = host.ownerDocument || global.document || (typeof globalThis.document !== "undefined" ? globalThis.document : null);
    if (!document || typeof document.createElement !== "function") throw new TypeError("SectionTabs requires a document");
    const state = options.state || {}, rows = [], byId = new Map(), panels = new Set();
    const ownedIds = new Set(), reservedIds = new Set((Array.isArray(options.items) ? options.items : []).map(item => item && item.panel && attribute(item.panel, "id")).filter(Boolean));
    let destroyed = false;
    const prefix = "nimda-section-tabs-" + (++sequence);
    function uniqueId(suffix) {
      const base = prefix + "-" + suffix;
      let value = base, attempt = 0;
      while (claimedIds.has(value) || reservedIds.has(value) || typeof document.getElementById === "function" && document.getElementById(value)) value = base + "-" + (++attempt);
      claimedIds.add(value); ownedIds.add(value);
      return value;
    }
    const tablist = document.createElement("div");
    tablist.className = "nimda-section-tabs";
    tablist.setAttribute("role", "tablist");
    tablist.setAttribute("aria-label", options.label || "功能分区");
    tablist.setAttribute("aria-orientation", "horizontal");

    function paint() {
      for (const row of rows) {
        const active = row.id === state.activeId;
        row.button.setAttribute("aria-selected", String(active));
        row.button.tabIndex = active ? 0 : -1;
        row.button.className = "nimda-section-tab" + (active ? " is-active" : "");
        row.panel.hidden = !active;
      }
    }
    function select(id, settings) {
      const row = byId.get(String(id));
      if (destroyed || !row) return false;
      const changed = state.activeId !== row.id;
      if (changed) { state.activeId = row.id; paint(); }
      if (settings && settings.focus && typeof row.button.focus === "function") row.button.focus({ preventScroll: true });
      if (changed && typeof options.onChange === "function") options.onChange(row.id);
      return true;
    }
    function setBadge(id, text) {
      const row = byId.get(String(id));
      if (destroyed || !row) return false;
      const value = text === undefined || text === null ? "" : String(text);
      row.badge.textContent = value;
      row.badge.hidden = !value.trim();
      restoreAttribute(row.button, "aria-description", value.trim() ? value : null);
      return true;
    }

    for (const item of Array.isArray(options.items) ? options.items : []) {
      if (!item || item.id === undefined || item.id === null || !String(item.id) || !item.panel || typeof item.panel.setAttribute !== "function") continue;
      const id = String(item.id), panel = item.panel;
      if (byId.has(id) || panels.has(panel)) continue;
      const label = item.label === undefined || item.label === null ? id : String(item.label);
      const original = { hidden: Boolean(panel.hidden), id: attribute(panel, "id"), role: attribute(panel, "role"), labelledBy: attribute(panel, "aria-labelledby"), marker: attribute(panel, "data-section-tab-panel") };
      let panelId = original.id;
      if (!panelId || claimedIds.has(panelId) || typeof document.getElementById === "function" && document.getElementById(panelId) && document.getElementById(panelId) !== panel) panelId = uniqueId("panel-" + rows.length);
      else { claimedIds.add(panelId); ownedIds.add(panelId); }
      const button = document.createElement("button"), badge = document.createElement("span"), text = document.createElement("span");
      const buttonId = uniqueId("tab-" + rows.length);
      button.type = "button"; button.id = buttonId; button.setAttribute("id", buttonId);
      button.setAttribute("role", "tab"); button.setAttribute("aria-controls", panelId);
      button.setAttribute("aria-label", label); button.setAttribute("data-section-tab", id);
      text.className = "nimda-section-tab-label"; text.textContent = label;
      badge.className = "nimda-section-tab-badge"; badge.hidden = true; badge.setAttribute("aria-hidden", "true");
      button.appendChild(text); button.appendChild(badge); tablist.appendChild(button);
      panel.id = panelId; panel.setAttribute("id", panelId); panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", buttonId); panel.setAttribute("data-section-tab-panel", prefix);
      const row = { id, panel, panelId, button, buttonId, badge, original, click: null, keydown: null };
      const index = rows.length;
      row.click = event => { event.preventDefault(); select(id, { focus: true }); };
      row.keydown = event => {
        if (destroyed || event.altKey || event.ctrlKey || event.metaKey) return;
        let next;
        if (event.key === "ArrowRight") next = (index + 1) % rows.length;
        else if (event.key === "ArrowLeft") next = (index + rows.length - 1) % rows.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = rows.length - 1;
        else return;
        event.preventDefault(); select(rows[next].id, { focus: true });
      };
      button.addEventListener("click", row.click); button.addEventListener("keydown", row.keydown);
      rows.push(row); byId.set(id, row); panels.add(panel);
    }
    const preferred = byId.has(String(state.activeId)) ? String(state.activeId) : byId.has(String(options.defaultId)) ? String(options.defaultId) : rows.length ? rows[0].id : "";
    state.activeId = preferred; paint(); host.appendChild(tablist);
    return {
      select,
      setBadge,
      destroy() {
        if (destroyed) return;
        destroyed = true;
        for (const row of rows) {
          if (typeof row.button.removeEventListener === "function") {
            row.button.removeEventListener("click", row.click); row.button.removeEventListener("keydown", row.keydown);
          }
          row.panel.hidden = row.original.hidden;
          // Restore only attributes still owned by this instance, preserving
          // unrelated caller edits made while the tabs were mounted.
          if (attribute(row.panel, "id") === row.panelId) restoreAttribute(row.panel, "id", row.original.id);
          if (attribute(row.panel, "role") === "tabpanel") restoreAttribute(row.panel, "role", row.original.role);
          if (attribute(row.panel, "aria-labelledby") === row.buttonId) restoreAttribute(row.panel, "aria-labelledby", row.original.labelledBy);
          if (attribute(row.panel, "data-section-tab-panel") === prefix) restoreAttribute(row.panel, "data-section-tab-panel", row.original.marker);
        }
        if (tablist.parentNode === host) host.removeChild(tablist);
        for (const id of ownedIds) claimedIds.delete(id);
        ownedIds.clear();
        byId.clear(); panels.clear();
      }
    };
  }

  common.SectionTabs = { mount };
})(window);
