from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path, PurePosixPath

from work_catalog_yaml.catalog import (
    catalog_to_html,
    catalog_to_json_text,
    load_catalog_from_file,
    materialize_catalog,
    preview_tree,
)
from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.parse import (
    JpTvParseError,
    infer_txt_relpath_for_materialize,
    parse_jp_tv_batch,
    parse_jp_tv_txt_file,
)
from work_catalog_yaml.jp_tv.render import render_jp_tv_text
from work_catalog_yaml.jp_tv.validate import jp_tv_works_to_plain_list
from work_catalog_yaml.layout import workspace_parsed_yaml_dir
from work_catalog_yaml.scan import scan_data_to_yaml
from work_catalog_yaml.yaml_io import dump_yaml_string


USAGE_EPILOG = """
jp-tv parse : 单个 txt → YAML（与当前 ``validate`` / ``parse`` 定义的 schema 一致；须保留原始 txt 以便重生成）。
jp-tv parse-batch : data/source 内 [JP][TVInfo]* → data/features/collection-detail/db/<同名>.yaml（根级作品数组；形见 samples/tv-jp.yaml）；加 ``--force`` 覆盖已有 yaml。

jp-tv to-txt : 按 ``collection-type.data.domain``（大类码）+ ``release_type`` 与 country slug + 文件名写出 Data/Animation/Japan/TV/…

解析 txt 会生成 date、collection-type（含 collectioned / markers / continuations）、country、name。

平面目录参见 samples/example-tvinfo.yaml ；结构化条目参见 samples/tv-jp.yaml

jp-tv browse : 需 ``pip install 'work-catalog-yaml[web]'``。配置仅需 ``paths.filesystem_root``（数据 DB）；
``paths.filesystem_root`` 指向数据 DB；服务端加载该目录下全部 ``*.yaml``。保存前备份写入 ``<DB 的上一级>/History/``。
配置文件查找见包内说明 / ``--config`` / ``JP_TV_BROWSE_CONFIG_PATH``。"""


def _prefer_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


def _cmd_preview(args: argparse.Namespace) -> None:
    cat = load_catalog_from_file(args.input)
    print(preview_tree(cat))


def _cmd_materialize(args: argparse.Namespace) -> None:
    cat = load_catalog_from_file(args.input)
    base = Path(args.output) if args.output else Path.cwd()
    written = materialize_catalog(cat, base)
    print(f"已写入 {len(written)} 个文件 → {base.resolve()}")


def _cmd_to_json(args: argparse.Namespace) -> None:
    cat = load_catalog_from_file(args.input)
    text = catalog_to_json_text(cat)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _cmd_to_html(args: argparse.Namespace) -> None:
    if not args.output:
        sys.exit("to-html 需要 -o out.html")
    cat = load_catalog_from_file(args.input)
    Path(args.output).write_text(catalog_to_html(cat), encoding="utf-8")
    print(f"已生成 {Path(args.output).resolve()}")


def _cmd_scan(args: argparse.Namespace) -> None:
    text = scan_data_to_yaml(args.root)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _cmd_jp_tv_parse_batch(args: argparse.Namespace) -> None:
    try:
        written, skipped = parse_jp_tv_batch(
            txt_root=Path(args.txt_root) if args.txt_root else None,
            yaml_root=Path(args.yaml_root) if args.yaml_root else None,
            force=args.force,
        )
    except (FileNotFoundError, JpTvParseError) as e:
        sys.exit(str(e))
    out_root = (
        Path(args.yaml_root).resolve()
        if args.yaml_root
        else workspace_parsed_yaml_dir().resolve()
    )
    print(f"YAML 根目录: {out_root}")
    print(f"新写入 {len(written)} 个；跳过已存在 {len(skipped)} 个（加 --force 可覆盖）")


def _cmd_jp_tv_parse(args: argparse.Namespace) -> None:
    try:
        entries = parse_jp_tv_txt_file(args.input)
    except JpTvParseError as e:
        sys.exit(str(e))
    text = dump_yaml_string(jp_tv_works_to_plain_list(entries))
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"已写入 {Path(args.output).resolve()}")
    else:
        print(text, end="")


