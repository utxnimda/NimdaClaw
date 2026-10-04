"""One human-readable UTF-8 format for application and operation logs."""
from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

_CONTEXT_LABELS = {
    "stage": "阶段", "action": "动作", "object": "对象", "source_path": "来源路径", "target_path": "目标路径",
    "path": "路径", "name": "名称", "work_key": "作品标识", "yaml_source_rel": "数据库文件",
    "index_in_file": "文件内记录索引", "record_index": "记录索引", "press_index": "压制记录索引",
    "expected": "预期", "actual": "实际", "error_type": "异常类型", "location": "代码位置",
    "reason": "原因", "shortcut_path": "快捷方式", "target_error": "目标错误", "target_exists": "目标存在",
    "code": "诊断代码", "source": "来源", "target": "目标",
    "phase": "阶段", "object_type": "对象类型", "status": "状态", "press_key": "压制记录标识",
    "press_path": "压制目录", "operation_count": "操作数", "root_path": "根目录", "relpath": "相对路径",
    "history_file": "历史文件", "link_path": "链接路径",
    "library_id": "资源库标识", "root": "根目录",
}


def log_level(value: Any) -> str:
    key = str(value or "info").casefold()
    if key in {"debug", "trace"}:
        return key.upper()
    if key in {"error", "critical", "fatal", "failed"}:
        return "ERROR"
    if key in {"warn", "warning", "cancelled"}:
        return "WARN"
    return "INFO"


def timestamp(value: Any = None) -> str:
    try:
        if isinstance(value, datetime):
            date = value
        elif isinstance(value, (int, float)):
            date = datetime.fromtimestamp(value).astimezone()
        elif isinstance(value, str):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            date = datetime.now().astimezone()
        date = date.astimezone()
    except (ValueError, TypeError, OverflowError, OSError):
        date = datetime.now().astimezone()
    return date.strftime("%Y-%m-%d %H:%M:%S.") + f"{date.microsecond // 1000:03d}"


def format_log_lines(message: Any, *, at: Any = None, level: Any = "INFO") -> str:
    """Prefix every physical line, including multiline context and tracebacks."""
    text = str(message if message is not None else "").replace("\x00", "\\0")
    prefix = f"[{timestamp(at)}][{log_level(level)}] "
    return "\n".join(prefix + line for line in (text.splitlines() or [""])) + "\n"


def _value_text(value: Any, *, depth: int = 0) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (str, int, float)):
        return str(value)
    if depth >= 2:
        return ""
    if isinstance(value, (list, tuple)):
        return "、".join(_value_text(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        labels = {"file": "文件", "line": "行", "function": "函数", "name": "名称", "type": "类型"}
        return "，".join(f"{labels.get(key, _CONTEXT_LABELS.get(key, key))}：{_value_text(item, depth=depth + 1)}"
                        for key, item in value.items() if key in labels or key in _CONTEXT_LABELS)
    return ""


def format_context(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    items = []
    for key, label in _CONTEXT_LABELS.items():
        if key not in value:
            continue
        text = _value_text(value[key])
        if text:
            items.append(f"{label}：{text}")
    return "；".join(items)


def format_operation_entry(payload: dict[str, Any]) -> str:
    operation = payload.get("operation") if isinstance(payload.get("operation"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    detail = payload.get("detail") if isinstance(payload.get("detail"), dict) else {}
    item = detail if payload.get("kind") == "result_detail" else event
    at = item.get("at") or payload.get("at") or operation.get("updated_at")
    level = item.get("level") or ("error" if payload.get("traceback") else operation.get("status"))
    messages = []
    if event.get("seq") == 1:
        messages.append(f"操作 ID：{operation.get('id', '')}；标题：{operation.get('title', '')}；接口：{operation.get('path', '')}；来源：{operation.get('source', 'server')}")
    message = item.get("message") or (operation.get("message") if event else "")
    if message:
        messages.append(str(message))
    if event.get("detail"):
        messages.append(str(event["detail"]))
    contexts = {}
    for source in (operation.get("context"), item.get("context"), item):
        if isinstance(source, dict):
            contexts.update({key: source[key] for key in _CONTEXT_LABELS if key in source})
    context = format_context(contexts)
    if context:
        messages.append(context)
    completed, total = event.get("completed"), event.get("total")
    if completed is not None or total is not None:
        messages.append(f"进度：{completed if completed is not None else '-'} / {total if total is not None else '-'} {event.get('unit', '')}".rstrip())
    if operation.get("finished_at") and isinstance(operation.get("result"), dict):
        counters = operation["result"].get("counters", [])
        text = "；".join(f"{counter.get('label', '')}：{_value_text(counter.get('value'))}" for counter in counters if isinstance(counter, dict))
        if text:
            messages.append("处理统计：" + text)
    output = "".join(format_log_lines(message, at=at, level=level) for message in messages)
    if payload.get("traceback"):
        output += format_log_lines(payload["traceback"], at=at, level="ERROR")
    return output


class UnifiedLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        content = format_log_lines(f"{record.name}: {record.getMessage()}", at=record.created, level=record.levelname)
        if record.exc_info:
            content += format_log_lines(self.formatException(record.exc_info), at=record.created, level="ERROR")
        if record.stack_info:
            content += format_log_lines(record.stack_info, at=record.created, level=record.levelname)
        return content.rstrip("\n")
