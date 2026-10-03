"""Repair the independently verified 1981/2022 Urusei bindings and two bad links.

The media stays untouched. Default mode is read-only. Apply requires the exact
preview hash and backs up catalog, derived index and changed shortcuts together.
Existing shortcut properties are preserved when changing only their targets.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("catalog_binding_migration", ROOT / "scripts/repair-catalog-bindings.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
links = migration.links
history_catalog_root = migration.history_catalog_root
from collection_detail.save import catalog_write_transaction
from work_catalog_yaml.persistence import FileWrite, commit_file_writes, history_snapshot_name
from work_catalog_yaml.yaml_io import load_yaml_string, dump_yaml_string


TITLE = "うる星やつら"
DEFAULT_MEDIA_ROOT = Path("Y:/うる星やつら")
REPAIRS = (
    {"begin_date": "19811014", "end_date": "19860319",
     "press": (("VCB", "うる星やつら_BDRip"), ("DBDR", "うる星やつら_BDRip(DBDR)"))},
    {"begin_date": "20221013", "end_date": "20230323",
     "press": (("JSUM", "うる星やつら 2022_BDRip"), ("DBD", "うる星やつら 2022_BDRip(DBDR)"))},
)


def _read_shortcut_targets(paths: list[Path]) -> dict:
    return links._windows_shortcut_targets(paths)


def _retarget_shortcut_bytes(previous: bytes, target: Path) -> bytes:
    """Edit a temporary copy, retaining icon/arguments/description/custom fields."""
    with tempfile.TemporaryDirectory(prefix="nimda-link-repair-") as directory:
        stage = Path(directory) / "link.lnk"
        stage.write_bytes(previous)
        environment = dict(os.environ, NIMDA_REPAIR_LINK=str(stage), NIMDA_REPAIR_TARGET=str(target))
        powershell = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = """$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:NIMDA_REPAIR_LINK)
