from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from collection_detail import link_index, shortcut_service
from collection_detail.catalog_repository import CatalogRepository
from work_catalog_yaml.yaml_io import load_yaml_string, dump_yaml_string
import test_incremental_shortcuts as fixtures
from test_jp_tv_link_index import _catalog_yaml_mapped


class ScopedShortcutServiceTest(unittest.TestCase):
    setUp = fixtures.IncrementalShortcutTest.setUp
    create = staticmethod(fixtures.IncrementalShortcutTest.create)

    def refs(self, index: int = 0) -> list[dict]:
        work = CatalogRepository(self.settings).load_works()[index]
        return [{"work_key": work["work_key"], "press_key": work["press"][0]["press_key"]}]

    def test_single_scope_generates_only_selected_preserving_full_cache(self) -> None:
        cached = self.root / "index.yaml"
        cached.write_text("sentinel: full-index-do-not-replace\n", encoding="utf-8")
        before_db = self.source.read_bytes()
        before_cache = cached.read_bytes()
        refs = self.refs()
        plan = shortcut_service.preview_shortcuts(refs, settings=self.settings)
        self.assertEqual(plan["total"], 1)
        self.assertEqual(plan["scope"], refs)
        self.assertEqual(plan["items"][0]["work_key"], refs[0]["work_key"])
        self.assertEqual(plan["skipped_unbound_count"], 0)
        with patch.object(link_index, "_create_windows_shortcut", side_effect=self.create) as create:
            result = shortcut_service.apply_shortcuts(refs, plan["plan_id"], settings=self.settings)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(result["created"], 1)
        self.assertFalse(result["index_cache_updated"])
        self.assertEqual(cached.read_bytes(), before_cache)
        self.assertEqual(self.source.read_bytes(), before_db)
        self.assertEqual(self.keep.read_bytes(), b"keep")

    def test_empty_null_or_unknown_scope_cannot_expand_to_full_database(self) -> None:
        for refs in ([], None, [{"work_key": "forged", "press_key": "forged"}]):
            with self.subTest(refs=refs), self.assertRaises(ValueError):
                shortcut_service.preview_shortcuts(refs, settings=self.settings)
        self.assertFalse(list(self.finish.rglob("*.lnk")))

    def test_scope_change_rejects_previous_confirmation(self) -> None:
        plan = shortcut_service.preview_shortcuts(self.refs(0), settings=self.settings)
        with patch.object(link_index, "_create_windows_shortcut") as create:
            with self.assertRaisesRegex(ValueError, "重新预检"):
                shortcut_service.apply_shortcuts(self.refs(1), plan["plan_id"], settings=self.settings)
            create.assert_not_called()

    def test_unselected_record_destination_conflict_is_not_bypassed(self) -> None:
        docs = load_yaml_string(self.source.read_text(encoding="utf-8"))
        other = self.media / "Other" / "BDRip"
        other.mkdir(parents=True)
        docs += load_yaml_string(_catalog_yaml_mapped("Bound", other.parent.as_posix(), "BDRip"))
        self.source.write_text(dump_yaml_string(docs), encoding="utf-8")
        refs = self.refs()
        plan = shortcut_service.preview_shortcuts(refs, settings=self.settings)
        self.assertTrue(plan["conflict_count"])
        self.assertEqual(plan["conflicts"][0]["code"], "shortcut-scope-output-conflict")
        with patch.object(link_index, "_create_windows_shortcut") as create:
            with self.assertRaisesRegex(ValueError, "冲突"):
                shortcut_service.apply_shortcuts(refs, plan["plan_id"], settings=self.settings)
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
