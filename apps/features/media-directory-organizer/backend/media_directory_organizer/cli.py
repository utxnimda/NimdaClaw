"""Standalone command line interface for directory organization."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from media_directory_organizer.catalog import MediaCatalog
from media_directory_organizer.service import apply_plan, build_plan
from media_directory_organizer.settings import load_organizer_settings


def _prefer_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def render_plan(plan: dict[str, Any], *, details: bool = False) -> str:
    summary = plan["summary"]
    lines = [
        f"作品目录：{plan['root']}",
        f"数据库：{plan['catalog_root']}",
        "数据库系列作品：" + " / ".join(plan["family_works"]),
        "",
        "目标目录（移动前人工确认）：",
    ]
    if not plan["assignments"]:
        lines.append("  （没有生成可执行的目录调整）")
    for index, assignment in enumerate(plan["assignments"], start=1):
        authority = (
            "数据库 press_path"
            if assignment["target_authority"] == "database_press_path"
            else "由数据库作品名/格式/压制组推导，需重点确认"
        )
        lines.extend(
            [
                f"  {index}. 源目录：{assignment['source_dir']}",
                f"     目标目录：{assignment['target_dir']}",
                f"     依据：{assignment['work_name']} / {assignment['press_format']} / "
                f"{assignment['press_group']}；{authority}",
                f"     文件：{assignment['file_count']} 个，{_format_bytes(assignment['bytes'])}",
            ]
        )
    lines.extend(
        [
            "",
            f"汇总：{summary['source_directory_count']} 个源目录 -> "
            f"{summary['target_directory_count']} 个目标目录；"
            f"{summary['file_count']} 个文件；{_format_bytes(summary['bytes'])}",
            f"问题：{summary['issue_count']} 个",
        ]
    )
    if plan["issues"]:
        lines.append("")
        lines.append("阻止执行的问题：")
        for issue in plan["issues"]:
            suffix = f"（{issue['path']}）" if issue["path"] else ""
            lines.append(f"  - [{issue['code']}] {issue['message']}{suffix}")
    if details and plan["moves"]:
        lines.append("")
        lines.append("逐文件计划（文件名保持不变）：")
        for move in plan["moves"]:
            lines.append(f"  - {move['source']} -> {move['target']}")
    lines.extend(
        [
            "",
            f"计划 ID：{plan['plan_id']}",
            "状态：可执行" if plan["ready"] else "状态：不可执行（请先解决上述问题）",
        ]
    )
    if plan["ready"]:
        lines.append(f"确认后执行：apply --root \"{plan['root']}\" --confirm {plan['plan_id']}")
    return "\n".join(lines)


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, type=Path, help="要分析的作品父目录")
    parser.add_argument("--config", type=Path, help="插件配置 YAML；默认使用工作区 feature 配置")
    parser.add_argument("--catalog-root", type=Path, help="覆盖 collection-detail 数据库目录")
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="只处理指定的一级子目录名；可重复传入。省略时处理全部不合规一级子目录",
    )
    parser.add_argument("--json", action="store_true", help="输出供未来 UI/API 复用的 JSON")
    parser.add_argument("--details", action="store_true", help="额外输出逐文件移动计划")


def _build(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_organizer_settings(args.config)
    catalog_root = args.catalog_root.expanduser().resolve() if args.catalog_root else settings.catalog_root
    catalog = MediaCatalog.load(catalog_root, domain="", country="")
    return build_plan(
        args.root,
        catalog=catalog,
        settings=settings,
        source_names=args.source or None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media-directory-organizer",
        description="按作品数据库预览并调整各分类作品的压制子目录；文件名永不改变。",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview", help="只分析并输出源目录到目标目录计划，不移动文件")
    _add_common_arguments(preview)
    apply = sub.add_parser("apply", help="重新预览并凭计划 ID 确认后执行")
    _add_common_arguments(apply)
    apply.add_argument("--confirm", default="", help="必须与本次重新生成的计划 ID 完全一致")
    return parser


def main(argv: list[str] | None = None) -> int:
    _prefer_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        plan = _build(args)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            print(render_plan(plan, details=args.details))
        if args.command == "preview":
            return 0 if plan["ready"] or not plan["issues"] else 2
        if not args.confirm:
            print("\n尚未移动任何文件：请人工核对上方目标目录，再用 --confirm <计划 ID> 执行。", file=sys.stderr)
            return 2
        result = apply_plan(plan, confirmation=args.confirm)
        if args.json:
            print(json.dumps({"execution": result}, ensure_ascii=False, indent=2))
        else:
            print(f"\n执行完成：移动 {result['moved_file_count']} 个文件。")
            for target in result["target_directories"]:
                print(f"  目标目录：{target}")
            for warning in result["cleanup_warnings"]:
                print(f"  警告：{warning}")
        return 0
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
