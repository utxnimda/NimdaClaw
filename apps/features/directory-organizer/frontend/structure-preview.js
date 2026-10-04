(function (global) {
  "use strict";

  // This is a read-only projection of the executor's per-file plan. A directory
  // summary never replaces, edits, or authorizes any of those file operations.
  const lower = value => value.toLowerCase();
  const join = (root, relative) => !relative || relative === "." ? root : root + (/[\\/]$/.test(root) ? "" : root.includes("\\") ? "\\" : "/") + relative;

  function parse(raw) {
    const text = String(raw || ""), parts = [], displays = [];
    const head = /^[\\/]{2}/.test(text) ? "//" : /^[\\/]/.test(text) ? "/" : "";
    const expression = /[^\\/]+/g;
    let match;
    while ((match = expression.exec(text))) {
      if (match[0] === ".") continue;
      parts.push(match[0]); displays.push(text.slice(0, expression.lastIndex));
    }
    return { text, head, parts, displays, keys: parts.map(lower) };
  }

  const keyAt = (parsed, length) => parsed.head + parsed.keys.slice(0, length).join("/");
  const beneath = (path, ancestor) => path === ancestor || path.startsWith(ancestor + "/");
  const relativeText = parts => parts.length ? parts.join("/") : ".";
  const compare = (left, right) => left.kind !== right.kind ? (left.kind === "directory" ? -1 : 1) :
    lower(left.name) < lower(right.name) ? -1 : lower(left.name) > lower(right.name) ? 1 : left.name < right.name ? -1 : left.name > right.name ? 1 : 0;

  function graph() {
    const directories = new Map(), meta = new WeakMap();
    function directory(raw) {
      const parsed = parse(raw);
      let parent = null, result = null;
      for (let depth = 1; depth <= parsed.parts.length; depth++) {
        const key = keyAt(parsed, depth);
        result = directories.get(key);
        if (!result) {
          result = { kind: "directory", name: parsed.parts[depth - 1], path: parsed.displays[depth - 1], children: [], fileCount: 0, movedCount: 0, status: "unchanged", changed: false };
          const info = { key, parts: parsed.parts.slice(0, depth), parent, children: new Map(), initial: false, touched: false, emptyCount: 0, retainedEmptyCount: 0, mapKey: null, mapInvalid: false };
          meta.set(result, info); directories.set(key, result);
          if (parent) meta.get(parent).children.set(key, result);
        }
        parent = result;
      }
      // Empty roots are only a defensive fallback for an incomplete UI draft.
      if (!result) {
        const key = parsed.head;
        result = directories.get(key);
        if (!result) {
          result = { kind: "directory", name: raw || ".", path: raw || "", children: [], fileCount: 0, movedCount: 0, status: "unchanged", changed: false };
          meta.set(result, { key, parts: [], parent: null, children: new Map(), initial: false, touched: false, emptyCount: 0, retainedEmptyCount: 0, mapKey: null, mapInvalid: false });
          directories.set(key, result);
        }
      }
      return result;
    }
    function file(raw, record) {
      const parsed = parse(raw), name = parsed.parts[parsed.parts.length - 1] || String(raw), key = keyAt(parsed, parsed.parts.length);
      const parentText = parsed.parts.length > 1 ? parsed.displays[parsed.parts.length - 2] : parsed.head;
      const parent = directory(parentText);
      const node = { kind: "file", name, path: raw, children: [], fileCount: 1, movedCount: record.changed ? 1 : 0, status: record.changed ? "moved" : "unchanged", changed: record.changed };
      meta.set(node, { key, parts: parsed.parts, parent, record });
      const children = meta.get(parent).children;
      let childKey = key;
      while (children.has(childKey)) childKey += "\u0000";
      children.set(childKey, node);
      return node;
    }
    function aggregate(root, after) {
      const pending = [root], ordered = [];
      while (pending.length) {
        const node = pending.pop(); ordered.push(node);
        if (node.kind === "directory") pending.push(...meta.get(node).children.values());
      }
      for (let index = ordered.length - 1; index >= 0; index--) {
        const node = ordered[index];
        if (node.kind !== "directory") continue;
        const info = meta.get(node), children = [...info.children.values()];
        node.children = children.sort(compare);
        node.fileCount = children.reduce((sum, item) => sum + item.fileCount, 0);
        node.movedCount = children.reduce((sum, item) => sum + item.movedCount, 0);
        info.emptyCount = node.fileCount === 0 ? 1 : children.reduce((sum, item) => sum + (item.kind === "directory" ? meta.get(item).emptyCount : 0), 0);
        info.retainedEmptyCount = (info.initial && !children.length ? 1 : 0) + children.reduce((sum, item) => sum + (item.kind === "directory" ? meta.get(item).retainedEmptyCount : 0), 0);
        node.changed = node.movedCount > 0;
        node.status = after && !info.initial ? "added" : !node.fileCount && after ? "retained" : !node.movedCount ? "unchanged" : node.movedCount === node.fileCount ? "contents_changed" : "mixed";
      }
      return ordered;
    }
    return { directories, meta, directory, file, aggregate };
  }

  function buildPreview(input) {
    input = input || {};
    const sourceRoot = String(input.sourceRoot || ""), targetRoot = String(input.targetRoot || sourceRoot);
    const sourceParsed = parse(sourceRoot), targetParsed = parse(targetRoot);
    const sourceKey = keyAt(sourceParsed, sourceParsed.parts.length), targetKey = keyAt(targetParsed, targetParsed.parts.length);
    const suppliedDirectories = Array.isArray(input.sourceDirectories), complete = suppliedDirectories && input.sourceComplete !== false;
    const warnings = [];
    if (!suppliedDirectories) warnings.push("未提供完整源目录快照；只显示已知目录，移动摘要保持逐文件。");
    if (input.sourceComplete === false) warnings.push("源目录包含未完整枚举的资源；不合并目录移动，并保守保留来源目录。");
    const beforeGraph = graph(), afterGraph = graph();
    const before = beforeGraph.directory(sourceRoot), originalAfterRoot = afterGraph.directory(sourceRoot);
    const rawFiles = Array.isArray(input.files) ? input.files : [];
    const records = [], fileMoves = [], destinations = new Map(), sources = new Set();
    let duplicateSource = false;

    (suppliedDirectories ? input.sourceDirectories : []).forEach(relative => {
      const raw = join(sourceRoot, String(relative));
      beforeGraph.directory(raw); afterGraph.directory(raw);
    });
    for (const item of rawFiles) {
      if (!item || typeof item.source_rel !== "string" || !item.source_rel) continue;
      const sourceRelative = item.source_rel, targetRelative = typeof item.target_rel === "string" ? item.target_rel : sourceRelative;
      const source = join(sourceRoot, sourceRelative), proposedTarget = join(targetRoot, targetRelative);
      const sourcePath = parse(source), targetPath = parse(proposedTarget);
      const sourceIdentity = keyAt(sourcePath, sourcePath.parts.length), targetIdentity = keyAt(targetPath, targetPath.parts.length);
      const changed = item.changed !== false && sourceIdentity !== targetIdentity;
      const record = { source, target: changed ? proposedTarget : source, sourceRelative, targetRelative: changed ? targetRelative : sourceRelative, source_rel: sourceRelative, target_rel: changed ? targetRelative : sourceRelative, kind: "file", fileCount: 1, changed, sourcePath, targetPath: changed ? targetPath : sourcePath };
      record.sourceNode = beforeGraph.file(source, record);
      // Recreate inferred parents as well as explicitly snapshotted directories.
      afterGraph.directory(beforeGraph.meta.get(record.sourceNode).parent.path);
      const destinationKey = changed ? targetIdentity : sourceIdentity;
      destinations.set(destinationKey, (destinations.get(destinationKey) || 0) + 1);
      if (sources.has(sourceIdentity)) duplicateSource = true;
      sources.add(sourceIdentity); records.push(record);
      if (changed) fileMoves.push(moveRecord(record));
    }

    beforeGraph.aggregate(before, false);
    // Directory entries present before execution are preserved unless cleanup
    // can remove an empty ancestor of an actually moved file.
    // The source's ancestors also already exist, which matters when the target
    // is a parent of the selected source rather than a newly created sibling.
    for (const node of afterGraph.directories.values()) afterGraph.meta.get(node).initial = true;
    // FileTransaction.run creates the target root even for an empty file plan.
    const targetAfterRoot = afterGraph.directory(sourceKey === targetKey ? sourceRoot : targetRoot);
    for (const record of records) {
      afterGraph.file(record.target, record);
      if (!record.changed) continue;
      // Mark only source ancestors. The target directory and its ancestors are
      // protected by the same rule as FileTransaction.complete().
      let parent = beforeGraph.meta.get(record.sourceNode).parent;
      while (parent && beneath(beforeGraph.meta.get(parent).key, sourceKey)) {
        const afterNode = afterGraph.directories.get(beforeGraph.meta.get(parent).key);
        if (afterNode) afterGraph.meta.get(afterNode).touched = true;
        parent = beforeGraph.meta.get(parent).parent;
      }
    }
    if (input.sourceComplete !== false) {
      const cleanup = [...afterGraph.directories.values()].filter(node => afterGraph.meta.get(node).touched).sort((left, right) => afterGraph.meta.get(right).parts.length - afterGraph.meta.get(left).parts.length);
      for (const node of cleanup) {
        const info = afterGraph.meta.get(node);
        if (info.children.size || beneath(targetKey, info.key)) continue;
        if (info.parent) afterGraph.meta.get(info.parent).children.delete(info.key);
        afterGraph.directories.delete(info.key);
      }
    }

    let after;
    if (beneath(sourceKey, targetKey)) after = [targetAfterRoot];
    else if (beneath(targetKey, sourceKey)) after = [originalAfterRoot];
    else after = [targetAfterRoot].concat(afterGraph.directories.has(sourceKey) ? [originalAfterRoot] : []);
    after.forEach(root => afterGraph.aggregate(root, true));

    // For each source ancestor, compare the unchanged suffix once per file.
    // No descendant list is materialized for a candidate directory.
    if (complete && !duplicateSource) for (const record of records) {
      let common = 0;
      const sourceParts = record.sourcePath.keys, targetParts = record.targetPath.keys;
      while (common < sourceParts.length && common < targetParts.length && sourceParts[sourceParts.length - common - 1] === targetParts[targetParts.length - common - 1]) common++;
      const destinationKey = keyAt(record.targetPath, targetParts.length), duplicated = destinations.get(destinationKey) !== 1;
      let directory = beforeGraph.meta.get(record.sourceNode).parent;
      while (directory && beneath(beforeGraph.meta.get(directory).key, sourceKey)) {
        const info = beforeGraph.meta.get(directory), suffixLength = sourceParts.length - info.parts.length;
        if (!record.changed || duplicated || suffixLength > common) info.mapInvalid = true;
        else {
          const mapped = keyAt(record.targetPath, targetParts.length - suffixLength);
          if (info.mapKey === null) info.mapKey = mapped;
          else if (info.mapKey !== mapped) info.mapInvalid = true;
        }
        directory = info.parent;
      }
    }

    const moves = [], pending = [before];
    while (pending.length) {
      const node = pending.pop();
      if (node.kind === "file") {
        const record = beforeGraph.meta.get(node).record;
        if (record.changed) moves.push(moveRecord(record));
        continue;
      }
      const info = beforeGraph.meta.get(node), destination = info.mapKey && afterGraph.directories.get(info.mapKey);
      const canMerge = complete && !duplicateSource && node.fileCount > 0 && node.fileCount === node.movedCount && !info.emptyCount && !info.mapInvalid && destination && !beneath(info.mapKey, info.key) && !beneath(info.key, info.mapKey) && destination.fileCount === node.fileCount && !afterGraph.meta.get(destination).retainedEmptyCount;
      if (canMerge) {
        const sourceRelative = relativeText(info.parts.slice(sourceParsed.parts.length));
        const destinationInfo = afterGraph.meta.get(destination);
        const targetRelative = relativeText(destinationInfo.parts.slice(targetParsed.parts.length));
        moves.push({ kind: "directory", source: node.path, target: destination.path, sourceRelative, targetRelative, source_rel: sourceRelative, target_rel: targetRelative, fileCount: node.fileCount });
        markDirectoryMove(node); markDirectoryMove(destination);
      } else {
        for (let index = node.children.length - 1; index >= 0; index--) pending.push(node.children[index]);
      }
    }
    const directoryMoves = moves.filter(item => item.kind === "directory").length;
    const stats = { totalFiles: records.length, changedFiles: fileMoves.length, unchangedFiles: records.length - fileMoves.length, directoryMoves, fileMoves: moves.length - directoryMoves, summaryMoves: moves.length };
    return { before, after, moves, fileMoves, counts: stats, stats, warnings };
  }

  function moveRecord(record) {
    return { kind: "file", source: record.source, target: record.target, sourceRelative: record.sourceRelative, targetRelative: record.targetRelative, source_rel: record.sourceRelative, target_rel: record.targetRelative, fileCount: 1 };
  }

  function markDirectoryMove(root) {
    const pending = [root];
    while (pending.length) {
      const node = pending.pop();
      if (node.kind !== "directory") continue;
      node.status = "moved";
      for (const child of node.children) if (child.kind === "directory") pending.push(child);
    }
  }

  global.NimdaOrganizerStructure = { buildPreview };
})(window);
