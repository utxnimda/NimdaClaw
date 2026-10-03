from __future__ import annotations

import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from media_directory_organizer import landing, suggestions, web
from media_directory_organizer.catalog import CatalogWork, MediaCatalog, PressRecord
from media_directory_organizer.service import build_plan
from media_directory_organizer.settings import OrganizerSettings, load_organizer_settings
from work_catalog_yaml.paths import normalize_copied_path
from work_catalog_yaml.yaml_io import dump_yaml_string


class CopiedWorkRootTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.resources = self.base / "resources"
        self.root = self.resources / "Gun x Sword"
        self.source = self.root / "Gun x Sword_BDRip(VCB)"
        self.source.mkdir(parents=True)
        self.media = self.source / "Gun x Sword 01.mkv"
        self.media.write_bytes(b"synthetic-media")
        self.catalog_root = self.base / "catalog"
        self.catalog_root.mkdir()
        self.settings = OrganizerSettings(
            catalog_root=self.catalog_root, allowed_resource_roots=(self.resources,),
            format_markers={"BDRip": ("bdrip",)}, group_markers={"VCB": ("vcb",)},
            group_suffixes={"VCB": "VCB"}, default_work_root=self.root,
        )
        self.catalog = MediaCatalog(works=(CatalogWork(
            name="Gun x Sword", path=str(self.root), domain="animation", country="japan",
            release_type="tv", presses=(PressRecord("BDRip", "VCB", self.source.name),),
            source_file=str(self.catalog_root / "fixture.yaml"),
        ),), catalog_root=self.catalog_root)

    def copied(self, path: Path | None = None) -> str:
        return '\u202a "\u2066' + str(path or self.root) + '\u2069"\u202c '

    def test_user_windows_path_becomes_absolute_before_resolution(self) -> None:
        raw = "\u202aU:\\Gun x Sword"
        self.assertFalse(PureWindowsPath(raw).is_absolute())
        cleaned = normalize_copied_path(raw)
        self.assertEqual(cleaned, "U:\\Gun x Sword")
        self.assertTrue(PureWindowsPath(cleaned).is_absolute())
        self.assertEqual(web._root_from_body({"root": raw}, self.settings), cleaned)

    def test_direct_preview_has_identical_plan_for_clean_and_copied_roots(self) -> None:
        before = [(str(path.relative_to(self.base)), path.is_dir()) for path in self.base.rglob("*")]
        expected = build_plan(self.root, catalog=self.catalog, settings=self.settings)
        expected.pop("created_at")
        for raw in (self.copied(), Path(self.copied())):
            with self.subTest(raw=str(raw)):
                actual = build_plan(raw, catalog=self.catalog, settings=self.settings)
                actual.pop("created_at")
                self.assertEqual(actual, expected)
                self.assertEqual(actual["root"], str(self.root))
        self.assertEqual(self.media.read_bytes(), b"synthetic-media")
        self.assertEqual([(str(path.relative_to(self.base)), path.is_dir()) for path in self.base.rglob("*")], before)

    def test_ui_preview_normalizes_before_catalog_matching_and_source_binding(self) -> None:
        with (
            patch.object(web.MediaCatalog, "load", return_value=self.catalog),
            patch.object(web, "preview_organizer_plan_shortcuts", side_effect=lambda plan, **_: plan),
            patch.object(web, "shortcut_repair_descriptor_for_plan", return_value=None),
        ):
            clean = web.preview_organizer_from_ui_body({"root": str(self.root)}, settings=self.settings)
            copied = web.preview_organizer_from_ui_body({"root": self.copied()}, settings=self.settings)
        clean["plan"].pop("created_at")
        copied["plan"].pop("created_at")
        self.assertEqual(copied, clean)
        self.assertFalse(copied["plan"].get("registration_required"))

    def test_registration_and_suggestion_root_validation_use_the_same_clean_path(self) -> None:
        self.assertEqual(landing._validated_root(self.copied(), self.settings), self.root)
        self.assertEqual(suggestions._validated_root(self.copied(), self.settings), self.root)
        empty_catalog = MediaCatalog(works=(), catalog_root=self.catalog_root)
        with patch.object(web.MediaCatalog, "load", return_value=empty_catalog):
            plan = web.preview_organizer_from_ui_body({"root": self.copied()}, settings=self.settings)["plan"]
        self.assertTrue(plan["registration_required"])
        self.assertEqual(plan["root"], str(self.root))
        self.assertEqual(self.media.read_bytes(), b"synthetic-media")

    def test_wrapper_only_inputs_are_rejected_instead_of_resolving_to_cwd_or_default(self) -> None:
        for raw in ('\u202a""\u202c', "\u2066\u2069", '\ufeff " " '):
            for validate in (landing._validated_root, suggestions._validated_root):
                with self.subTest(raw=repr(raw), validator=validate.__module__), self.assertRaises(ValueError):
                    validate(raw, self.settings)
            with self.assertRaises(ValueError):
                web._root_from_body({"root": raw}, self.settings)
            with self.assertRaises(ValueError):
                build_plan(raw, catalog=self.catalog, settings=self.settings)
        self.assertEqual(web._root_from_body({}, self.settings), str(self.root))

    def test_normalization_does_not_bypass_allowed_resource_roots_or_traversal_checks(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        for raw in (self.copied(outside), self.copied(self.root / ".." / ".." / "outside")):
            for validate in (landing._validated_root, suggestions._validated_root):
                with self.subTest(validator=validate.__module__), self.assertRaisesRegex(ValueError, "资源库范围"):
                    validate(raw, self.settings)
            with self.assertRaisesRegex(ValueError, "资源库范围"):
                build_plan(raw, catalog=self.catalog, settings=self.settings)

    def test_interior_spaces_and_unicode_in_real_directory_names_are_preserved(self) -> None:
        interior = self.resources / "Gun  x\u202a Sword"
        interior.mkdir()
        self.assertEqual(landing._validated_root(self.copied(interior), self.settings), interior)

    def test_config_paths_clean_copy_wrappers_without_changing_workspace_relative_base(self) -> None:
        config = self.base / "organizer.yaml"
        config.write_text(dump_yaml_string({"paths": {
            "catalog_root": '\u202a"catalog"\u202c',
            "allowed_resource_roots": ['\u2066"resources"\u2069'],
            "default_work_root": self.copied(),
        }}), encoding="utf-8")
        with patch("work_catalog_yaml.layout.workspace_root", return_value=self.base):
            settings = load_organizer_settings(config)
        self.assertEqual(settings.catalog_root, self.catalog_root)
        self.assertEqual(settings.allowed_resource_roots, (self.resources,))
        self.assertEqual(settings.default_work_root, self.root)

    def test_wrapper_only_allowed_root_never_becomes_unrestricted_access(self) -> None:
        config = self.base / "organizer.yaml"
        config.write_text(dump_yaml_string({"paths": {"allowed_resource_roots": ['\u202a""']}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "allowed_resource_roots"):
            load_organizer_settings(config)


if __name__ == "__main__":
    unittest.main()
