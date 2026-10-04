# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules


repo_root = Path(SPEC).resolve().parent.parent
framework_backend = repo_root / "apps" / "framework" / "backend"
entrypoint = framework_backend / "work_catalog_yaml" / "desktop.py"
sys.path.insert(0, str(framework_backend))
from work_catalog_yaml.layout import backend_roots

backend_paths = backend_roots(repo_root)

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
for frontend in sorted((repo_root / "apps" / "features").glob("*/frontend")):
    add_tree(datas, frontend, frontend.relative_to(repo_root))

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
# Legacy presentation exports are resolved lazily via import_module.
hiddenimports += ["work_catalog_yaml.jp_tv.browse_payload"]
for backend in backend_paths:
    if backend == framework_backend:
        continue
    for package in sorted(backend.iterdir()):
        if package.is_dir() and (package / "__init__.py").is_file():
            hiddenimports += collect_submodules(package.name)

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
