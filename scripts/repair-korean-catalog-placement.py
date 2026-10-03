"""Preview misplaced Korean television records; apply only after explicit review.

Only top-level [JP][TVInfo]*.yaml records whose exact classification is
country=korea, domain=television, release_type=tv are moved. Broadcast date
values and all other record fields remain intact; only copied path wrappers
are cleaned. Existing KR records are never merged or overwritten silently.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/framework/backend"))

from ruamel.yaml import YAML
from ruamel.yaml.events import AliasEvent
from work_catalog_yaml.jp_tv.browse_settings import load_jp_tv_browse_settings
from work_catalog_yaml.jp_tv.dates import normalize_air_date
from work_catalog_yaml.layout import ensure_feature_backend_paths
from work_catalog_yaml.paths import normalize_copied_path
from work_catalog_yaml.persistence import FileWrite, commit_file_writes, history_snapshot_name

ensure_feature_backend_paths()
from collection_detail.save import catalog_relpath_for_new_work, catalog_write_transaction, history_catalog_root


_JP_FILE = re.compile(r"^\[JP\]\[TVInfo\].*\.yaml$", re.IGNORECASE)


@dataclass
class CatalogDocument:
    path: Path
    before: bytes | None
    document: Any
    works: list[Any]


def _digest(value: bytes | None) -> str | None:
    return hashlib.sha256(value).hexdigest() if value is not None else None


def _yaml() -> YAML:
    parser = YAML(typ="rt")
    parser.preserve_quotes = True
    parser.allow_unicode = True
    parser.width = 1_000_000
    return parser


def _works(document: Any, path: Path) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("works", "entries"):
            if isinstance(document.get(key), list):
                return document[key]
    raise ValueError(f"作品列表格式无效：{path}")


def _read_document(path: Path, *, allow_missing: bool = False) -> CatalogDocument:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError(f"数据库文件不能是符号链接或连接点：{path}")
    if path.exists() and not path.is_file():
        raise ValueError(f"数据库文件必须为普通文件：{path}")
    if not path.exists():
        if not allow_missing:
            raise ValueError(f"数据库文件不存在：{path}")
        document: Any = []
        return CatalogDocument(path, None, document, document)
    before = path.read_bytes()
    text = before.decode("utf-8")
    parser = _yaml()
    if any(isinstance(event, AliasEvent) for event in parser.parse(text)):
        raise ValueError(f"存在 YAML 别名，无法保证跨文件迁移独立性：{path}")
    document = parser.load(text)
    return CatalogDocument(path, before, document, _works(document, path))


def _attribute(work: Any, kind: str) -> Any:
    if not isinstance(work, dict) or not isinstance(work.get("attributes"), list):
        return None
    found = [item.get("data") for item in work["attributes"] if isinstance(item, dict) and item.get("type") == kind]
    if len(found) > 1:
        raise ValueError(f"作品含有重复 {kind} 属性，拒绝猜测")
    return found[0] if found else None


def _selected(work: Any) -> bool:
    if _attribute(work, "country") != "korea":
        return False
    collection = _attribute(work, "collection-type")
    return isinstance(collection, dict) and collection.get("domain") == "television" and collection.get("release_type") == "tv"


def _identity(work: Any) -> tuple[str, ...] | None:
    collection = _attribute(work, "collection-type")
    dates = _attribute(work, "date")
    name = _attribute(work, "name")
    if not isinstance(collection, dict) or not isinstance(name, str) or not name.strip():
        return None
    raw_start = dates.get("start") if isinstance(dates, dict) else ""
    try:
        start = normalize_air_date(raw_start)
    except ValueError:
        start = str(raw_start or "")
    # End-date and press changes do not create a different work identity. A
    # separate broadcast start remains distinct for split-season collections.
    return (str(_attribute(work, "country") or ""), str(collection.get("domain") or ""),
            str(collection.get("release_type") or ""), unicodedata.normalize("NFKC", name).strip().casefold(), start)


def _clean_record(work: Any) -> tuple[Any, list[dict[str, str]]]:
    result = copy.deepcopy(work)
    collection = _attribute(result, "collection-type")
    changes: list[dict[str, str]] = []

    def clean(mapping: Any, key: str, field: str) -> None:
        if not isinstance(mapping, dict) or not isinstance(mapping.get(key), str):
            return
        previous = mapping[key]
        normalized = normalize_copied_path(previous)
        if normalized != previous:
            # Do not normalize slash styles, internal whitespace, or dates.
            mapping[key] = normalized
            changes.append({"field": field, "before": str(previous), "after": normalized})

    def clean_presses(rows: Any, prefix: str) -> None:
        if isinstance(rows, list):
            for index, row in enumerate(rows):
                clean(row, "press_path", f"{prefix}[{index}].press_path")

    if isinstance(collection, dict):
        clean(collection, "path", "collection-type.path")
        clean_presses(collection.get("collectioned"), "collection-type.collectioned")
        continuations = collection.get("continuations")
        if isinstance(continuations, list):
            for index, continuation in enumerate(continuations):
                if isinstance(continuation, dict):
                    clean(continuation, "path", f"collection-type.continuations[{index}].path")
                    clean_presses(continuation.get("collectioned"), f"collection-type.continuations[{index}].collectioned")
    return result, changes


def _serialize(document: CatalogDocument) -> bytes:
    parser = _yaml()
    if document.before is not None and b"\r\n" in document.before:
        parser.line_break = "\r\n"
    stream = StringIO()
    parser.dump(document.document, stream)
    content = stream.getvalue().encode("utf-8")
    # Round-trip serialization must not change scalar values or drop unknown
    # attributes; in particular compact/ISO date tokens are not normalized.
    parsed = _yaml().load(content.decode("utf-8"))
    if parsed != document.document:
        raise ValueError(f"写回后的记录内容发生非预期变化：{document.path}")
    return content


def repair_catalog(db: Path, *, history: Path, apply: bool = False,
                   expect_plan_sha256: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    db = db.expanduser().resolve()
    history = history.expanduser().resolve()
    if not db.is_dir():
        raise ValueError(f"数据库目录不存在：{db}")
    if history == db or history.is_relative_to(db):
        raise ValueError("备份目录必须位于数据库目录之外")
    with catalog_write_transaction(db):
        sources = [_read_document(path) for path in sorted(db.iterdir()) if _JP_FILE.fullmatch(path.name)]
        documents = {document.path: document for document in sources}
        transfers: list[tuple[CatalogDocument, int, CatalogDocument, Any]] = []
        moves: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        pending_by_target: dict[Path, list[tuple[Any, str]]] = {}
        for source in sources:
            for index, work in enumerate(source.works):
                if not _selected(work):
                    continue
                name = _attribute(work, "name")
                if _identity(work) is None:
                    raise ValueError(f"待迁移作品缺少可核对的名称：{source.path.name}:{index}")
                dates = _attribute(work, "date")
                start = dates.get("start") if isinstance(dates, dict) else ""
                target_path = db / catalog_relpath_for_new_work("korea", start, now=now)
                if target_path.parent != db:
                    raise ValueError("迁移目标必须是数据库根目录内的文件")
                target = documents.get(target_path)
                if target is None:
                    target = _read_document(target_path, allow_missing=True)
                    documents[target_path] = target
                cleaned, path_changes = _clean_record(work)
                context = {"source": source.path.name, "source_index": index, "name": str(name), "target": target_path.name}
                for existing, existing_location in [
                    *((row, f"{target_path.name}:{position}") for position, row in enumerate(target.works)),
                    *pending_by_target.get(target_path, []),
                ]:
                    if existing == cleaned or _identity(existing) == _identity(cleaned):
                        conflicts.append({**context, "existing": existing_location,
                                          "reason": "相同内容已存在" if existing == cleaned else "同作品身份已有记录，不自动合并"})
                pending_by_target.setdefault(target_path, []).append((cleaned, f"{source.path.name}:{index}"))
                moves.append({**context, "date": copy.deepcopy(dates), "path_changes": path_changes})
                transfers.append((source, index, target, cleaned))

        result: dict[str, Any] = {
            "ok": not conflicts, "applied": False, "can_apply": not conflicts,
            "database": str(db), "history_directory": str(history),
            "source_files_scanned": len(sources), "records_to_move": len(moves),
            "path_fields_to_clean": sum(len(move["path_changes"]) for move in moves),
            "moves": moves, "conflicts": conflicts, "files": [], "backups": [],
        }
        if conflicts:
            return result
        removals: dict[Path, set[int]] = {}
        changed_paths: set[Path] = set()
        for source, index, target, record in transfers:
            removals.setdefault(source.path, set()).add(index)
            target.works.append(record)
            changed_paths.update((source.path, target.path))
        for source in sources:
            removed = removals.get(source.path, set())
            if removed:
                source.works[:] = [work for index, work in enumerate(source.works) if index not in removed]
        writes = []
        for path in sorted(changed_paths):
            document = documents[path]
            after = _serialize(document)
            result["files"].append({"file": path.name, "created": document.before is None,
                                    "before_sha256": _digest(document.before), "after_sha256": _digest(after),
                                    "records_after": len(document.works)})
            writes.append(FileWrite(path, after, document.before))
        plan_material = {"database": str(db), "moves": moves, "files": result["files"]}
        result["plan_sha256"] = hashlib.sha256(json.dumps(plan_material, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if expect_plan_sha256 and result["plan_sha256"] != expect_plan_sha256:
            raise ValueError("迁移计划与已确认的预览不一致，未备份或写入；请重新预览")
        if apply:
            # Validate all preimages under the shared application lock, before
            # creating recovery snapshots. commit_file_writes repeats its own
            # byte-for-byte CAS checks before every atomic replacement.
            for write in writes:
                current = _read_document(write.target, allow_missing=True).before
                if _digest(current) != _digest(write.previous):
                    raise ValueError(f"文件在计划后发生变化，未写入：{write.target}")
            committed = []
            for write in writes:
                backup = history / history_snapshot_name(write.target) if write.previous is not None else None
                if backup:
                    result["backups"].append(str(backup))
                committed.append(FileWrite(write.target, write.content, write.previous, backup))
            commit_file_writes(committed)
            result["applied"] = True
        return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="备份并应用迁移；默认只预览")
    parser.add_argument("--expect-plan-sha256", help="仅在当前计划与已审预览哈希完全一致时执行")
    args = parser.parse_args()
    settings = load_jp_tv_browse_settings()
    if settings.filesystem_root is None:
        parser.error("未配置 collection-detail 数据库目录")
    result = repair_catalog(settings.filesystem_root, history=history_catalog_root(settings), apply=args.apply,
                            expect_plan_sha256=args.expect_plan_sha256)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
