"""Read-only verification of a desktop build against its source checkout.

Run with the same Python environment used by PyInstaller. This does not execute
the packaged application, read the shared database, or modify the package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import marshal
from pathlib import Path
import sys
from types import CodeType
import zipfile


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "framework" / "backend"))

from work_catalog_yaml.layout import backend_roots
from work_catalog_yaml.yaml_io import load_yaml_string


# CLI-only modules and legacy forwarding imports are intentionally unreachable
# from the desktop entrypoint. If present, their content is still checked.
OPTIONAL_MODULES = frozenset({
    "work_catalog_yaml.__main__", "work_catalog_yaml.cli", "work_catalog_yaml.catalog",
    "work_catalog_yaml.paths", "work_catalog_yaml.scan", "work_catalog_yaml.jp_tv.browse_save",
    "work_catalog_yaml.jp_tv.browse_api",
    "work_catalog_yaml.jp_tv.collection_records", "work_catalog_yaml.jp_tv.link_index",
})


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return digest_stream(stream)


def digest_stream(stream) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def verify_code(source: Path, code: CodeType) -> None:
    if not isinstance(code, CodeType):
        raise ValueError(f"Packaged module is not a code object: {source}")
    expected = compile(source.read_bytes(), code.co_filename, "exec", dont_inherit=True, optimize=1)
    if expected != code:
        raise ValueError(f"Packaged Python module differs from source: {source}")


def verify_python(repo: Path, package: Path) -> int:
    # Deferred import keeps fixture tests independent of build-only dependencies.
    from PyInstaller.archive.readers import CArchiveReader

    executable = CArchiveReader(str(package / "Nimda.exe"))
    modules = executable.open_embedded_archive("PYZ.pyz")
    verified = 0
    for backend in backend_roots(repo):
        for directory in sorted(backend.iterdir()):
            if not directory.is_dir() or not (directory / "__init__.py").is_file():
                continue
            for source in sorted(directory.rglob("*.py")):
                parts = list(source.relative_to(backend).with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts.pop()
                name = ".".join(parts)
                if name == "work_catalog_yaml.desktop":
                    code = marshal.loads(executable.extract("desktop"))
                elif name in modules.toc:
                    code = modules.extract(name)
                elif name in OPTIONAL_MODULES:
                    continue
                else:
                    raise ValueError(f"Required Python module missing from package: {name}")
                verify_code(source, code)
                verified += 1
    return verified


def verify_frontend(repo: Path, package: Path) -> int:
    roots = [repo / "apps" / "framework" / "frontend"]
    roots.extend(sorted((repo / "apps" / "features").glob("*/frontend")))
    expected = {file.relative_to(repo).as_posix(): file for root in roots for file in root.rglob("*") if file.is_file()}
    actual = {file.relative_to(package).as_posix(): file for file in (package / "apps").rglob("*") if file.is_file()}
    if expected.keys() != actual.keys():
        raise ValueError(f"Frontend file set differs (missing={sorted(expected.keys() - actual.keys())}, extra={sorted(actual.keys() - expected.keys())})")
    for name, source in expected.items():
        if digest_file(source) != digest_file(actual[name]):
            raise ValueError(f"Packaged frontend differs from source: {name}")
    return len(expected)


def verify_default_config(repo: Path, package: Path) -> None:
    relative = Path("work_catalog_yaml/jp_tv/browse_config.default.yaml")
    source = repo / "apps" / "framework" / "backend" / relative
    if digest_file(source) != digest_file(package / relative):
        raise ValueError("Packaged default configuration differs from source")


def verify_layout(package: Path, workspace: Path | None = None) -> None:
    for forbidden in ("config", "data"):
        if (package / forbidden).exists():
            raise ValueError(f"Package must not contain workspace data/config: {forbidden}")
    config = load_yaml_string((package / "nimda-desktop.yaml").read_text(encoding="utf-8"))
    paths = config.get("paths") if isinstance(config, dict) else None
    root = paths.get("workspace_root") if isinstance(paths, dict) else None
    if not isinstance(root, str) or not root.strip():
        raise ValueError("Desktop bootstrap must specify paths.workspace_root")
    configured = Path(root).expanduser()
    configured = (configured if configured.is_absolute() else package / configured).resolve()
    if workspace is not None and configured != workspace.resolve():
        raise ValueError(f"Desktop bootstrap points to a different workspace: {configured}")


def verify_archive(package: Path, archive: Path) -> int:
    expected = {file.relative_to(package.parent).as_posix(): file for file in package.rglob("*") if file.is_file()}
    with zipfile.ZipFile(archive) as zipped:
        entries = [entry for entry in zipped.infolist() if not entry.is_dir()]
        if len({entry.filename for entry in entries}) != len(entries):
            raise ValueError("Duplicate filenames in desktop ZIP")
        if {entry.filename for entry in entries} != expected.keys():
            raise ValueError("Desktop ZIP file set differs from the package directory")
        for entry in entries:
            file = expected[entry.filename]
            if entry.file_size != file.stat().st_size:
                raise ValueError(f"Desktop ZIP size differs: {entry.filename}")
            with zipped.open(entry) as stream:
                if digest_stream(stream) != digest_file(file):
                    raise ValueError(f"Desktop ZIP content differs: {entry.filename}")
    return len(expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=ROOT / "dist" / "Nimda")
    parser.add_argument("--archive", type=Path, default=ROOT / "dist" / "Nimda-Windows-x64.zip")
    parser.add_argument("--workspace", type=Path, help="expected shared workspace selected by the bootstrap")
    args = parser.parse_args()
    package = args.package.resolve()
    try:
        verify_layout(package, args.workspace)
        verify_default_config(ROOT, package)
        summary = {
            "python_modules": verify_python(ROOT, package),
            "frontend_files": verify_frontend(ROOT, package),
            "archive_files": verify_archive(package, args.archive),
            "exe_sha256": digest_file(package / "Nimda.exe"),
            "archive_sha256": digest_file(args.archive),
        }
    except (OSError, ValueError, KeyError, ImportError, zipfile.BadZipFile) as error:
        print(f"Desktop verification failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
