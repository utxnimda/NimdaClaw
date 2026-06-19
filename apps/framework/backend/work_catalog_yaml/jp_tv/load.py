from __future__ import annotations

from pathlib import Path

from work_catalog_yaml.jp_tv.validate import JpTvEntry, load_jp_tv_entries_from_yaml
from work_catalog_yaml.yaml_io import load_yaml


def load_jp_tv_yaml_file(file_path: str | Path) -> list[JpTvEntry]:
    path = Path(file_path)
    doc = load_yaml(path)
    return load_jp_tv_entries_from_yaml(doc)
