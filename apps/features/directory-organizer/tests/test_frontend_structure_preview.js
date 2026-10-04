"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { performance } = require("node:perf_hooks");
const window = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../frontend/structure-preview.js"), "utf8"), { window });
const build = input => window.NimdaOrganizerStructure.buildPreview({ sourceRoot: "U:/A", targetRoot: "U:/A", sourceDirectories: [], sourceComplete: true, ...input });
const plain = value => JSON.parse(JSON.stringify(value));
const file = (source_rel, target_rel = source_rel, changed) => ({ source_rel, target_rel, ...(changed === undefined ? {} : { changed }) });
const flatten = roots => {
  const result = [], pending = Array.isArray(roots) ? [...roots] : [roots];
  while (pending.length) { const node = pending.pop(); result.push(node); pending.push(...node.children); }
  return result;
};
const paths = roots => flatten(roots).map(node => node.path.replaceAll("\\", "/"));

test("a nested directory rename is one outermost move with all details retained", () => {
  const input = { sourceDirectories: ["XXX", "XXX/sub"], files: [file("XXX/01.mkv", "YYY/01.mkv"), file("XXX/sub/02.ass", "YYY/sub/02.ass")] };
  const original = JSON.stringify(input), preview = build(input);
  assert.equal(JSON.stringify(input), original);
  assert.deepEqual(plain(preview.moves), [{ kind: "directory", source: "U:/A/XXX", target: "U:/A/YYY", sourceRelative: "XXX", targetRelative: "YYY", source_rel: "XXX", target_rel: "YYY", fileCount: 2 }]);
  assert.equal(preview.fileMoves.length, 2);
  assert.equal(preview.before.fileCount, 2);
  assert.equal(preview.before.status, "contents_changed");
  assert.equal(preview.before.children[0].status, "moved");
  assert.equal(preview.before.children[0].children[0].status, "moved");
  assert.equal(preview.after[0].fileCount, 2);
  assert.equal(preview.after[0].status, "contents_changed");
  assert.equal(preview.after[0].children[0].status, "moved");
  assert.equal(paths(preview.after).includes("U:/A/XXX"), false);
});

test("flattening a directory into its own parent is not a whole-directory move", () => {
  const preview = build({ sourceDirectories: ["XXX"], files: [file("XXX/a.mkv", "a.mkv"), file("XXX/b.ass", "b.ass")] });
  assert.equal(preview.stats.directoryMoves, 0);
  assert.equal(preview.moves.length, 2);
  assert.equal(preview.moves.every(item => item.kind === "file"), true);
});

test("a root-only rename collapses even when all relative paths are identical", () => {
  const preview = build({ targetRoot: "V:/作品/YYY", sourceDirectories: ["sub"], files: [file("a.mkv"), file("sub/b.ass")] });
  assert.equal(preview.moves.length, 1);
  assert.equal(preview.moves[0].kind, "directory");
  assert.equal(preview.moves[0].source_rel, ".");
  assert.equal(preview.moves[0].target_rel, ".");
  assert.equal(preview.moves[0].source, "U:/A");
  assert.equal(preview.moves[0].target, "V:/作品/YYY");
  assert.equal(preview.after.length, 1);
  assert.equal(preview.before.status, "moved");
  assert.equal(preview.after[0].status, "moved");
  assert.equal(preview.stats.changedFiles, 2);
});

test("unchanged files, partial moves and internal file renames prevent parent collapse", () => {
  const unchanged = build({ sourceDirectories: ["XXX"], files: [file("XXX/a.mkv", "YYY/a.mkv"), file("XXX/b.mkv", "XXX/b.mkv", false)] });
  assert.deepEqual(plain(unchanged.moves.map(item => item.kind)), ["file"]);
  assert.equal(unchanged.before.children[0].status, "mixed");
  assert.ok(paths(unchanged.after).includes("U:/A/XXX/b.mkv"));
  const renamed = build({ sourceDirectories: ["XXX"], files: [file("XXX/a.mkv", "YYY/renamed.mkv"), file("XXX/b.mkv", "YYY/b.mkv")] });
  assert.equal(renamed.moves.every(item => item.kind === "file"), true);
  assert.equal(renamed.before.status, "contents_changed");
  assert.equal(renamed.before.children[0].status, "contents_changed");
});

