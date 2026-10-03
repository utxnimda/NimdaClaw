"""Resource-scan snapshot publication, independent of application configuration.

Callers serialize cache readers and writers. A scan writes into a new generation;
its manifest is the final atomic write, so an interrupted scan never replaces the
previous snapshot with a partially written tree.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import uuid
import threading
from functools import wraps
from pathlib import Path
from typing import Any, Callable, ParamSpec, TypeVar

from work_catalog_yaml.persistence import atomic_write_bytes
from work_catalog_yaml.operation_progress import report_progress
from work_catalog_yaml.yaml_io import dump_yaml_string

from collection_detail.resource_tree import RESOURCE_SCAN_METRICS_VERSION, resource_node_cache_payload


_GENERATION_RE = re.compile(r"^[0-9a-f]{32}$")
_CACHE_LOCK = threading.RLock()
_P = ParamSpec("_P")
_R = TypeVar("_R")


def serialized_cache_access(function: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with _CACHE_LOCK:
            return function(*args, **kwargs)
    return wrapped


def generation_directory(node_root: Path, generation: str) -> Path:
    if not _GENERATION_RE.fullmatch(generation):
        raise ValueError("资源库缓存版本无效，请重新扫描。")
    return node_root / generation


def node_cache_path(node_root: Path, relpath: str, generation: str) -> Path:
    directory = generation_directory(node_root, generation)
    digest = hashlib.sha1(str(relpath or "").encode("utf-8")).hexdigest()
    return directory / digest[:2] / f"{digest[2:]}.yaml"


def write_node_cache(node_root: Path, generation: str, node: dict[str, Any], *, _progress: list[int] | None = None) -> None:
    if node.get("children_loaded") is False:
        return
    relpath = str(node.get("relpath") or "")
    if _progress is None:
        _progress = [0]
    if _progress[0] % 100 == 0:
        report_progress("写入资源目录索引缓存", completed=_progress[0], unit="缓存节点", detail=str(node.get("path") or relpath))
    payload = resource_node_cache_payload(node)
    payload["generation"] = generation
    atomic_write_bytes(node_cache_path(node_root, relpath, generation), dump_yaml_string(payload).encode("utf-8"))
    _progress[0] += 1
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            write_node_cache(node_root, generation, child, _progress=_progress)


def publish_resource_snapshot(
    manifest_path: Path,
    node_root: Path,
    payload: dict[str, Any],
    tree: dict[str, Any],
) -> dict[str, Any]:
    generation = uuid.uuid4().hex
    directory = generation_directory(node_root, generation)
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        **payload,
        "ok": True,
        "cached": True,
        "metrics_version": RESOURCE_SCAN_METRICS_VERSION,
        "generation": generation,
        "cache_path": str(manifest_path),
        "node_cache_dir": str(directory),
        "tree": resource_node_cache_payload(tree)["node"],
    }
    manifest_bytes = dump_yaml_string(manifest).encode("utf-8")
    try:
        write_node_cache(node_root, generation, tree)
        report_progress("发布完整资源库缓存快照", detail=str(manifest_path))
        atomic_write_bytes(manifest_path, manifest_bytes)
    except BaseException:
        report_progress("资源库缓存发布中断，检查并恢复上一快照")
        # An interruption can occur just after atomic replacement. Retain that
        # generation when the manifest already references it (or cannot be read).
        try:
            published = manifest_path.read_bytes() == manifest_bytes
        except FileNotFoundError:
            published = False
        except OSError:
            published = True
        if not published:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    return manifest


def remove_previous_generation(node_root: Path, generation: str) -> None:
    """Remove only the replaced snapshot; another process may be staging others."""
    if not _GENERATION_RE.fullmatch(generation):
        return
    directory = generation_directory(node_root, generation)
    if (
        directory.is_dir()
        and not directory.is_symlink()
        and not (hasattr(directory, "is_junction") and directory.is_junction())
    ):
        shutil.rmtree(directory, ignore_errors=True)
