from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collection_detail.catalog_repository import CatalogRepository, resolve_catalog_directory
from work_catalog_yaml.yaml_io import load_yaml_string, dump_yaml_string
from test_jp_tv_link_index import _settings, _catalog_yaml_mapped


class CatalogRepositoryTest(unittest.TestCase):
    def test_document_and_work_projections_share_authoritative_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "media" / "Shared" / "BDRip"
            source = root / "[JP][TVInfo][2098].yaml"
            raw = load_yaml_string(_catalog_yaml_mapped("First Season", target.parent.as_posix(), "BDRip"))
            raw += load_yaml_string(_catalog_yaml_mapped("Second Season", target.parent.as_posix(), "BDRip"))
            raw[0]["custom"] = {"preserve": True}
            source.write_text(dump_yaml_string(raw), encoding="utf-8")
            repository = CatalogRepository(_settings(root, source))
            document = repository.read_document(source)
            self.assertEqual(document.source_sha256, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(document.document[0]["custom"], {"preserve": True})
            self.assertEqual(len(document.entries), 2)
            works = repository.load_works()
            self.assertEqual(len(works), 2, "Two DB records may point at one physical release")
            self.assertEqual(works[0]["path"], works[1]["path"])
            self.assertNotEqual(works[0]["work_key"], works[1]["work_key"])
            self.assertEqual(works[0]["begin_date"], "20980101")
            self.assertEqual(len(list(repository.read_documents())), 1)

    def test_repository_rejects_reading_a_file_outside_current_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            outside = root / "outside.yaml"
            outside.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "DB"):
                CatalogRepository(_settings(db)).read_document(outside)

    def test_catalog_directory_rejects_escape_and_absolute_release_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for value in ("../other", "/other", "C:/other", r"C:other"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    resolve_catalog_directory(root, "Work", value)
            with self.assertRaises(ValueError):
                resolve_catalog_directory(root, "Work/../Other", "BDRip")
            with self.assertRaisesRegex(ValueError, "不能为空"):
                resolve_catalog_directory(root, "Work", "")

    def test_repository_accepts_explicit_nested_source_but_rejects_outside_member(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            nested = db / "nested" / "works.yaml"
            nested.parent.mkdir(parents=True)
            nested.write_text("[]", encoding="utf-8")
            outside = root / "outside.yaml"
            outside.write_text("[]", encoding="utf-8")
            repository = CatalogRepository(_settings(db, nested, outside))
            self.assertEqual(repository.read_document(nested).relative_path, "nested/works.yaml")
            self.assertEqual(repository.catalog_paths(), [nested.resolve()])
            with self.assertRaises(ValueError):
                repository.read_source(outside)

    def test_repository_read_phase_caches_only_membership_not_document_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "works.yaml"
            source.write_text("[]", encoding="utf-8")
            with patch("collection_detail.catalog_repository._catalog_yaml_paths", return_value=[source]) as membership:
                repository = CatalogRepository(_settings(root, source))
                first = repository.read_source(source)
                source.write_text("- attributes: []", encoding="utf-8")
                second = repository.read_source(source)
                self.assertNotEqual(first.source_sha256, second.source_sha256)
                self.assertEqual(membership.call_count, 1)

    def test_catalog_directory_supports_work_only_and_lexical_disk_probing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch("collection_detail.catalog_repository.Path.resolve", side_effect=AssertionError("must not follow a disk link")):
                work = resolve_catalog_directory(root, "Work", resolve_links=False)
                release = resolve_catalog_directory(root, "Work", "BDRip", resolve_links=False)
            self.assertEqual(work, root / "Work")
            self.assertEqual(release, root / "Work" / "BDRip")


if __name__ == "__main__":
    unittest.main()