test("split destinations and destination mixing prevent false directory moves", () => {
  const split = build({ sourceDirectories: ["XXX"], files: [file("XXX/a.mkv", "YYY/a.mkv"), file("XXX/b.mkv", "ZZZ/b.mkv")] });
  assert.equal(split.moves.length, 2); assert.equal(split.stats.directoryMoves, 0);
  const mixed = build({ sourceDirectories: ["XXX", "YYY"], files: [file("XXX/a.mkv", "YYY/a.mkv"), file("YYY/keep.txt", "YYY/keep.txt", false)] });
  assert.equal(mixed.moves[0].kind, "file");
  const merging = build({ sourceDirectories: ["XXX", "ZZZ"], files: [file("XXX/a.mkv", "YYY/a.mkv"), file("ZZZ/b.mkv", "YYY/b.mkv")] });
  assert.equal(merging.stats.directoryMoves, 0);
});

test("source empty branches stay behind and prevent whole-directory collapse", () => {
  const preview = build({ targetRoot: "V:/B", sourceDirectories: ["XXX", "XXX/empty", "untouched", "untouched/deep"], files: [file("XXX/a.mkv")] });
  assert.equal(preview.moves[0].kind, "file");
  assert.equal(preview.after.length, 2);
  assert.ok(paths(preview.after[1]).includes("U:/A/XXX/empty"));
  assert.ok(paths(preview.after[1]).includes("U:/A/untouched/deep"));
  assert.equal(paths(preview.after[0]).includes("V:/B/XXX/empty"), false);
  assert.equal(preview.after[1].fileCount, 0);
});

test("an unrelated empty sibling does not block a valid smaller directory move", () => {
  const preview = build({ targetRoot: "V:/B", sourceDirectories: ["XXX", "empty"], files: [file("XXX/a.mkv")] });
  assert.equal(preview.moves.length, 1); assert.equal(preview.moves[0].kind, "directory");
  assert.equal(preview.moves[0].source_rel, "XXX");
  assert.ok(paths(preview.after).includes("U:/A/empty"));
});

test("pre-existing empty destination directories count as destination mixing", () => {
  const preview = build({ sourceDirectories: ["XXX", "YYY", "YYY/empty"], files: [file("XXX/a.mkv", "YYY/a.mkv")] });
  assert.equal(preview.stats.directoryMoves, 0);
  assert.ok(paths(preview.after).includes("U:/A/YYY/empty"));
});

test("segment comparisons do not mix similar textual prefixes", () => {
  const preview = build({ sourceDirectories: ["XXX", "XXX2", "XXX_backup"], files: [file("XXX/a.mkv", "YYY/a.mkv"), file("XXX2/b.mkv", "XXX2/b.mkv", false), file("XXX_backup/c.mkv", "XXX_backup/c.mkv", false)] });
  assert.equal(preview.moves[0].kind, "directory"); assert.equal(preview.moves[0].source_rel, "XXX");
  assert.equal(preview.moves[0].fileCount, 1);
  const roots = build({ sourceRoot: "U:/A", targetRoot: "U:/A2", sourceDirectories: ["empty"], files: [file("a.mkv")] });
  assert.equal(roots.after.length, 2);
});

test("Windows separators and case compare canonically while labels retain Unicode", () => {
  const preview = build({ sourceRoot: "U:\\作品", targetRoot: "u:/作品", sourceDirectories: ["XXX", "XXX\\字幕"], files: [file("XXX\\\\字幕\\你好.ass", "YYY//字幕/你好.ass")] });
  assert.equal(preview.moves.length, 1); assert.equal(preview.moves[0].kind, "directory");
  assert.equal(preview.moves[0].source_rel, "XXX");
  assert.equal(preview.fileMoves[0].source_rel, "XXX\\\\字幕\\你好.ass");
  assert.equal(preview.fileMoves[0].target_rel, "YYY//字幕/你好.ass");
  assert.ok(flatten(preview.before).some(node => node.name === "你好.ass"));
});

test("changed false preserves the actual source spelling and location", () => {
  const preview = build({ sourceRoot: "U:/Mixed", targetRoot: "u:/MIXED", sourceDirectories: ["Sub"], files: [file("Sub/A.MKV", "sub/a.mkv", false)] });
  assert.equal(preview.moves.length, 0); assert.equal(preview.after.length, 1);
  assert.ok(paths(preview.after).includes("U:/Mixed/Sub/A.MKV"));
  assert.equal(paths(preview.after).some(value => value.includes("sub/a.mkv")), false);
  const conservative = build({ targetRoot: "V:/Different", files: [file("a.mkv", "b.mkv", false)] });
  assert.equal(conservative.fileMoves.length, 0);
  assert.equal(conservative.after.length, 2);
  assert.ok(paths(conservative.after).includes("U:/A/a.mkv"));
});

