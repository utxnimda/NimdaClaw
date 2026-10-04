"""Read-only disk snapshots and strict paths used by preview and execution."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat

from work_catalog_yaml.paths import normalize_copied_path
from work_catalog_yaml.storage.filesystem import ordinary_directory

MAX_FILES = 20000
MAX_CHILDREN = 2000


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def absolute_path(raw) -> Path:
    value = normalize_copied_path(raw)
    path = Path(value)
    if not value or not path.is_absolute() or ".." in path.parts:
        raise ValueError("目录须为完整绝对路径，不允许相对路径或 ..")
    return Path(os.path.abspath(path))


def existing_directory(raw) -> Path:
    path = absolute_path(raw)
    if not ordinary_directory(str(path)):
        raise ValueError(f"目录不存在或不是普通目录（不支持符号链接 / 联接）：{path}")
    return path


def relative_path(raw) -> str:
    if not isinstance(raw, str):
        raise ValueError("相对路径必须是文字")
    value = raw.replace("\\", "/")
    parts = value.split("/")
    if not value or PureWindowsPath(value).drive or value.startswith("/") or any(
        part in {"", ".", ".."} or part[-1:] in {" ", "."} or
        re.search(r'[<>:"|?*\x00-\x1f]', part) or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
        for part in parts
    ):
        raise ValueError(f"不安全的目标相对路径：{raw}")
    return value


def child_directory(root: Path, child) -> Path:
    name = relative_path(child)
    if "/" in name:
        raise ValueError("整理单元必须是所选目录的直属子目录")
    return existing_directory(str(root / name))


def path_key(path: Path) -> str:
    return os.path.normcase(str(path))


def signature(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink > 1:
        raise ValueError(f"资源不是普通独占文件（不支持链接）：{path}")
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino]


def safe_target(path: Path) -> Path:
    """Validate the original chain without resolving away links; allow missing dirs."""
    path = absolute_path(str(path))
    for part in reversed((path, *path.parents)):
        if os.path.lexists(part):
            metadata = part.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise ValueError(f"目标目录链包含文件、符号链接或联接：{part}")
    return path


def nearest_existing(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return path


def snapshot(directory: Path) -> dict:
    existing_directory(str(directory))
    files, directories, issues = [], [], []
    pending = [directory]
    while pending:
        current = pending.pop()
        if not ordinary_directory(str(current)):
            raise ValueError(f"扫描中的目录已变化或不可读：{current}")
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda item: item.name.casefold())
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(directory).as_posix()
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                issues.append({"code": "unsafe-link", "message": "不整理符号链接或联接，请先人工处理", "path": str(path), "blocking": True})
            elif stat.S_ISDIR(metadata.st_mode):
                directories.append(relative)
                pending.append(path)
                if len(directories) > MAX_FILES:
                    raise ValueError("子目录层级数量超过安全限制，请缩小选择范围")
            elif stat.S_ISREG(metadata.st_mode):
                files.append({"relative_path": relative, "size": metadata.st_size, "signature": signature(path)})
                if len(files) > MAX_FILES:
                    raise ValueError(f"单目录超过 {MAX_FILES} 个文件，请缩小选择范围")
            else:
                issues.append({"code": "special-file", "message": "不支持的资源类型", "path": str(path), "blocking": True})
    files.sort(key=lambda item: item["relative_path"])
    directories.sort()
    identity = directory.stat()
    value = {"files": files, "directories": directories, "identity": [identity.st_dev, identity.st_ino], "issues": issues}
    value["fingerprint"] = digest(value)
    return value


def scan_root(raw) -> dict:
    root = existing_directory(raw)
    children, root_files, issues = [], [], []
    with os.scandir(root) as iterator:
        for entry in sorted(iterator, key=lambda item: item.name.casefold()):
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                issues.append({"code": "unsafe-link", "message": "跳过链接目录 / 文件", "path": entry.path, "blocking": False})
            elif stat.S_ISDIR(metadata.st_mode):
                children.append({"id": digest(entry.path)[:24], "name": entry.name, "path": entry.path, "status": "unpreviewed", "summary": "尚未预览"})
            elif stat.S_ISREG(metadata.st_mode):
                root_files.append({"name": entry.name, "size": metadata.st_size})
            if len(children) + len(root_files) > MAX_CHILDREN:
                raise ValueError("所选目录直属项目过多，请选择更具体的作品目录")
    return {"root": str(root), "children": children, "root_files": root_files, "issues": issues}
