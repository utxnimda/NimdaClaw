"""Workspace layout helpers.

Preferred layout:

```
nimda/
  apps/framework/
  apps/features/<feature-id>/
  config/framework/
  config/features/<feature-id>/
  data/source/
  data/features/<feature-id>/
```

The older sibling layout is still accepted as a fallback for external checkouts
and small tests.
"""
from __future__ import annotations

import sys
from pathlib import Path


def code_repo_root() -> Path:
    """Backend code root for the framework app."""
    return Path(__file__).resolve().parents[1]


def workspace_root() -> Path:
    """Return the ``nimda`` workspace root when the project uses the new layout."""
    repo = code_repo_root()
    for cand in (repo, *repo.parents):
        if cand.name == "nimda" and (cand / "apps").is_dir() and (cand / "data").is_dir():
            return cand
    # Fallback for the old sibling layout: <workspace>/<code-repo>.
    return repo.parent


def default_source_data_dir() -> Path:
    """Raw source data directory."""
    ws = workspace_root()
    modern = ws / "data" / "source"
    if modern.is_dir() or ws.name == "nimda":
        return modern
    return code_repo_root().parent / "Data"


def workspace_catalog_data_root() -> Path:
    """Legacy runtime catalog data root."""
    ws = workspace_root()
    modern = ws / "data" / "work-catalog"
    if modern.is_dir() or ws.name == "nimda":
        return modern
    return code_repo_root().parent / "work-catalog-data"


def workspace_config_root() -> Path:
    """Workspace-level application configuration root."""
    ws = workspace_root()
    modern = ws / "config"
    if modern.is_dir() or ws.name == "nimda":
        return modern
    return workspace_catalog_data_root() / "Config"


def framework_frontend_root() -> Path:
    return workspace_root() / "apps" / "framework" / "frontend"


def framework_config_path() -> Path:
    return workspace_config_root() / "framework" / "app.yaml"


def feature_root(feature_id: str) -> Path:
    return workspace_root() / "apps" / "features" / feature_id


def feature_frontend_root(feature_id: str) -> Path:
    return feature_root(feature_id) / "frontend"


def feature_backend_root(feature_id: str) -> Path:
    return feature_root(feature_id) / "backend"


def feature_config_path(feature_id: str) -> Path:
    return workspace_config_root() / "features" / feature_id / "config.yaml"


def feature_data_root(feature_id: str) -> Path:
    return workspace_root() / "data" / "features" / feature_id


def ensure_feature_backend_paths() -> None:
    features_dir = workspace_root() / "apps" / "features"
    if not features_dir.is_dir():
        return
    for backend in sorted(features_dir.glob("*/backend")):
        if not backend.is_dir():
            continue
        s = str(backend.resolve())
        if s not in sys.path:
            sys.path.insert(0, s)


def workspace_parsed_yaml_dir() -> Path:
    """Parsed JP TV YAML DB directory."""
    modern = feature_data_root("collection-detail") / "db"
    if modern.is_dir() or workspace_root().name == "nimda":
        return modern
    return workspace_catalog_data_root() / "DB"