test("missing or incomplete directory snapshots do not claim directory moves", () => {
  const files = [file("XXX/a.mkv", "YYY/a.mkv")];
  const missing = build({ sourceDirectories: undefined, files });
  assert.equal(missing.moves[0].kind, "file"); assert.ok(missing.warnings.length);
  const incomplete = build({ sourceDirectories: ["XXX"], sourceComplete: false, files });
  assert.equal(incomplete.moves[0].kind, "file"); assert.ok(incomplete.warnings.length);
  assert.ok(paths(incomplete.after).includes("U:/A/XXX"));
});

test("overlapping source and target roots render one complete tree without duplicates", () => {
  const nested = build({ targetRoot: "U:/A/New", sourceDirectories: ["empty"], files: [file("a.mkv")] });
  assert.equal(nested.after.length, 1); assert.equal(nested.after[0].path, "U:/A");
  assert.ok(paths(nested.after).includes("U:/A/New/a.mkv"));
  assert.ok(paths(nested.after).includes("U:/A/empty"));
  const parent = build({ sourceRoot: "U:/A/Old", targetRoot: "U:/A", sourceDirectories: ["empty"], files: [file("a.mkv")] });
  assert.equal(parent.after.length, 1); assert.equal(parent.after[0].path, "U:/A");
  assert.ok(paths(parent.after).includes("U:/A/Old/empty"));
  assert.ok(paths(parent.after).includes("U:/A/a.mkv"));
  const parentRename = build({ sourceRoot: "U:/A/Old", targetRoot: "U:/A", files: [file("a.mkv", "renamed.mkv")] });
  assert.equal(parentRename.after[0].status, "contents_changed");
});

test("moving content into its own descendant is not called a whole directory move", () => {
  const preview = build({ targetRoot: "U:/A/B", files: [file("a.mkv")] });
  assert.equal(preview.moves.length, 1); assert.equal(preview.moves[0].kind, "file");
  assert.equal(preview.after.length, 1); assert.equal(preview.after[0].path, "U:/A");
  assert.ok(paths(preview.after).includes("U:/A/B/a.mkv"));
});

test("duplicate targets never produce an apparent whole-directory move", () => {
  const preview = build({ sourceDirectories: ["X", "Y"], files: [file("X/a.mkv", "Z/a.mkv"), file("Y/a.mkv", "Z/a.mkv")] });
  assert.equal(preview.stats.directoryMoves, 0);
  assert.equal(preview.after[0].fileCount, 2);
});

test("unchanged and empty plans retain directory topology and report no moves", () => {
  const preview = build({ sourceDirectories: ["empty", "empty/child"], files: [] });
  assert.equal(preview.moves.length, 0);
  assert.deepEqual(paths(preview.before), paths(preview.after));
  assert.equal(preview.stats.totalFiles, 0);
  const emptyTarget = build({ targetRoot: "V:/NotCreated", sourceDirectories: ["empty"], files: [] });
  assert.equal(emptyTarget.after.length, 2); assert.equal(emptyTarget.after[0].path, "V:/NotCreated");
  assert.ok(paths(emptyTarget.after).includes("U:/A/empty"));
});

test("UNC roots normalize separators without confusing server/share boundaries", () => {
  const preview = build({ sourceRoot: "\\\\server\\share\\Old", targetRoot: "//SERVER/share/New", sourceDirectories: ["字幕"], files: [file("字幕/a.ass")] });
  assert.equal(preview.moves.length, 1); assert.equal(preview.moves[0].source_rel, ".");
  assert.equal(preview.after.length, 1);
  assert.equal(preview.moves[0].source, "\\\\server\\share\\Old");
});

test("12,000 files are aggregated without quadratic descendant scans", () => {
  const files = [], sourceDirectories = ["XXX"];
  for (let folder = 0; folder < 120; folder++) {
    sourceDirectories.push("XXX/" + folder);
    for (let index = 0; index < 100; index++) files.push(file(`XXX/${folder}/${index}.mkv`, `YYY/${folder}/${index}.mkv`));
  }
  const started = performance.now(), preview = build({ sourceDirectories, files });
  const elapsed = performance.now() - started;
  assert.equal(preview.moves.length, 1); assert.equal(preview.moves[0].fileCount, 12000);
  assert.equal(preview.fileMoves.length, 12000); assert.equal(preview.after[0].fileCount, 12000);
  assert.ok(elapsed < 5000, `Preview took ${Math.round(elapsed)} ms`);
});
