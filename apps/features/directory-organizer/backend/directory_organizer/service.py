"""One disk-first preview/execution workflow, reused by single and batch actions."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
import os
import threading
import time
import unicodedata
from uuid import uuid4

from collection_detail import catalog_edit_service as catalog
from collection_detail import shortcut_service as shortcuts
from collection_detail.catalog_repository import work_key
from work_catalog_yaml.layout import feature_data_root
from work_catalog_yaml.operation_progress import operation_context, report_exception, report_progress
from work_catalog_yaml.persistence import directory_write_transaction
from work_catalog_yaml.common.record_merge import merge_confirmed_changes
from work_catalog_yaml.input_validation import parse_record_index
from .execution import MediaExecution
from .snapshots import (absolute_path, child_directory, digest, existing_directory, nearest_existing,
                        path_key, relative_path, safe_target, scan_root, snapshot)
from .strategies import list_strategies, propose_layout

DB_ISSUE_CODES = frozenset({"new-record-confirmation", "name-required", "press-key-stale", "press-order-changed", "press-selection",
                          "unselected-press-target", "format-required", "binding-target-mismatch", "binding-invalid",
                          "record-required", "catalog-edit-invalid"})


def _attribute(record, kind, default=None):
    attributes = record.get("attributes", []) if isinstance(record, dict) else []
    if not isinstance(attributes, list):
        raise ValueError("作品 attributes 必须是数组")
    for item in attributes:
        if isinstance(item, dict) and item.get("type") == kind:
            return item.get("data", default)
    return default


def _collection(record):
    value = _attribute(record, "collection-type", {})
    return value if isinstance(value, dict) else {}


def _presses(record):
    collection = _collection(record)
    def rows(raw):
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValueError("压制记录 collectioned 必须是对象数组")
        return raw
    yield from rows(collection.get("collectioned", []))
    continuations = collection.get("continuations", [])
    if not isinstance(continuations, list) or any(not isinstance(item, dict) for item in continuations):
        raise ValueError("补充收集 continuations 必须是对象数组")
    for continuation in continuations:
        yield from rows(continuation.get("collectioned", []))


def _normal(value):
    return "".join(char for char in unicodedata.normalize("NFKC", str(value or "")).casefold() if char.isalnum())


def _issue(code, message, *, path="", blocking=True):
    return {"code": code, "message": message, "path": str(path), "blocking": blocking,
            "level": "error" if blocking else "warning"}


def _work_key(item):
    ref = item.get("ref") or {}
    if not ref:
        return ""
    if not isinstance(ref, dict) or not isinstance(ref.get("yaml_source_rel"), str):
        raise ValueError("作品引用须包含数据库来源路径和记录位置")
    return work_key(ref["yaml_source_rel"], parse_record_index(ref.get("index_in_file")))


def _binding_path(item, press):
    root = item.get("path") or _collection(item.get("record", {})).get("path", "")
    relative = press.get("press_path", "")
    if not root or not relative:
        return ""
    try:
        return path_key(catalog.resolve_record_directory(item["record"], relative_path(relative)))
    except ValueError:
        return ""


def _matched_presses(item, source, proposal):
    presses = item.get("press") or []
    exact = [press for press in presses if _binding_path(item, press) == path_key(source)]
    if exact:
        return exact
    format_name = _normal(proposal.get("press_format"))
    group = _normal(proposal.get("press_group"))
    matches = [press for press in presses if format_name and _normal(press.get("press_format")) == format_name and
               (not group or _normal(press.get("press_group")) == group)]
    if len(matches) == 1:
        return matches
    return []


def _new_record_identity(record):
    dates = _attribute(record, "date", {})
    collection = _collection(record)
    return tuple(_normal(value) for value in (
        _attribute(record, "name", ""), _attribute(record, "country", ""),
        collection.get("domain"), collection.get("release_type"),
        dates.get("start", "") if isinstance(dates, dict) else "",
        dates.get("end", "") if isinstance(dates, dict) else ""))


def _candidates(records, source, title):
    names = {_normal(title), _normal(source.name)} - {""}
    parent_name = _normal(source.parent.name)
    # A series root can contain TV, movies and sequels. Its name is only a
    # search hint when the child already identifies a different work.
    if not _normal(title) and parent_name:
        names.add(parent_name)
    ranked = []
    for item in records:
        name = _normal(item.get("name") or _attribute(item.get("record", {}), "name", ""))
        exact = any(_binding_path(item, press) == path_key(source) for press in item.get("press", []))
        score = 1000 if exact else 900 if name in names else 50 if name and (
            name == parent_name or any(name in value or value in name for value in names)) else 0
        if score:
            ranked.append((score, item))
    ranked.sort(key=lambda value: (-value[0], str(value[1].get("name", ""))))
    return ranked


def _template(title, proposal, root, release):
    return {"attributes": [
        {"type": "name", "data": title}, {"type": "country", "data": "japan"},
        {"type": "date", "data": {"start": "", "end": ""}},
        {"type": "collection-type", "data": {"domain": "animation", "release_type": "tv", "path": str(root),
            "markers": [], "collectioned": [{"press_format": proposal.get("press_format", ""),
                "press_group": proposal.get("press_group", ""), "press_path": release}]}},
    ]}


class PlanStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._plans = OrderedDict()

    def put(self, plan):
        with self._lock:
            for key, previous in list(self._plans.items()):
                if previous["source_path"] == plan["source_path"] and not previous.get("_result"):
                    del self._plans[key]
            self._plans[plan["id"]] = deepcopy(plan)
            while len(self._plans) > 64 or sum(len(value["files"]) for value in self._plans.values()) > 100000:
                self._plans.popitem(last=False)

    def get(self, key):
        with self._lock:
            value = self._plans.get(key)
            if value is None or time.monotonic() - value["_created"] > 3600:
                raise ValueError("预览已过期或已被新预览替代，请重新预览该子目录")
            return deepcopy(value)

    def finish(self, key, result):
        with self._lock:
            if key in self._plans:
                self._plans[key]["_result"] = deepcopy(result)


class OrganizerService:
    def __init__(self, *, journal_root=None):
        self.plans = PlanStore()
        self.journal_root = Path(journal_root) if journal_root else feature_data_root("directory-organizer") / "history"
        self._execute_lock = threading.RLock()

    def scan(self, body, *, settings):
        result = scan_root(body.get("root"))
        result["strategies"] = list_strategies()
        return result

    def search_catalog(self, body, *, settings):
        query = _normal(body.get("query", ""))
        limit = body.get("limit", 50)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("查找数量必须为 1–100")
        records = catalog.catalog_records(settings)
        matches = [item for item in records if not query or query in _normal(item.get("name")) or query in _normal(item.get("path"))]
        return {"records": matches[:limit], "total": len(matches), "truncated": len(matches) > limit}

    def preview(self, body, *, settings):
        root = existing_directory(body.get("root"))
        source = child_directory(root, body.get("child"))
        report_progress("读取所选子目录", context={"stage": "目录预览", "source_path": str(source)})
        facts = snapshot(source)
        draft_given = body.get("draft")
        if draft_given is not None and not isinstance(draft_given, dict):
            raise ValueError("编辑草稿必须是对象")
        draft = deepcopy(draft_given or {})
        if "completed" in draft and not isinstance(draft["completed"], bool):
            raise ValueError("已整理完成标记必须是布尔值")
        file_targets = draft.get("file_targets") or {}
        if not isinstance(file_targets, dict):
            raise ValueError("文件目标修改必须是 source_rel → target_rel 对象")
        strategy = str(body.get("strategy") or "auto")
        proposal = propose_layout(source.name, facts["files"], strategy_id=strategy, overrides={"targets": file_targets})
        records = catalog.catalog_records(settings)
        by_key = {_work_key(item): item for item in records}
        ranked = _candidates(records, source, proposal.get("title"))
        exact = [item for score, item in ranked if score == 1000]
        named = [item for score, item in ranked if score == 900]
        inferred = exact or (named if len(named) == 1 else [])
        suggested_release = proposal.get("release_name") or source.name
        suggested_root = str(root)
        if len(inferred) == 1:
            matched = _matched_presses(inferred[0], source, proposal)
            if len(matched) == 1 and inferred[0].get("path") and matched[0].get("press_path"):
                try:
                    suggested_root = str(catalog.resolve_record_directory(inferred[0]["record"]))
                    suggested_release = matched[0]["press_path"]
                except ValueError:
                    pass  # Keep a usable disk-first draft for repairing invalid old bindings.
        completed = bool(draft.get("completed", False))
        draft.setdefault("completed", False)
        draft.setdefault("work_root", suggested_root)
        draft.setdefault("release_name", suggested_release)
        if "records" not in draft:
            draft["records"] = []
            for item in inferred:
                record = deepcopy(item["record"])
                selected = _matched_presses(item, source, proposal)
                selected_keys = [press["press_key"] for press in selected]
                if selected_keys:
                    _collection(record)["path"] = draft["work_root"]
                selected_positions = {int(key.split(":", 1)[0]) for key in selected_keys}
                for position, press in enumerate(_presses(record)):
                    if position in selected_positions:
                        press["press_path"] = draft["release_name"]
                draft["records"].append({"ref": deepcopy(item["ref"]), "record": record, "press_keys": selected_keys, "confirmed": True})
            if not draft["records"]:
                draft["records"] = [{"ref": None, "record": _template(proposal.get("title", ""), proposal, draft["work_root"], draft["release_name"]),
                                     "press_keys": [], "confirmed": False}]
        if not isinstance(draft["records"], list) or len(draft["records"]) > 100:
            raise ValueError("一个子目录关联的作品记录必须为数组且不超过 100 项")
        work_root = safe_target(absolute_path(draft["work_root"]))
        release = relative_path(draft["release_name"])
        target = work_root / release
        safe_target(target)
        issues = deepcopy(facts["issues"])
        if source.stat().st_dev != nearest_existing(target).stat().st_dev:
            issues.append(_issue("cross-volume", "当前只支持同卷安全移动；跨盘整理请先选择同卷目标目录", path=target))
        if target in source.parents:
            issues.append(_issue("target-ancestor", "目标压制目录不能是来源目录的上级", path=target))
        if completed and path_key(target) != path_key(source):
            issues.append(_issue("completed-path", "标记已整理完成时不移动媒体；目标必须保持当前子目录", path=target))
        selected = draft["records"]
        for item in selected:
            if not isinstance(item, dict) or not isinstance(item.get("record"), dict):
                raise ValueError("作品草稿须包含完整 record 对象")
            if item.get("ref") is None and item.get("confirmed") is not True:
                issues.append(_issue("new-record-confirmation", "未匹配已有 DB；请核对全部作品信息并勾选确认新增（默认国家/品类只是待确认模板）"))
            if not _attribute(item["record"], "name", ""):
                issues.append(_issue("name-required", "请填写数据库作品名"))
            presses = list(_presses(item["record"]))
            press_keys = item.get("press_keys") or []
            if not isinstance(press_keys, list):
                raise ValueError("press_keys 必须是数组")
            positions = set()
            original = by_key.get(_work_key(item)) if item.get("ref") else None
            original_presses = list(_presses(original["record"])) if original else []
            original_keys = {press["press_key"] for press in original.get("press", [])} if original else set()
            for key in press_keys:
                try:
                    position = int(str(key).split(":", 1)[0])
                except ValueError:
                    raise ValueError("关联压制记录键无效") from None
                if position < 0 or position >= len(presses):
                    raise ValueError("关联压制记录位置已变化，请重新选择")
                if str(key) != f"{position}:manual" and str(key) not in original_keys:
                    issues.append(_issue("press-key-stale", "压制记录标识已变化，请重新选择当前子目录关联的压制记录"))
                if str(key) != f"{position}:manual" and position < len(original_presses):
                    if presses[position] != original_presses[position] and any(
                        presses[position] == previous for index, previous in enumerate(original_presses) if index != position
                    ):
                        issues.append(_issue("press-order-changed", "压制记录顺序已调整，请重新手动选择关联行"))
                positions.add(position)
            if not positions and len(presses) == 1 and item.get("ref") is None:
                positions = {0}
            if not positions:
                issues.append(_issue("press-selection", "请选择当前子目录对应的压制记录；不会自动修改其他压制版本"))
            for index, previous in enumerate(original_presses):
                if index in positions:
                    continue
                before_path = _binding_path(original, previous)
                after_path = _binding_path({"record": item["record"]}, presses[index]) if index < len(presses) else ""
                if before_path != after_path:
                    issues.append(_issue("unselected-press-target", "作品根目录或压制行修改影响了未选中的其他压制记录；请保持其原有实际目标，或在收集列表单独修改", path=before_path))
            for position in positions:
                press = presses[position]
                if not str(press.get("press_format", "")).strip():
                    issues.append(_issue("format-required", "请填写压制格式；压制组允许为空"))
                try:
                    binding = catalog.resolve_record_directory(item["record"], relative_path(press.get("press_path", "")))
                    if path_key(binding) != path_key(target):
                        issues.append(_issue("binding-target-mismatch", "DB 作品根目录 / 所选压制目录与整理目标不同，请手动统一后重新预览", path=binding))
                except ValueError as exc:
                    issues.append(_issue("binding-invalid", str(exc)))
            item["_press_positions"] = sorted(positions)
        if selected:
            positions = selected[0].get("_press_positions") or []
            presses = list(_presses(selected[0]["record"]))
            if positions:
                chosen = presses[positions[0]]
                proposal = propose_layout(source.name, facts["files"], strategy_id=strategy, overrides={
                    "title": _attribute(selected[0]["record"], "name", ""),
                    "press_format": chosen.get("press_format", ""), "press_group": chosen.get("press_group", ""),
                    "release_name": draft["release_name"], "targets": file_targets})
        if not selected:
            issues.append(_issue("record-required", "请关联至少一条作品 DB 记录，或补齐新作品信息"))
        # DB-confirmed metadata can resolve inference warnings, but unresolved
        # file destinations must remain blocking until explicitly assigned.
        for warning in proposal.get("issues", []):
            if warning.get("code") in {"title-unresolved", "format-unresolved", "work-multiple"} or (
                completed and warning.get("code") in {
                    "existing-category-ambiguous", "existing-episode-ambiguous",
                    "subtitle-video-unresolved", "subtitle-video-ambiguous", "audio-video-ambiguous",
                    "target-conflict",
                }
            ):
                issues.append({**warning, "blocking": False, "level": "warning"})
            else:
                issues.append(warning)
        known_files = {item["relative_path"]: item for item in facts["files"]}
        if set(file_targets) - known_files.keys():
            issues.append(_issue("stale-file-edit", "手动文件目标中含已不存在的来源文件，请重新核对"))
        files, moves, destinations = [], [], set()
        for item in proposal["files"]:
            source_rel = item["source_rel"]
            if source_rel not in known_files:
                raise ValueError("整理策略返回了未扫描的文件")
            target_rel = relative_path(source_rel if completed else file_targets.get(source_rel, item["target_rel"]))
            destination = target / target_rel
            original = source / source_rel
            safe_target(destination.parent)
            identity = path_key(destination)
            if identity in destinations:
                issues.append(_issue("target-collision", "多个文件计划进入同一个目标位置", path=destination))
            destinations.add(identity)
            changed = path_key(original) != identity
            if changed and os.path.lexists(destination):
                issues.append(_issue("target-exists", "目标文件已经存在；不会覆盖、合并或删除它", path=destination))
            files.append({**item, "target_rel": target_rel, "changed": changed, "target_path": str(destination), "size": known_files[source_rel]["size"]})
            if changed:
                moves.append({"source": str(original), "target": str(destination), "signature": known_files[source_rel]["signature"]})
        if len(files) != len(known_files) or len({item["source_rel"] for item in files}) != len(known_files):
            raise ValueError("整理策略未完整覆盖全部来源文件，拒绝生成计划")
        if "completed" not in (draft_given or {}) and not moves and path_key(target) == path_key(source) and not any(
            issue.get("blocking", issue.get("level") == "error") and issue.get("code") not in DB_ISSUE_CODES for issue in issues
        ):
            draft["completed"] = True
        edits = [{"ref": item.get("ref"), "record": item["record"]} for item in selected]
        db_preview = catalog.preview_catalog_edits(edits, settings=settings)
        issues.extend(db_preview.get("issues", []))
        blocking = any(item.get("blocking", item.get("level") == "error") for item in issues)
        changes = [item for item in db_preview.get("changes", []) if item.get("action") != "unchanged"]
        blocking_issues = [item for item in issues if item.get("blocking", item.get("level") == "error")]
        database_status = "unresolved" if any(item.get("code") in DB_ISSUE_CODES for item in blocking_issues) else "needs_sync" if changes else "matched"
        media_status = "unresolved" if any(item.get("code") not in DB_ISSUE_CODES for item in blocking_issues) else "complete" if completed or not moves else "needs_work"
        plan = {"id": str(uuid4()), "root": str(root), "child": source.name, "source_path": str(source), "target_path": str(target),
            "status": "unresolved" if blocking else "needs_work" if moves or changes else "complete",
            "media_status": media_status, "database_status": database_status, "strategy_id": proposal["strategy_id"],
            "strategy_reason": proposal["strategy_reason"], "layout_assessment": deepcopy(proposal.get("layout_assessment", {})),
            "counts": {"files": len(files), "moves": len(moves), "unchanged": len(files) - len(moves),
                       "db_records": len(selected), "db_changes": len(changes),
                       "blocking_issues": sum(bool(item.get("blocking", item.get("level") == "error")) for item in issues)},
            "draft": draft, "files": files,
            "source_directories": list(facts["directories"]), "source_structure_complete": not bool(facts["issues"]),
            "catalog_candidates": [item for _, item in ranked[:50]], "issues": issues, "db_changes": changes,
            "shortcut_preview": {"pending_after_save": True, "message": "整理和 DB 保存后，按关联记录预检具体快捷方式；再次确认后生成，不影响其他记录"},
            "can_execute": not blocking, "summary": f"{len(files)} 个文件，计划移动 {len(moves)} 个，关联 {len(selected)} 条 DB 记录",
            "_snapshot": facts, "_moves": moves, "_edits": db_preview.get("edits", edits), "_created": time.monotonic(),
            "_catalog_root": str(settings.filesystem_root)}
        associated_keys = {_work_key(item) for item in selected if item.get("ref")}
        plan["_record_before"] = {_work_key(item): deepcopy(item["record"]) for item in records if _work_key(item) in associated_keys}
        self.plans.put(plan)
        return {key: deepcopy(value) for key, value in plan.items() if not key.startswith("_")}

    def _execute_one(self, plan, *, settings, generate_shortcuts):
        if plan.get("_result"):
            return {**plan["_result"], "replayed": True}
        if not plan["can_execute"] or plan["_catalog_root"] != str(settings.filesystem_root):
            raise ValueError("预览包含未决问题或数据库配置已变化，不能执行")
        edits = catalog.preview_catalog_edits(plan["_edits"], settings=settings)
        if any(item.get("blocking", True) for item in edits.get("issues", [])):
            raise ValueError("DB 校验已变化：" + "；".join(item.get("message", "") for item in edits["issues"]))
        for edit, draft in zip(edits.get("edits", []), plan["draft"]["records"]):
            presses = list(_presses(edit["record"]))
            for position in draft["_press_positions"]:
                if _binding_path({"record": edit["record"]}, presses[position]) != path_key(Path(plan["target_path"])):
                    raise ValueError("DB 目录解析配置已变化，目标不再与确认的计划一致，请重新预览")
        media = MediaExecution(plan, self.journal_root)
        db_committed = False
        db_uncertain = False
        try:
            media.run()
            report_progress("保存已确认的作品信息", context={"stage": "同步作品DB", "source_path": plan["source_path"]})
            saved = catalog.apply_catalog_edits(edits.get("edits", plan["_edits"]), settings=settings)
            db_committed = bool(saved.get("db_committed", True))
            warnings = media.complete(saved)
            warnings.extend(saved.get("issues", []))
            result = {"plan_id": plan["id"], "child": plan["child"], "status": "warning" if warnings else "succeeded",
                      "message": "媒体整理与 DB 同步完成", "moved": len(media.moved), "database": saved,
                      "issues": warnings, "receipt_path": str(media.journal), "shortcut_refs": []}
            for record in saved.get("records", []):
                for press in record.get("press", []):
                    if _binding_path(record, press) == path_key(Path(plan["target_path"])):
                        result["shortcut_refs"].append({"work_key": _work_key(record), "press_key": press["press_key"]})
            if generate_shortcuts and result["shortcut_refs"]:
                try:
                    result["shortcut_preview"] = shortcuts.preview_shortcuts(result["shortcut_refs"], settings=settings)
                    result["message"] += "；快捷方式已预检，等待单独确认生成"
                except Exception as exc:
                    report_exception(exc, context={"stage": "快捷方式预检", "source_path": plan["source_path"]})
                    result["status"] = "warning"
                    result["issues"].append(_issue("shortcut-preview-failed", str(exc), blocking=False))
            return result
        except Exception as exc:
            if hasattr(exc, "db_committed") and exc.db_committed is not False:
                db_uncertain = exc.db_committed is None
                db_committed = True  # Unknown/partial DB rollback: never undo media blindly.
            report_exception(exc, context={"stage": "执行目录整理", "source_path": plan["source_path"], "target_path": plan["target_path"]})
            rollback_errors = [] if db_committed else media.rollback()
            return {"plan_id": plan["id"], "child": plan["child"], "status": "warning" if db_committed else "failed",
                    "message": str(exc) + ("；DB 写入状态无法确认，未自动回滚媒体，请核对处理详情和收据" if db_uncertain else
                        "；DB 已保存，未回滚媒体，请核对处理详情" if db_committed else "；已尝试回滚本目录的媒体变更"),
                    "db_committed": db_committed, "issues": rollback_errors, "receipt_path": str(media.journal)}

    def execute(self, body, *, settings):
        ids = body.get("plan_ids")
        if body.get("confirm") is not True or not isinstance(ids, list) or not 1 <= len(ids) <= 64 or not all(isinstance(key, str) for key in ids) or len(set(ids)) != len(ids):
            raise ValueError("必须明确确认 1–64 个互不重复的已预览目录")
        if settings.filesystem_root is None:
            raise ValueError("未配置作品数据库目录")
        with self._execute_lock, directory_write_transaction(settings.filesystem_root):
            plans = [self.plans.get(key) for key in ids]
            prospective = {}
            new_identities = set()
            # Validate every original DB version and overlapping intended edits
            # before the first directory is touched.
            for plan in plans:
                if plan.get("_result"):
                    continue
                if not plan["can_execute"] or plan["_catalog_root"] != str(settings.filesystem_root):
                    raise ValueError("批次包含未决目录或数据库配置已变化，请重新预览：" + plan["child"])
                checked = catalog.preview_catalog_edits(plan["_edits"], settings=settings)
                if any(item.get("blocking", True) for item in checked.get("issues", [])):
                    raise ValueError("批次中的 DB 预览已失效，请重新预览：" + plan["child"])
                for edit in plan["_edits"]:
                    if not edit.get("ref"):
                        identity = _new_record_identity(edit["record"])
                        if identity in new_identities:
                            raise ValueError("批次中多个目录准备新增同一作品；请先完成其中一个目录，再为其他目录关联已新增的作品记录")
                        new_identities.add(identity)
                        continue
                    key = _work_key(edit)
                    base = plan["_record_before"][key]
                    prospective[key] = merge_confirmed_changes(base, edit["record"], prospective.get(key, base), location=key)
            targets = set()
            for plan in plans:
                if plan.get("_result"):
                    continue
                target_root = Path(plan["target_path"])
                for other in plans:
                    if other["id"] == plan["id"] or other.get("_result"):
                        continue
                    other_source = Path(other["source_path"])
                    if target_root == other_source or other_source in target_root.parents or target_root in other_source.parents:
                        raise ValueError("批次中的整理目标与另一来源目录重叠，请分开处理或调整目标")
                for move in plan["_moves"]:
                    key = path_key(Path(move["target"]))
                    if key in targets:
                        raise ValueError("批次内多个目录写入同一文件，请分别调整目标并重新预览")
                    targets.add(key)
            results = []
            updated_records = {}
            stopped = False
            for index, plan in enumerate(plans):
                if stopped:
                    results.append({"plan_id": plan["id"], "child": plan["child"], "status": "skipped", "message": "前一目录失败，批次暂停；此目录尚未执行"})
                    continue
                report_progress("处理所选子目录", completed=index, total=len(plans), unit="目录", context={"object": plan["child"], "stage": "批量顺序处理"})
                try:
                    for edit in plan["_edits"]:
                        key = _work_key(edit)
                        if edit.get("ref") and key in updated_records:
                            latest = updated_records[key]
                            edit["record"] = merge_confirmed_changes(plan["_record_before"][key], edit["record"], latest["record"], location=key)
                            edit["ref"] = deepcopy(latest["ref"])
                    with operation_context(object=plan["child"], source_path=plan["source_path"]):
                        result = self._execute_one(plan, settings=settings, generate_shortcuts=body.get("generate_shortcuts") is True)
                except Exception as exc:
                    report_exception(exc, context={"stage": "执行前校验", "source_path": plan["source_path"]})
                    result = {"plan_id": plan["id"], "child": plan["child"], "status": "failed", "message": str(exc)}
                self.plans.finish(plan["id"], result)
                results.append(result)
                for record in result.get("database", {}).get("records", []):
                    updated_records[_work_key(record)] = record
                stopped = result["status"] == "failed" or result.get("db_committed") is True
            failures = [row for row in results if row["status"] in {"failed", "warning", "skipped"}]
            return {"results": results, "partial": bool(failures), "failed_count": sum(row["status"] == "failed" for row in results),
                    "issues": [{"message": row["message"], "object": row["child"], "level": "error" if row["status"] == "failed" else "warning"} for row in failures]}

    def shortcut_action(self, body, *, settings):
        refs = body.get("refs")
        if body.get("preview") is True:
            return {"shortcut_preview": shortcuts.preview_shortcuts(refs, settings=settings)}
        if body.get("confirm") is not True:
            raise ValueError("生成快捷方式必须再次明确确认具体预览")
        return shortcuts.apply_shortcuts(refs, str(body.get("plan_id") or ""), settings=settings)
