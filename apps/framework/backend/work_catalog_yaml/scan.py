from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from work_catalog_yaml.yaml_io import dump_yaml_string


def scan_data_to_yaml(root_dir: str | Path) -> str:
    base = Path(root_dir).resolve()
    root_label = base.name.rstrip("/\\") or "data"
    rels: list[str] = []
    for p in base.rglob("*"):
        if p.is_file():
            rels.append(p.relative_to(base).as_posix())
    rels.sort()

    files = []
    for rel in rels:
        full = base / rel
        content = full.read_text(encoding="utf-8")
        files.append({"path": rel.replace("\\", "/"), "content": content})

    doc = {
        "version": 1,
        "metadata": {
            "generated": datetime.now(timezone.utc).isoformat(),
            "sourceRoot": root_label,
        },
        "root": root_label,
        "files": files,
    }
    return dump_yaml_string(doc)