def _cmd_jp_tv_render(args: argparse.Namespace) -> None:
    entries = load_jp_tv_yaml_file(args.input)
    sys.stdout.write(render_jp_tv_text(entries))


def _cmd_jp_tv_materialize(args: argparse.Namespace) -> None:
    entries = load_jp_tv_yaml_file(args.input)
    base = Path(args.output) if args.output else Path.cwd()
    yaml_p = Path(args.input).resolve()
    if args.relative_path:
        rel_txt = args.relative_path.replace("\\", "/").lstrip("/")
    else:
        rel_txt = infer_txt_relpath_for_materialize(entries, yaml_p)
    dest_parts = (*PurePosixPath(args.data_root.strip("/")).parts, *PurePosixPath(rel_txt.strip("/")).parts)
    dest = Path(base).resolve()
    for part in dest_parts:
        if part and part != ".":
            dest = dest / part
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_jp_tv_text(entries), encoding="utf-8")
    print(f"已写入 {dest.resolve()}")


def _jp_tv_browse_listen_port(host: str, preferred: int, *, span: int = 48) -> tuple[int, bool]:
    """若 ``preferred`` 可 bind 则用之，否则在 ``[preferred, preferred+span)`` 内找第一个空端口。"""
    import socket

    for p in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, p))
            except OSError:
                continue
            return p, p != preferred
    return preferred, False


