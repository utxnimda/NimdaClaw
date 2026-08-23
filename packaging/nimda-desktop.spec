# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules


repo_root = Path(SPEC).resolve().parent.parent
framework_backend = repo_root / "apps" / "framework" / "backend"
detail_backend = repo_root / "apps" / "features" / "collection-detail" / "backend"
info_backend = repo_root / "apps" / "features" / "collection-info" / "backend"
organizer_backend = repo_root / "apps" / "features" / "media-directory-organizer" / "backend"
entrypoint = framework_backend / "work_catalog_yaml" / "desktop.py"
backend_paths = [framework_backend, detail_backend, info_backend, organizer_backend]

for backend_path in backend_paths:
    value = str(backend_path)
    if value not in sys.path:
        sys.path.insert(0, value)


def add_tree(datas, source, destination):
    source = Path(source)
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative_parent = path.relative_to(source).parent
        datas.append((str(path), str(Path(destination) / relative_parent)))


datas = []
add_tree(datas, repo_root / "apps" / "framework" / "frontend", "apps/framework/frontend")
add_tree(
    datas,
    repo_root / "apps" / "features" / "collection-detail" / "frontend",
    "apps/features/collection-detail/frontend",
)
add_tree(
    datas,
    repo_root / "apps" / "features" / "collection-info" / "frontend",
    "apps/features/collection-info/frontend",
)
add_tree(
    datas,
    repo_root / "apps" / "features" / "media-directory-organizer" / "frontend",
    "apps/features/media-directory-organizer/frontend",
)

default_config = framework_backend / "work_catalog_yaml" / "jp_tv" / "browse_config.default.yaml"
datas.append((str(default_config), "work_catalog_yaml/jp_tv"))

binaries = []
hiddenimports = []
for package_name in ("webview", "clr_loader", "pythonnet"):
    package_datas, package_binaries, package_hidden = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("collection_detail")
hiddenimports += collect_submodules("collection_info")
hiddenimports += collect_submodules("media_directory_organizer")

a = Analysis(
    [str(entrypoint)],
    pathex=[str(path) for path in backend_paths],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["cefpython3", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Nimda",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory=".",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Nimda",
)
