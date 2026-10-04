"""Bounded, explicit result projection safe for durable operation diagnostics.

Never traverse arbitrary data/rows/catalog/request payloads. Only operational
summaries and named diagnostic arrays belong in logs, not business databases.
"""
from __future__ import annotations

from typing import Any
from pathlib import PurePath
import json
import math

_CONTAINERS = frozenset({"summary", "file_generation", "execution", "result", "scan", "validation",
                       "plan_summary", "disk_summary", "mapping_summary", "index_db"})
_COUNTERS = frozenset({
    "created", "updated", "deleted", "removed", "written", "saved", "total", "count",
    "planned", "existing", "skipped", "failed_count", "error_count", "warning_count",
    "skipped_empty_target", "skipped_missing_target", "file_count", "directory_count",
    "work_count", "press_count", "shortcut_count", "unmapped_shortcuts_count",
    "disk_only_shortcut_count", "unmapped_shortcut_count", "matched", "missing",
    "ok_count", "checked", "scanned", "unchanged", "elapsed_ms", "duration_ms",
    "created_count", "updated_count", "skipped_existing", "scanned_shortcut_count", "matched_shortcut_count",
    "planned_shortcut_count", "db_work_count", "db_press_count", "link_count", "node_count",
    "total_press", "mapped_press", "unconfigured_press", "unconfigured_work_path", "unconfigured_press_path",
    "ready", "missing_target", "missing_catalog_binding", "target_fixable", "duplicate_shortcut", "invalid_path",
    "shortcut_exists", "target_mismatch", "shortcut_target_unknown", "unmapped_on_disk", "empty_target_path", "renamed", "failed", "shortcut_leaves",
})
_DETAILS = {"errors": "error", "failed": "error", "issues": "warning", "warnings": "warning",
            "unmapped_shortcuts": "warning", "writes": "info", "details": "info"}
_LABELS = {
    "created": "已创建", "created_count": "已创建", "updated": "已更新", "updated_count": "已更新",
    "deleted": "已删除", "removed": "已移除", "written": "已写入", "saved": "已保存",
    "total": "总数", "count": "数量", "planned": "计划数", "existing": "已存在", "skipped": "已跳过",
    "failed_count": "失败数", "error_count": "错误数", "warning_count": "警告数",
    "skipped_empty_target": "跳过空目标", "skipped_missing_target": "跳过不存在目标", "skipped_existing": "跳过已存在",
    "file_count": "文件数", "directory_count": "目录数", "work_count": "作品数", "press_count": "压制记录数",
    "shortcut_count": "快捷方式数", "unmapped_shortcuts_count": "未关联快捷方式", "unmapped_shortcut_count": "未关联快捷方式",
    "disk_only_shortcut_count": "仅磁盘快捷方式", "matched": "已匹配", "missing": "缺失",
    "ok_count": "成功数", "checked": "已检查", "scanned": "已扫描", "unchanged": "未变化",
    "elapsed_ms": "耗时（毫秒）", "duration_ms": "耗时（毫秒）", "scanned_shortcut_count": "已扫描快捷方式",
    "matched_shortcut_count": "已匹配快捷方式", "planned_shortcut_count": "计划快捷方式", "db_work_count": "数据库作品数",
    "db_press_count": "数据库压制记录数", "link_count": "链接数", "node_count": "节点数",
    "total_press": "压制记录总数", "mapped_press": "已绑定压制记录", "unconfigured_press": "未绑定压制记录",
    "unconfigured_work_path": "缺少作品目录", "unconfigured_press_path": "缺少压制目录", "ready": "可处理",
    "missing_target": "目标不存在", "missing_catalog_binding": "缺少数据库绑定", "target_fixable": "可修复目标",
    "duplicate_shortcut": "重复快捷方式", "invalid_path": "无效路径", "shortcut_exists": "已存在快捷方式",
    "target_mismatch": "目标不一致", "shortcut_target_unknown": "目标未知", "unmapped_on_disk": "未关联快捷方式",
    "empty_target_path": "空目标路径", "renamed": "已重命名", "failed": "失败数", "shortcut_leaves": "磁盘快捷方式",
}
_PREFIXES = {"summary": "汇总", "file_generation": "文件生成", "execution": "执行", "result": "结果", "scan": "扫描",
             "validation": "校验", "plan_summary": "计划", "disk_summary": "磁盘", "mapping_summary": "关联", "index_db": "索引缓存"}


