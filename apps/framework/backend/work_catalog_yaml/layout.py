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

import os
import sys
from pathlib import Path, PureWindowsPath


_APPLICATION_ROOT_ENV = "NIMDA_APPLICATION_ROOT"
_WORKSPACE_ROOT_ENV = "NIMDA_WORKSPACE_ROOT"


def code_repo_root() -> Path:
    """Backend code root for the framework app."""
    return Path(__file__).resolve().parents[1]


def _is_workspace(path: Path) -> bool:
    return all((path / name).is_dir() for name in ("config", "data"))


def application_root() -> Path:
    """Return the root containing the application's bundled ``apps`` resources."""
    configured = os.environ.get(_APPLICATION_ROOT_ENV, "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        if not (root / "apps").is_dir():
            raise ValueError(
                f"{_APPLICATION_ROOT_ENV} is not a valid Nimda application root: {root} "
                "(missing apps)"
            )
        return root
    return workspace_root()


def workspace_root() -> Path:
    """Return the mutable workspace containing shared config and data."""
    configured = os.environ.get(_WORKSPACE_ROOT_ENV, "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
        missing = [name for name in ("config", "data") if not (root / name).is_dir()]
        if missing:
            raise ValueError(
                f"{_WORKSPACE_ROOT_ENV} 不是有效的 Nimda 工作区：{root}（缺少 {', '.join(missing)}）"
            )
        return root
    repo = code_repo_root()
    for cand in (repo, *repo.parents):
        if (cand / "apps").is_dir() and _is_workspace(cand):
            return cand
    # Fallback for the old sibling layout: <workspace>/<code-repo>.
    return repo.parent


def resolve_workspace_path(value: str | Path) -> Path:
    """Resolve a configured path consistently, independent of the launch cwd."""
    path = Path(value).expanduser()
    windows_path = PureWindowsPath(path)
    if windows_path.drive and not windows_path.root:
        raise ValueError(f"配置路径不能使用驱动器相对路径，请使用完整路径：{value}")
    return (path if path.is_absolute() else workspace_root() / path).resolve()


def default_source_data_dir() -> Path:
    """Raw source data directory."""
    ws = workspace_root()
    modern = ws / "data" / "source"
    if modern.is_dir() or _is_workspace(ws):
        return modern
    return code_repo_root().parent / "Data"


def workspace_catalog_data_root() -> Path:
    """Legacy runtime catalog data root."""
    ws = workspace_root()
    modern = ws / "data" / "work-catalog"
    if modern.is_dir() or _is_workspace(ws):
        return modern
    return code_repo_root().parent / "work-catalog-data"


def workspace_config_root() -> Path:
    """Workspace-level application configuration root."""
    ws = workspace_root()
    modern = ws / "config"
    if modern.is_dir() or _is_workspace(ws):
        return modern
    return workspace_catalog_data_root() / "Config"


def framework_frontend_root() -> Path:
    return application_root() / "apps" / "framework" / "frontend"


def framework_config_path() -> Path:
    return workspace_config_root() / "framework" / "app.yaml"


def feature_root(feature_id: str) -> Path:
    return application_root() / "apps" / "features" / feature_id


def feature_frontend_root(feature_id: str) -> Path:
    return feature_root(feature_id) / "frontend"


def feature_backend_root(feature_id: str) -> Path:
    return feature_root(feature_id) / "backend"


def feature_config_path(feature_id: str) -> Path:
    return workspace_config_root() / "features" / feature_id / "config.yaml"


def feature_data_root(feature_id: str) -> Path:
    return workspace_root() / "data" / "features" / feature_id


def backend_roots(root: Path | None = None) -> tuple[Path, ...]:
    """Discover framework and feature backends for runtime and packaging."""
    app_root = root if root is not None else application_root()
    candidates = (
        app_root / "apps" / "framework" / "backend",
        *sorted((app_root / "apps" / "features").glob("*/backend")),
    )
    return tuple(path.resolve() for path in candidates if path.is_dir())


def ensure_feature_backend_paths(root: Path | None = None) -> None:
    for backend in backend_roots(root):
        s = str(backend.resolve())
        if s not in sys.path:
            sys.path.insert(0, s)


def workspace_parsed_yaml_dir() -> Path:
    """Parsed JP TV YAML DB directory."""
    modern = feature_data_root("collection-detail") / "db"
    if modern.is_dir() or _is_workspace(workspace_root()):
        return modern
    return workspace_catalog_data_root() / "DB"