def _cmd_jp_tv_browse(args: argparse.Namespace) -> None:
    if getattr(args, "config", None):
        os.environ["JP_TV_BROWSE_CONFIG_PATH"] = str(Path(args.config).resolve())
    if getattr(args, "browse_static", None):
        os.environ["JP_TV_BROWSE_STATIC_DIR"] = str(Path(args.browse_static).resolve())
    from work_catalog_yaml.jp_tv.browse_settings import reset_browse_settings_cache

    reset_browse_settings_cache()

    try:
        import uvicorn

        from work_catalog_yaml.jp_tv.browse_app import app as jp_tv_browse_asgi_app
        from work_catalog_yaml.jp_tv.browse_app import browse_static_root
    except ImportError as e:
        sys.exit(f"请先安装：`pip install 'work-catalog-yaml[web]'`（{e}）")

    host = str(args.host or "127.0.0.1")
    preferred = int(args.port)
    if getattr(args, "strict_port", False):
        port = preferred
        bumped = False
    else:
        port, bumped = _jp_tv_browse_listen_port(host, preferred)
    if bumped:
        print(
            f"[jp-tv browse] 端口 {preferred} 已被占用，改用 {port}。"
            " 若需固定端口请加 --strict-port 并结束占用该端口的进程。",
            file=sys.stderr,
        )

    print(
        f"[jp-tv browse] browse_static → {browse_static_root()}",
        file=sys.stderr,
    )
    print(
        f"JP TV YAML 浏览器：http://{host}:{port}/ （配置：config/framework/app.yaml + config/features/*/config.yaml，"
        "或 JP_TV_BROWSE_CONFIG_PATH / 当前目录 jp-tv-browse.config.yaml）",
    )

    uvicorn.run(jp_tv_browse_asgi_app, host=host, port=port, log_level="info")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="work-catalog",
        description="从 YAML 描述目录结构与 TV 数据并导出文本 / 落盘",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=USAGE_EPILOG,
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_prev = sub.add_parser("preview", help="平面 catalog：预览 root 与文件行数")
    p_prev.add_argument("-i", "--input", required=True, help="catalog.yaml")
    p_prev.set_defaults(func=_cmd_preview)

    p_mat = sub.add_parser("materialize", help="平面 catalog：写入 <root>/<files.path>")
    p_mat.add_argument("-i", "--input", required=True)
    p_mat.add_argument("-o", "--output", help="输出根目录，默认当前目录")
    p_mat.set_defaults(func=_cmd_materialize)

    p_js = sub.add_parser("to-json", help="平面 catalog → JSON（含 lines 数组）")
    p_js.add_argument("-i", "--input", required=True)
    p_js.add_argument("-o", "--output", help="输出文件；省略则打印到 stdout")
    p_js.set_defaults(func=_cmd_to_json)

    p_ht = sub.add_parser("to-html", help="平面 catalog → 简单 HTML")
    p_ht.add_argument("-i", "--input", required=True)
    p_ht.add_argument("-o", "--output", required=True)
    p_ht.set_defaults(func=_cmd_to_html)

    p_sc = sub.add_parser("scan-data", help="扫描数据目录 → 平面 catalog YAML")
    p_sc.add_argument("-root", "--root", required=True, dest="root", help="例如 Data 目录")
    p_sc.add_argument("-o", "--output", help="输出 yaml；省略则 stdout")
    p_sc.set_defaults(func=_cmd_scan)

    jp = sub.add_parser("jp-tv", help="jp_tv_info 结构化 YAML")
    jp_sub = jp.add_subparsers(dest="jp_tv_action", required=True)

    p_jpb = jp_sub.add_parser(
        "parse-batch",
        help="批量：data/source 内 [JP][TVInfo]* → data/features/collection-detail/db/<同名>.yaml（作品数组；结构随当前解析代码，须从 txt 重跑以应用 schema 变更）",
    )
    p_jpb.add_argument(
        "--txt-root",
        type=Path,
        help="原始 txt 根目录，默认 <仓库上级>/Data",
    )
    p_jpb.add_argument(
        "--yaml-root",
        type=Path,
        help="YAML 输出根目录（扁平 *.yaml），默认 data/features/collection-detail/db",
    )
    p_jpb.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 .yaml",
    )
    p_jpb.set_defaults(func=_cmd_jp_tv_parse_batch)

    p_jpp = jp_sub.add_parser(
        "parse",
        help="解析单个 [JP][TVInfo].txt → 作品数组 YAML（写出形态与当前 validate/parse 一致；改 domain 等后请对原始 txt 重生成）",
    )
    p_jpp.add_argument("-i", "--input", required=True, help=".txt 路径")
    p_jpp.add_argument("-o", "--output", help="输出 .yaml（省略则打印）")
    p_jpp.set_defaults(func=_cmd_jp_tv_parse)

    p_jpr = jp_sub.add_parser("render", help="打印 [JP][TVInfo] 行文本")
    p_jpr.add_argument("-i", "--input", required=True)
    p_jpr.set_defaults(func=_cmd_jp_tv_render)

    p_jpm = jp_sub.add_parser(
        "to-txt",
        help="作品 YAML 数组写回 txt: output/data-root/relpath.txt",
    )
    p_jpm.add_argument("-i", "--input", required=True)
    p_jpm.add_argument("-o", "--output", help="工程根目录，默认当前目录")
    p_jpm.add_argument("--data-root", default="Data", help="落盘目录名，默认 Data")
    p_jpm.add_argument(
        "--relative-path",
        default=None,
        help="txt 相对路径（POSIX）；省略则按首条 collection-type + country + 文件名推断",
    )
    p_jpm.set_defaults(func=_cmd_jp_tv_materialize)

    p_jpv = jp_sub.add_parser(
        "browse",
        help="本地 Web：上传 JP TV 作品数组 YAML（需安装 [web] 可选依赖）",
    )
    p_jpv.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    p_jpv.add_argument("--port", type=int, default=8765, help="首选端口，默认 8765（被占用时自动递增，除非 --strict-port）")
    p_jpv.add_argument(
        "--strict-port",
        action="store_true",
        help="仅绑定 --port；若被占用则报错退出",
    )
    p_jpv.add_argument(
        "--config",
        type=Path,
        default=None,
        help="浏览页 YAML 配置文件路径（jp-tv-browse.config.yaml）；会设置 JP_TV_BROWSE_CONFIG_PATH",
    )
    p_jpv.add_argument(
        "--browse-static",
        type=Path,
        default=None,
        metavar="DIR",
        help="主体前端目录（含 index.html）；开发时指向 apps/framework/frontend。也可设 JP_TV_BROWSE_STATIC_DIR。",
    )
    p_jpv.set_defaults(func=_cmd_jp_tv_browse)

    return p


def main() -> None:
    _prefer_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
