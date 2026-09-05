from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_catalog_yaml.catalog import Catalog, CatalogFile, materialize_catalog
from work_catalog_yaml.cli import _cmd_jp_tv_materialize
from work_catalog_yaml.jp_tv.browse_settings import load_jp_tv_browse_settings
from work_catalog_yaml.layout import (
    backend_roots,
    default_source_data_dir,
    resolve_workspace_path,
    workspace_parsed_yaml_dir,
    workspace_root,
)
from work_catalog_yaml.paths import resolve_output_path


class WorkspaceLayoutTest(unittest.TestCase):
    def test_renamed_workspace_and_missing_default_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "renamed-project"
            backend = root / "apps" / "framework" / "backend"
            backend.mkdir(parents=True)
            (root / "config").mkdir()
            (root / "data").mkdir()
            with patch.dict(os.environ, {"NIMDA_WORKSPACE_ROOT": ""}), patch(
                "work_catalog_yaml.layout.code_repo_root", return_value=backend
            ):
                self.assertEqual(workspace_root(), root)
                self.assertEqual(default_source_data_dir(), root / "data" / "source")
                self.assertEqual(workspace_parsed_yaml_dir(), root / "data/features/collection-detail/db")

    def test_configured_relative_paths_do_not_depend_on_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            database = root / "data" / "catalog"
            database.mkdir(parents=True)
            (database / "works.yaml").write_text("[]\n", encoding="utf-8")
            config = root / "config" / "browse.yaml"
            config.write_text("paths:\n  filesystem_root: data/catalog\n", encoding="utf-8")
            with patch.dict(os.environ, {"NIMDA_WORKSPACE_ROOT": str(root)}):
                settings = load_jp_tv_browse_settings(config)
                self.assertEqual(settings.filesystem_root, database.resolve())
                self.assertEqual(settings.resolved_catalog_yaml_paths, (str((database / "works.yaml").resolve()),))
                self.assertEqual(resolve_workspace_path("~/Nimda"), (Path.home() / "Nimda").resolve())
                with self.assertRaisesRegex(ValueError, "驱动器相对路径"):
                    resolve_workspace_path("S:media")

    def test_backend_discovery_includes_new_features_without_a_fixed_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            framework = root / "apps/framework/backend"
            feature = root / "apps/features/new-feature/backend"
            framework.mkdir(parents=True)
            feature.mkdir(parents=True)
            (root / "apps/features/frontend-only/frontend").mkdir(parents=True)
            self.assertEqual(backend_roots(root), (framework.resolve(), feature.resolve()))


class ExportPathTest(unittest.TestCase):
    def test_valid_export_preserves_content_and_windows_separators(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "Data" / "日本" / "works.txt"
            result = materialize_catalog(Catalog(files=[CatalogFile("日本\\works.txt", "作品\n")]), td)
            self.assertEqual(result, [target])
            self.assertEqual(target.read_text(encoding="utf-8"), "作品\n")

    def test_unsafe_batch_is_rejected_before_any_file_is_written(self) -> None:
        for path in ("../escape.txt", "nested/../../escape.txt", "/absolute.txt", "C:\\escape.txt", "C:escape.txt", "\\\\server\\share\\file", "file.txt:stream"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                catalog = Catalog(files=[CatalogFile("valid.txt", "first"), CatalogFile(path, "bad")])
                with self.assertRaises(ValueError):
                    materialize_catalog(catalog, root)
                self.assertEqual(list(root.iterdir()), [])

    def test_absolute_catalog_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                materialize_catalog(Catalog(root="/", files=[CatalogFile("file.txt", "bad")]), td)
            self.assertEqual(list(Path(td).iterdir()), [])

    def test_duplicate_and_file_directory_conflicts_are_preflighted(self) -> None:
        for paths in (("same.txt", "./same.txt"), ("parent", "parent/child.txt")):
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(ValueError):
                    materialize_catalog(Catalog(files=[CatalogFile(p, "value") for p in paths]), td)
                self.assertEqual(list(Path(td).iterdir()), [])

    def test_existing_directory_conflict_does_not_partially_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Data" / "occupied").mkdir(parents=True)
            existing = root / "Data" / "first.txt"
            existing.write_text("original", encoding="utf-8")
            with self.assertRaises(ValueError):
                materialize_catalog(Catalog(files=[CatalogFile("first.txt", "new"), CatalogFile("occupied", "bad")]), root)
            self.assertEqual(existing.read_text(encoding="utf-8"), "original")

    def test_resolved_path_cannot_escape_through_a_link(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "output"
            outside = Path(td) / "outside"
            root.mkdir()
            outside.mkdir()
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Creating symlinks is not permitted on this host")
            with self.assertRaises(ValueError):
                resolve_output_path(root, "linked/file.txt")

    def test_jp_tv_export_uses_the_same_path_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = argparse.Namespace(input="unused.yaml", output=td, relative_path="../escape.txt", data_root="Data")
            with patch("work_catalog_yaml.cli.load_jp_tv_yaml_file", return_value=[]):
                with self.assertRaises(ValueError):
                    _cmd_jp_tv_materialize(args)
            self.assertEqual(list(Path(td).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