$oldTarget = $link.TargetPath
$link.TargetPath = $env:NIMDA_REPAIR_TARGET
if ([string]::IsNullOrWhiteSpace($link.WorkingDirectory) -or $link.WorkingDirectory -eq $oldTarget) {
    $link.WorkingDirectory = $env:NIMDA_REPAIR_TARGET
}
$link.Save()
"""
        subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                       env=environment, check=True, capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        actual = _read_shortcut_targets([stage]).get(str(stage)) or {}
        if not actual.get("target_resolved") or migration._path_key(actual.get("target_path")) != migration._path_key(target):
            raise ValueError("临时快捷方式目标校验失败；没有修改正式文件")
        return stage.read_bytes()


def repair(settings, *, media_root: Path = DEFAULT_MEDIA_ROOT, apply: bool = False,
           expect_plan_sha256: str | None = None) -> dict:
    if settings.filesystem_root is None:
        raise ValueError("未配置作品数据库")
    if apply and not expect_plan_sha256:
        raise ValueError("执行必须提供已审预览的 expect_plan_sha256")
    media_root = Path(media_root)
    if not links._ordinary_catalog_target(str(media_root)):
        raise ValueError("作品根目录不存在或不是普通目录")
    if not any(media_root != root and media_root.is_relative_to(root) for root in links.resource_roots()):
        raise ValueError("作品目录不在配置资源根目录内")
    with catalog_write_transaction(settings.filesystem_root):
        works = links._load_catalog_works(settings)
        sources = {links._catalog_relpath(settings, path): path for path in links._catalog_yaml_paths(settings)}
        index_path = links._link_index_db_path()
        if not migration.ordinary(index_path):
            raise ValueError("索引必须为现有普通文件")
        index_before = index_path.read_bytes()
        index = load_yaml_string(index_before.decode("utf-8"))
        if not isinstance(index, dict) or index.get("catalog_root") != links._settings_catalog_root_key(settings):
            raise ValueError("索引不属于当前作品数据库")
        raw_items = index.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("索引条目格式错误")
        accepted, _ = links._validated_previous_index_items(works, raw_items)
        by_key = {item["entry_key"]: item for item in accepted}
        mappings = []
        documents = {}
        targets = []
        shortcut_roots = links.shortcut_roots()
        for definition in REPAIRS:
            matches = [work for work in works if work.get("name") == TITLE
                       and work.get("country") == "japan" and work.get("domain") == "animation"
                       and work.get("release_type") == "tv"
                       and work.get("begin_date") == definition["begin_date"]
                       and work.get("end_date") == definition["end_date"]]
            if len(matches) != 1:
                raise ValueError(f"作品身份无法唯一确认：{definition['begin_date']}")
            work = matches[0]
            presses = work.get("press") or []
            if len(presses) != len(definition["press"]):
                raise ValueError("压制记录已变化，需要重新核对")
            mapping = {"yaml_source_rel": work["yaml_source_rel"], "index_in_file": work["index_in_file"],
                       "path": str(media_root), "press": []}
            for group, relative in definition["press"]:
                candidates = [press for press in presses if press.get("press_format") == "BDRip" and press.get("press_group") == group]
                if len(candidates) != 1:
                    raise ValueError("压制身份无法唯一确认")
                press = candidates[0]
                target = media_root / relative
                if not links._ordinary_catalog_target(str(target)):
                    raise ValueError(f"目标目录不存在或不安全：{target}")
                key = links._index_entry_key(work, press)
                item = by_key.get(key)
                if item is None:
                    raise ValueError("旧索引身份冲突或重复，不能定向修复")
                shortcut = Path(item["shortcut_path"])
                allowed = next((root for root in shortcut_roots if shortcut.is_relative_to(root)), None)
                if allowed is None or shortcut.suffix.lower() != ".lnk" or not migration.ordinary(shortcut):
                    raise ValueError(f"快捷方式缺失或超出允许范围：{shortcut}")
                if not links._ordinary_catalog_target(str(shortcut.parent)):
                    raise ValueError("快捷方式父目录不是普通目录")
                expected_shortcut = links._safe_shortcut_path_from_rel(allowed, item["shortcut_relpath"])
                if migration._path_key(expected_shortcut) != migration._path_key(shortcut):
                    raise ValueError("索引快捷方式路径与相对位置不一致")
                targets.append({"entry_key": key, "year": definition["begin_date"][:4], "press_group": group,
                                "shortcut": shortcut, "target": target, "previous": shortcut.read_bytes()})
                mapping["press"].append({"press_key": press["press_key"], "press_path": relative})
            path = sources[work["yaml_source_rel"]]
            documents.setdefault(path, migration.read_document(path))
            mappings.append(mapping)

        actual = _read_shortcut_targets([item["shortcut"] for item in targets])
        link_changes = []
        for item in targets:
            evidence = actual.get(str(item["shortcut"])) or {}
            if not evidence.get("target_resolved") or not evidence.get("target_path"):
                raise ValueError(f"无法读取实际快捷方式：{item['shortcut']}")
            item["old_target"] = evidence["target_path"]
            if migration._path_key(item["old_target"]) != migration._path_key(item["target"]):
                link_changes.append(item)

        changes = []
        staged = []
        for mapping in mappings:
            path = sources[mapping["yaml_source_rel"]]
            before, document = documents[path]
            fields = migration.fill_mapping(document, mapping)
            if fields:
                changes.append({**mapping, "changes": fields})
        for path, (before, document) in documents.items():
            after = migration.serialize(document, before)
            migration.load_jp_tv_entries_from_yaml(load_yaml_string(after.decode("utf-8")))
            if after != before:
                staged.append(FileWrite(path, after, before))
        projected = links._merge_ui_mappings(works, links._ui_mapping_items({"works": mappings}))
        selected_keys = {item["entry_key"] for item in targets}
        canonical = {item["entry_key"]: item for item in links._index_entries_from_works(projected)
                     if item["entry_key"] in selected_keys}
        if len(canonical) != 4 or not all(item["target_source"] == "catalog" and item["target_exists"] for item in canonical.values()):
            raise ValueError("修复后目录绑定校验失败")
        projected_index = copy.deepcopy(index)
        projected_index["items"] = [canonical.get(item.get("entry_key"), item) if isinstance(item, dict) else item for item in raw_items]
        material = {"sources": {str(path): migration.digest(before) for path, (before, _) in documents.items()},
                    "index": migration.digest(index_before), "mappings": mappings, "canonical": canonical,
                    "links": [{"path": str(item["shortcut"]), "sha256": migration.digest(item["previous"]),
                               "before": item["old_target"], "after": str(item["target"])} for item in targets]}
        plan_hash = migration.digest(json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if expect_plan_sha256 and expect_plan_sha256 != plan_hash:
            raise ValueError("数据或快捷方式已变化；请重新预览，未写入")
        result = {"ok": True, "applied": False, "plan_sha256": plan_hash, "changes": changes,
                  "shortcuts_changed": len(link_changes), "bindings": material["links"], "files": [], "backups": []}
        if apply:
            for path, (before, _) in documents.items():
                if path.read_bytes() != before:
                    raise ValueError("数据库在扫描后变化")
            for item in targets:
                if item["shortcut"].read_bytes() != item["previous"] or not links._ordinary_catalog_target(str(item["target"])):
                    raise ValueError("快捷方式或媒体目录在扫描后变化")
            projected_index["generated_at"] = datetime.now().isoformat(timespec="seconds")
            staged.append(FileWrite(index_path, dump_yaml_string(projected_index).encode("utf-8"), index_before))
            for item in link_changes:
                staged.append(FileWrite(item["shortcut"], _retarget_shortcut_bytes(item["previous"], item["target"]), item["previous"]))
            history = history_catalog_root(settings) / "urusei-binding-repair"
            committed = []
            for write in staged:
                backup = history / history_snapshot_name(write.target)
                committed.append(FileWrite(write.target, write.content, write.previous, backup))
                result["backups"].append(str(backup))
                result["files"].append({"path": str(write.target), "backup": str(backup),
                                        "before_sha256": migration.digest(write.previous), "after_sha256": migration.digest(write.content)})
            commit_file_writes(committed)
            result["applied"] = True
        return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expect-plan-sha256")
    args = parser.parse_args()
    result = repair(migration.load_jp_tv_browse_settings(), apply=args.apply, expect_plan_sha256=args.expect_plan_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
