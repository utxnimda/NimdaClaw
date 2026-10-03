"""Preview (default) or back up and normalize catalog broadcast date scalars.

Only date.start/end tokens change; comments, ordering and unrelated YAML text
are retained. Invalid calendar dates and reversed ranges are reported, never
guessed. --apply uses the same lock/atomic-write/backup contract as the app.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/framework/backend"))

from ruamel.yaml import YAML
from ruamel.yaml.events import AliasEvent
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode
from work_catalog_yaml.jp_tv.browse_settings import load_jp_tv_browse_settings
from work_catalog_yaml.jp_tv.dates import normalize_air_date
from work_catalog_yaml.persistence import FileWrite, commit_file_writes, directory_write_transaction, history_snapshot_name


@dataclass
class DateFilePlan:
    path: Path
    before: bytes
    after: bytes
    changes: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    records: int


def field(node: Any, key: str) -> Any:
    if isinstance(node, MappingNode):
        for name, value in node.value:
            if name.value == key:
                return value
    return None


def plan_file(path: Path) -> DateFilePlan:
    before = path.read_bytes()
    text = before.decode("utf-8")
    yaml = YAML(typ="safe")
    if any(isinstance(event, AliasEvent) for event in yaml.parse(text)):
        raise ValueError(f"存在 YAML 别名，无法保证仅修改日期字段，拒绝自动迁移：{path}")
    document = yaml.compose(text)
    works = document if isinstance(document, SequenceNode) else field(document, "works") or field(document, "entries")
    if not isinstance(works, SequenceNode):
        raise ValueError(f"作品列表格式无效：{path}")
    edits: dict[tuple[int, int], str] = {}
    changes: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for index, work in enumerate(works.value):
        attributes = field(work, "attributes")
        if not isinstance(attributes, SequenceNode):
            raise ValueError(f"作品缺少 attributes：{path.name}:{index}")
        name = ""
        for attribute in attributes.value:
            kind, data = field(attribute, "type"), field(attribute, "data")
            if kind and kind.value == "name" and isinstance(data, ScalarNode):
                name = data.value
        complete: dict[str, str] = {}
        for attribute in attributes.value:
            kind = field(attribute, "type")
            if not kind or kind.value != "date":
                continue
            for key in ("start", "end"):
                node = field(field(attribute, "data"), key)
                context = {"file": path.name, "index": index, "name": name, "field": key}
                if not isinstance(node, ScalarNode):
                    issues.append({**context, "reason": "missing-date-scalar"})
                    continue
                # Parse only this scalar, rejecting tagged/anchored/multiline
                # tokens rather than making edits with non-local effects.
                span = (node.start_mark.index, node.end_mark.index)
                token = text[span[0]:span[1]]
                if "\n" in token or token.lstrip().startswith(("&", "*", "!", "|", ">")):
                    issues.append({**context, "value": node.value, "reason": "nonlocal-or-multiline-scalar"})
                    continue
                value = yaml.load(token)
                try:
                    canonical = normalize_air_date(value, validate_calendar=True)
                except ValueError as error:
                    issues.append({**context, "value": str(value), "reason": str(error)})
                    continue
                if canonical and re.fullmatch(r"[0-9]{8}", canonical) and all(int(part) for part in (canonical[:4], canonical[4:6], canonical[6:8])):
                    complete[key] = canonical
                elif canonical:
                    issues.append({**context, "value": str(value), "reason": "unknown-date-component"})
                if not isinstance(value, str) or value != canonical:
                    edits[span] = "'" + canonical + "'"
                    changes.append({**context, "before": str(value), "after": canonical})
        if complete.get("start") and complete.get("end") and complete["end"] < complete["start"]:
            issues.append({"file": path.name, "index": index, "name": name,
                           "reason": "end-before-start", **complete})
    after_text = text
    for (start, end), replacement in sorted(edits.items(), reverse=True):
        after_text = after_text[:start] + replacement + after_text[end:]
    # Parse the result before scheduling any disk write.
    yaml.load(after_text)
    return DateFilePlan(path, before, after_text.encode("utf-8"), changes, issues, len(works.value))


def normalize_catalog(db: Path, *, history: Path, apply: bool = False) -> dict[str, Any]:
    def run() -> dict[str, Any]:
        paths = sorted(path for path in db.glob("*.yaml") if not path.name.startswith("."))
        if any(path.is_symlink() or not path.is_file() for path in paths):
            raise ValueError("数据库文件必须为普通文件，不能使用符号链接")
        plans = [plan_file(path) for path in paths]
        changed = [plan for plan in plans if plan.before != plan.after]
        backups: list[str] = []
        if apply:
            writes = []
            for plan in changed:
                backup = history / history_snapshot_name(plan.path)
                backups.append(str(backup))
                writes.append(FileWrite(plan.path, plan.after, plan.before, backup))
            commit_file_writes(writes)
        return {"applied": apply, "files_scanned": len(plans), "records_scanned": sum(p.records for p in plans),
                "files_changed": len(changed), "fields_changed": sum(len(p.changes) for p in changed),
                "changes": [change for plan in changed for change in plan.changes],
                "issues": [issue for plan in plans for issue in plan.issues], "backups": backups}
    db = db.resolve()
    if not db.is_dir():
        raise ValueError(f"数据库目录不存在：{db}")
    if apply:
        with directory_write_transaction(db):
            return run()
    return run()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="back up and apply the previewed format-only changes")
    args = parser.parse_args()
    settings = load_jp_tv_browse_settings()
    if settings.filesystem_root is None:
        parser.error("未配置数据库目录")
    from work_catalog_yaml.layout import ensure_feature_backend_paths
    ensure_feature_backend_paths()
    from collection_detail.save import history_catalog_root
    result = normalize_catalog(settings.filesystem_root, history=history_catalog_root(settings), apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