def _text(value: Any, limit: int = 2048) -> str:
    return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""


_DIAGNOSTIC_FIELDS = frozenset({
    "stage", "phase", "action", "object", "object_type", "name", "reason", "code", "status",
    "path", "source_path", "target_path", "shortcut_path", "link_path", "target_error", "target_exists",
    "work_key", "press_key", "press_path", "yaml_source_rel", "index_in_file", "record_index", "press_index", "operation_count",
    "expected", "actual", "error_type", "location", "root_path", "relpath", "history_file", "history_path", "actual_target_path",
    "receipt_path", "moved", "db_committed", "db_record_count", "db_write_count",
})
_OBJECT_FIELDS = frozenset({"object", "object_type", "name", "path", "source_path", "target_path", "shortcut_path",
                            "link_path", "work_key", "press_key", "press_path", "yaml_source_rel", "index_in_file",
                            "record_index", "press_index", "root_path", "relpath", "history_file", "history_path", "actual_target_path"})


def diagnostic_context(value: Any) -> dict[str, Any]:
    """Explicit scalar diagnostics only; never copy body, tokens or DB rows."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in sorted(_DIAGNOSTIC_FIELDS.intersection(value)):
        item = value[key]
        if key == "location" and isinstance(item, dict):
            location = {name: item[name] for name in ("file", "function")
                        if isinstance(item.get(name), str) and item[name]}
            location = {name: text[:1024] for name, text in location.items()}
            if isinstance(item.get("line"), int) and not isinstance(item["line"], bool) and item["line"] > 0:
                location["line"] = item["line"]
            if location:
                result[key] = location
        elif isinstance(item, (str, PurePath)):
            text = str(item).strip()[:2048]
            if text:
                result[key] = text
        elif isinstance(item, (bool, int)) or isinstance(item, float) and math.isfinite(item):
            result[key] = item
    if "phase" in result:
        result.setdefault("stage", result.pop("phase"))
    return result


def _diagnostic(value: Any, level: str, *, context: dict[str, Any], kind: str = "") -> dict[str, Any] | None:
    if isinstance(value, str):
        value = {"message": value}
    if not isinstance(value, dict):
        return None
    explicit = {**diagnostic_context(value.get("context")), **diagnostic_context(value)}
    # A batch diagnostic identifying its own object must not inherit the last
    # iterated object's paths/DB row from the operation's latest progress event.
    inherited = {key: item for key, item in context.items() if key in {"stage", "action"}} if _OBJECT_FIELDS.intersection(explicit) else context
    item = {**inherited, **explicit}
    item["level"] = value.get("level") if value.get("level") in {"info", "warning", "error"} else level
    item["message"] = _text(value.get("message") or value.get("error") or value.get("reason") or value.get("status"))
    if not item.get("path"):
        item["path"] = next((_text(value.get(alias), 1024) for alias in ("shortcut_path", "link_path", "file", "source_path")
                             if _text(value.get(alias), 1024)), "")
        if not item["path"]:
            item.pop("path")
    if not item.get("code") and _text(value.get("type"), 128):
        item["code"] = _text(value["type"], 128)
    item["message"] = item["message"] or ("未关联数据库的快捷方式" if kind == "unmapped_shortcuts"
                                              else _text(item.get("reason") or item.get("object") or item.get("name") or item.get("path")))
    # Keep the established readable shortcut summary, while preserving each
    # original field separately for detailed displays and code diagnostics.
    if kind == "unmapped_shortcuts":
        targets = [f"{label}：{_text(item[field], 1024)}" for field, label in (("target_path", "目标"), ("target_error", "目标错误"))
                   if item.get(field)]
        if isinstance(item.get("target_exists"), bool):
            targets.append("目标存在" if item["target_exists"] else "目标不存在")
        if targets:
            item["message"] += "；" + "；".join(targets)
    return item if item["message"] else None


def iter_result_details(payload: Any, *, depth: int = 0, context: dict[str, Any] | None = None):
    """Stream every allowed diagnostic, while leaving arbitrary data untouched."""
    if not isinstance(payload, dict) or depth > 3:
        return
    scope = {**diagnostic_context(context), **diagnostic_context(payload.get("context"))}
    if payload.get("error"):
        item = _diagnostic(payload, "error", context=scope)
        if item is not None:
            yield item
    for key, level in _DETAILS.items():
        entries = payload.get(key)
        if not isinstance(entries, (list, tuple)):
            continue
        for value in entries:
            item = _diagnostic(value, level, context=scope, kind=key)
            if item is not None:
                yield item
    for key in sorted(_CONTAINERS.intersection(payload)):
        yield from iter_result_details(payload[key], depth=depth + 1, context=scope)
    for value in _organizer_details(payload):
        item = _diagnostic(value, "info", context=scope)
        if item is not None:
            yield item


def _organizer_details(payload: dict[str, Any]):
    """Project known organizer envelopes, never recurse into records or drafts.

    The UI is capped separately. Durable diagnostics also have explicit input
    limits, with an omission notice rather than silently dropping extra rows.
    """
    def number(value):
        return ((isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float) and math.isfinite(value)) and value >= 0

    def rows(entries, scope, *, level="warning", title="处理明细"):
        if not isinstance(entries, (list, tuple)):
            return
        for entry in entries[:200]:
            item = _diagnostic(entry, level, context=scope)
            if item is not None:
                # Explicit child identity prevents inheriting a different batch
                # child's last progress context in the outer projection.
                yield {**scope, **item}
        if len(entries) > 200:
            yield {**scope, "level": "warning", "message": f"{title}过多，摘要未展开其余 {len(entries) - 200} 项"}

    def shortcut(value, scope):
        if not isinstance(value, dict):
            return
        scope = {**scope, "stage": "快捷方式结果" if "created" in value else "快捷方式预检"}
        labels = {"total": "总计", "creatable": "可创建", "already_exists_count": "已存在",
                  "conflict_count": "冲突", "created": "已创建", "failed_count": "失败",
                  "skipped_empty_target": "空目标跳过", "skipped_missing_target": "目标不存在跳过",
                  "skipped_unbound_count": "未绑定跳过", "skipped_unsafe_count": "不安全目标跳过"}
        counts = [f"{label} {value[key]}" for key, label in labels.items() if number(value.get(key))]
        if counts:
            yield {**scope, "message": "快捷方式：" + "；".join(counts)}
        for key in ("issues", "conflicts", "failed"):
            yield from rows(value.get(key), scope, level="error" if key == "failed" else "warning", title="快捷方式问题")
        # items describe the preflight plan even after execution. Do not claim
        # that a planned item was created solely from the aggregate count.
        entries = value.get("items")
        if isinstance(entries, (list, tuple)):
            safe = []
            for row in entries[:200]:
                if not isinstance(row, dict):
                    continue
                item = diagnostic_context(row)
                if row.get("actual_target"):
                    item["actual_target_path"] = _text(row["actual_target"])
                item["message"] = "快捷方式预检条目：" + _text(row.get("status") or "待核对")
                safe.append(item)
            yield from rows(safe, scope, level="info")
            if len(entries) > 200:
                yield {**scope, "level": "warning", "message": f"快捷方式条目过多，摘要未展开其余 {len(entries) - 200} 项"}

    plan = payload.get("plan")
    if isinstance(plan, dict) and isinstance(plan.get("child"), str) and isinstance(plan.get("counts"), dict):
        scope = {**diagnostic_context(plan), "object": _text(plan["child"]), "stage": "目录整理预览"}
        counts = plan["counts"]
        labels = {"files": "文件", "moves": "待移动", "db_records": "关联作品", "db_changes": "DB 变更", "blocking_issues": "阻断问题"}
        parts = [f"{label} {counts[key]}" for key, label in labels.items() if number(counts.get(key))]
        yield {**scope, "message": "目录预览：" + ("可执行" if plan.get("can_execute") is True else "待核对") + ("；" + "；".join(parts) if parts else "")}
        yield from rows(plan.get("issues"), scope)
    results = payload.get("results")
    if isinstance(results, (list, tuple)):
        for row in results[:64]:
            if not isinstance(row, dict) or not isinstance(row.get("child"), str) or not isinstance(row.get("plan_id"), str) or not isinstance(row.get("status"), str) or row["status"] not in {"succeeded", "warning", "failed", "skipped"}:
                continue
            scope = {**diagnostic_context(row), "object": _text(row["child"]), "stage": "目录整理结果"}
            # On an exception the outer flag also means "DB state uncertain,
            # preserve media". Only a successful writer receipt proves commit.
            scope.pop("db_committed", None)
            database = row.get("database")
            parts = [_text(row.get("message") or row["status"])]
            if number(row.get("moved")):
                parts.append(f"已移动 {row['moved']} 个文件")
            if isinstance(database, dict):
                if isinstance(database.get("db_committed"), bool):
                    scope["db_committed"] = database["db_committed"]
                    parts.append("DB 已提交" if database["db_committed"] else "DB 未提交")
                for key, field, label in (("records", "db_record_count", "返回作品记录"), ("writes", "db_write_count", "写入数据库文件")):
                    if isinstance(database.get(key), (list, tuple)):
                        scope[field] = len(database[key])
                        parts.append(f"{label} {scope[field]} 项")
            yield {**scope, "message": "；".join(parts), "level": "error" if row["status"] == "failed" else "warning" if row["status"] in {"warning", "skipped"} else "info"}
            yield from rows(row.get("issues"), scope)
            if isinstance(database, dict):
                db_scope = {**scope, "stage": "作品 DB 保存"}
                yield from rows(database.get("issues"), db_scope)
                yield from rows(database.get("writes"), db_scope, level="info", title="数据库写入明细")
            yield from shortcut(row.get("shortcut_preview"), scope)
            yield from shortcut(row.get("shortcut_result"), scope)
        if len(results) > 64:
            yield {"stage": "目录整理结果", "level": "warning", "message": f"批次结果过多，摘要未展开其余 {len(results) - 64} 个目录"}
    yield from shortcut(payload.get("shortcut_preview"), {})
    if payload.get("incremental") is True and isinstance(payload.get("plan_id"), str) and isinstance(payload.get("items"), (list, tuple)):
        yield from shortcut(payload, {})


class ResultProjection:
    """One normalized stream for durable logs and the bounded panel snapshot."""
    def __init__(self, payload: Any, *, message: str = "", max_details: int = 80,
                 context: dict[str, Any] | None = None, extra_details=()) -> None:
        self.payload = payload
        self.context = diagnostic_context(context)
        self.extra_details = extra_details
        self.max_details = max_details
        self.result: dict[str, Any] = {"summary": _text(message, 512), "counters": [], "details": [], "truncated": 0}
        self._seen: set[str] = set()
        self._inspect(payload)

    def _inspect(self, value: Any, prefix: str = "", depth: int = 0) -> None:
        if not isinstance(value, dict) or depth > 3:
            return
        if not self.result["summary"]:
            self.result["summary"] = _text(value.get("message") or value.get("error"), 512)
        for key in sorted(_COUNTERS.intersection(value)):
            counter = value[key]
            finite_number = (isinstance(counter, int) and not isinstance(counter, bool)) or (isinstance(counter, float) and math.isfinite(counter))
            if finite_number and len(self.result["counters"]) < 48:
                self.result["counters"].append({"label": f"{prefix}{_LABELS.get(key, key)}", "value": counter})
        for key in sorted(_CONTAINERS.intersection(value)):
            self._inspect(value[key], f"{prefix}{_PREFIXES.get(key, key)}·", depth + 1)

    def iter_details(self):
        from itertools import chain

        extra = (_diagnostic(item, "error", context=self.context) for item in self.extra_details)
        for item in chain(extra, iter_result_details(self.payload, context=self.context)):
            if item is None:
                continue
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key not in self._seen:
                if len(self.result["details"]) < self.max_details:
                    self._seen.add(key)
                    self.result["details"].append(item)
                else:
                    self.result["truncated"] += 1
            yield item


def summarize_result(payload: Any, *, message: str = "", max_details: int = 80,
                     context: dict[str, Any] | None = None) -> dict[str, Any]:
    projection = ResultProjection(payload, message=message, max_details=max_details, context=context)
    for _ in projection.iter_details():
        pass
    return projection.result
