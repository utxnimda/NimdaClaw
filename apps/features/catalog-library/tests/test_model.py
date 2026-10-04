from __future__ import annotations

import unittest
from catalog_library.model import classification_definition, new_work_id, validate_extensions


class CatalogLibraryModelTest(unittest.TestCase):
    def test_work_ids_are_distinct_and_not_derived_from_title(self):
        first, second = new_work_id(), new_work_id()
        self.assertRegex(first, r"^work_[0-9a-f]{32}$")
        self.assertNotEqual(first, second)

    def test_legacy_record_is_not_implicitly_migrated(self):
        record = {"attributes": [], "custom": {"keep": True}}
        self.assertEqual(validate_extensions(record), record)
        self.assertNotIn("id", record)

    def test_invalid_extension_values_are_rejected(self):
        for record in ({"id": "bangumi:1"}, {"schema_version": True}, {"schema_version": 2},
                       {"metadata": []}, {"metadata": {"aliases": [1]}},
                       {"metadata": {"episodes": [1]}}, {"source_refs": ["bad"]},
                       {"source_refs": [{"provider": "bangumi", "external_id": None}]},
                       {"classifications": [{"type": "series", "value_id": "x"}]},
                       {"classifications": [{"type": "series", "value_id": "classification_" + "a" * 32, "order": float("inf")}]},
                       {"classifications": [{"type": "series", "value_id": "classification_" + "a" * 32, "order": True}]}):
            with self.subTest(record=record), self.assertRaises(ValueError):
                validate_extensions(record)

    def test_provider_metadata_can_extend_without_schema_lock_in(self):
        record = {"id": new_work_id(), "metadata": {"summary": "Local", "episodes": [{"name": "One"}],
                   "tracks": [{"title": "Track"}], "field_sources": {"summary": {"provider": "example"}}},
                   "source_refs": [{"provider": "example", "external_id": "123", "scope": "01-12"}]}
        self.assertEqual(validate_extensions(record), record)

    def test_classification_rename_keeps_identity_and_type(self):
        original = classification_definition({"name": "Fate"})
        renamed = classification_definition({"name": "Fate Series"}, existing=original)
        self.assertEqual(renamed["id"], original["id"])
        with self.assertRaises(ValueError):
            classification_definition({"name": "Fate", "type": "topic"}, existing=original)


if __name__ == "__main__":
    unittest.main()
