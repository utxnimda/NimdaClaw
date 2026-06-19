from __future__ import annotations

import json
import html as html_lib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from work_catalog_yaml.yaml_io import load_yaml


@dataclass
class CatalogFile:
    path: str
    content: str


@dataclass
class Catalog:
    version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    root: str = "Data"
    files: list[CatalogFile] = field(default_factory=list)


def load_catalog_from_file(file_path: str | Path) -> Catalog:
    path = Path(file_path)
    doc = load_yaml(path)
    if not isinstance(doc, dict):
        raise ValueError("YAML 根节点须为对象")
    files_raw = doc.get("files")
    if not isinstance(files_raw, list):
        raise ValueError("缺少 files 数组")
    files: list[CatalogFile] = []
    for i, fr in enumerate(files_raw):
        if not isinstance(fr, dict):
            raise ValueError(f"files[{i}] 须为对象")
        p = fr.get("path")
        c = fr.get("content")
        if not isinstance(p, str) or not str(p).strip():
            raise ValueError(f"files[{i}].path 无效")
        if not isinstance(c, str):
            raise ValueError(f"files[{i}].content 须为字符串")
        norm = str(p).replace("\\", "/")
        files.append(CatalogFile(path=norm, content=c))

    meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    rv = doc.get("root")
    root = rv.replace("\\", "/").rstrip("/") if isinstance(rv, str) else "Data"

    ver = doc.get("version")
    version = ver if isinstance(ver, int) else 1

    return Catalog(version=version, metadata=meta or {}, root=root, files=files)


def materialize_catalog(catalog: Catalog, output_base: str | Path) -> list[Path]:
    base = Path(output_base).resolve()
    written: list[Path] = []
    for f in catalog.files:
        dest = base / catalog.root / f.path.replace("\\", "/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.content, encoding="utf-8")
        written.append(dest)
    return written


def preview_tree(catalog: Catalog) -> str:
    lines = [f"root: {catalog.root}", ""]
    for f in catalog.files:
        lc = len(f.content.replace("\r\n", "\n").split("\n"))
        lines.append(f"- {catalog.root}/{f.path}  ({lc} 行)")
    return "\n".join(lines)


def catalog_to_json_text(catalog: Catalog) -> str:
    payload = {
        "version": catalog.version,
        "metadata": catalog.metadata or {},
        "root": catalog.root,
        "files": [
            {"path": f.path, "lines": f.content.replace("\r\n", "\n").split("\n")}
            for f in catalog.files
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def catalog_to_html(catalog: Catalog) -> str:
    title = str(catalog.metadata.get("title", "YAML 展开") if catalog.metadata else "YAML 展开")

    blocks = []
    for i, f in enumerate(catalog.files):
        summary = html_lib.escape(f"{catalog.root}/{f.path}")
        body = html_lib.escape(f.content.replace("\r\n", "\n"))
        open_attr = ' open' if i == 0 else ""
        blocks.append(
            f"""
    <details class="file"{open_attr}>
      <summary>{summary}</summary>
      <pre>{body}</pre>
    </details>"""
        )

    h1 = html_lib.escape(title)
    items = "\n".join(blocks)

    return f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html_lib.escape(title)}</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 24px; background: #0f1419; color: #e7e9ea; }}
    h1 {{ font-size: 1.125rem; font-weight: 600; }}
    .file {{ margin: 12px 0; border: 1px solid #38444d; border-radius: 8px; overflow: hidden; }}
    summary {{ cursor: pointer; padding: 10px 12px; background: #16181c; }}
    pre {{
      margin: 0;
      padding: 12px;
      overflow: auto;
      max-height: 60vh;
      font-size: 13px;
      line-height: 1.45;
      background: #000;
      white-space: pre;
    }}
  </style>
</head>
<body>
  <h1>{h1}</h1>
{items}
</body>
</html>"""
