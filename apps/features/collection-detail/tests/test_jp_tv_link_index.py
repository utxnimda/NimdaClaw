from __future__ import annotations

import base64
import json
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from collection_detail import link_index as link_index_mod
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.link_index import (
    collection_link_index_payload,
    generate_link_index_files_from_ui_body,
    generate_link_index_from_ui_body,
    open_link_index_path_from_ui_body,
    preview_link_index_from_ui_body,
    resource_libraries_cached_payload,
    resource_libraries_node_payload,
    save_resource_library_roots_from_ui_body,
    scan_resource_libraries_payload,
    save_link_index_from_ui_body,
)
from work_catalog_yaml.yaml_io import load_yaml, dump_yaml_string
from work_catalog_yaml import persistence


def _settings(db: Path, *paths: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1,
        filesystem_root=db,
        resolved_default_readable=str(paths[0]) if paths else None,
        resolved_catalog_yaml_paths=tuple(str(p) for p in paths),
        enum_options={},
        enum_labels={
            "domain": {"animation": "动画"},
            "country": {"japan": "日本"},
            "release_type": {"tv": "TV"},
        },
        enum_section_labels={},
        app_features=(),
    )


def _empty_binding_plan() -> dict:
    return {"mappings": [], "issues": [], "summary": {"unbound_candidate_count": 0, "blocking_issue_count": 0}}


def _catalog_yaml() -> str:
    return (
        textwrap.dedent(
            """
            - attributes:
              - type: date
                data:
                  start: "20990101"
                  end: "20990331"
              - type: collection-type
                data:
                  domain: animation
                  release_type: tv
                  collectioned:
                  - press_format: "BDRip"
                    press_group: "VCB"
                  - press_format: "720p"
                    press_group: "DMG"
                  markers: []
              - type: country
                data: japan
              - type: name
                data: Absolute Duo
            """
        ).strip()
        + "\n"
    )


def _catalog_yaml_mapped(name: str, work_path: str, press_path: str) -> str:
    return (
        textwrap.dedent(
            f"""
            - attributes:
              - type: date
                data:
                  start: "20980101"
                  end: "20980331"
              - type: collection-type
                data:
                  domain: animation
                  release_type: tv
                  path: "{work_path}"
                  collectioned:
                  - press_format: "BDRip"
                    press_group: "VCB"
                    press_path: "{press_path}"
                  markers: []
              - type: country
                data: japan
              - type: name
                data: {name}
            """
        ).strip()
        + "\n"
    )


def _catalog_yaml_with_press(name: str, press_rows: list[tuple[str, str]]) -> str:
    return _catalog_yaml_with_press_dates(name, press_rows, "20990101", "20990331")


def _catalog_yaml_with_press_dates(name: str, press_rows: list[tuple[str, str]], start: str, end: str) -> str:
    rows = "\n".join(
        f"                  - press_format: \"{fmt}\"\n                    press_group: \"{grp}\""
        for fmt, grp in press_rows
    )
    return (
        textwrap.dedent(
            f"""
            - attributes:
              - type: date
                data:
                  start: "{start}"
                  end: "{end}"
              - type: collection-type
                data:
                  domain: animation
                  release_type: tv
                  collectioned:
{rows}
                  markers: []
              - type: country
                data: japan
              - type: name
                data: {name}
            """
        ).strip()
        + "\n"
    )


def _binding_case(root: Path) -> tuple[Path, JpTvBrowseSettings, dict, Path, list[dict]]:
    db = root / "db"
    db.mkdir()
    target = root / "media" / "Binding Work" / "Binding Work_BDRip(VCB)"
    target.mkdir(parents=True)
    fp = db / "[JP][TVInfo][2099].yaml"
    fp.write_text(_catalog_yaml_with_press("Binding Work", [("BDRip", "VCB")]), encoding="utf-8")
    cfg = {"paths": {"resource_roots": [str(root / "media")], "shortcut_root": str(root / "finish")}}
    resources = [{"root": str(root / "media"), "path": str(target), "series_name": "Binding Work",
                  "work_name": "Binding Work", "name": target.name, "press_info": "BDRip(VCB)"}]
    return fp, _settings(db, fp), cfg, target, resources


def _resource_fixture_rows(root: Path, names: list[str]) -> list[dict]:
    rows = []
    for name in names:
        target = root / name
        target.mkdir(parents=True)
        work_name, press_info = link_index_mod._split_resource_leaf_name(name)
        rows.append({"root": str(root), "path": str(target), "name": name,
                     "work_name": work_name, "press_info": press_info})
    return rows


def _find_link_by_relpath(node: dict, relpath: str) -> dict | None:
    if node.get("type") == "link":
        node_relpath = str(node.get("relpath") or "")
        if node_relpath == relpath:
            return node
        bracket = relpath.find("[")
        if bracket >= 0 and node_relpath.endswith(relpath[bracket:]):
            return node
    for child in node.get("children", []) or []:
        found = _find_link_by_relpath(child, relpath)
        if found:
            return found
    return None


def _find_link_by_relpath_suffix(node: dict, suffix: str) -> dict | None:
    if node.get("type") == "link" and str(node.get("relpath") or "").endswith(suffix):
        return node
    for child in node.get("children", []) or []:
        found = _find_link_by_relpath_suffix(child, suffix)
        if found:
            return found
    return None


class JpTvLinkIndexTest(unittest.TestCase):
    def test_rebuild_rejects_shortcut_collisions_before_clearing_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db, finish = root / "DB", root / "Finish"
            db.mkdir()
            finish.mkdir()
            existing = finish / "keep.txt"
            existing.write_text("keep", encoding="utf-8")
            targets = [root / "A", root / "B"]
            for target in targets:
                target.mkdir()
            items = [{"shortcut_root": str(finish), "shortcut_relpath": "same.lnk", "target_path": str(target)} for target in targets]
            with (
                patch.object(link_index_mod, "_feature_config", return_value={"paths": {"shortcut_root": str(finish)}}),
                patch.object(link_index_mod, "_catalog_binding_generation_context", return_value=([], items, True, _empty_binding_plan())),
                patch.object(link_index_mod, "_create_windows_shortcut") as create,
            ):
                preview = generate_link_index_files_from_ui_body({"preview": True}, settings=_settings(db))
                self.assertEqual(preview["file_generation"]["conflict_count"], 1)
                with self.assertRaisesRegex(ValueError, "同一个快捷方式路径"):
                    generate_link_index_files_from_ui_body({"confirm_clear": True, "confirm_clear_twice": True}, settings=_settings(db))
                create.assert_not_called()
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")



    def setUp(self) -> None:
        self._index_db_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._index_db_temp.cleanup)
        self._index_db_path_patch = patch.object(
            link_index_mod,
            "_link_index_db_path",
            return_value=Path(self._index_db_temp.name) / "link-index.yaml",
        )
        self._index_db_path_patch.start()
        self.addCleanup(self._index_db_path_patch.stop)
        self._feature_data_root_patch = patch.object(
            link_index_mod,
            "feature_data_root",
            return_value=Path(self._index_db_temp.name) / "feature-data",
        )
        self._feature_data_root_patch.start()
        self.addCleanup(self._feature_data_root_patch.stop)
        default_config = patch.object(link_index_mod, "_feature_config", return_value={
            "paths": {"media_root": str(Path(self._index_db_temp.name) / "media"),
                      "shortcut_root": str(Path(self._index_db_temp.name) / "finish")}
        })
        default_config.start()
        self._default_config_patch = default_config
        self.addCleanup(default_config.stop)
        self._actual_shortcut_targets = link_index_mod._windows_shortcut_targets
        target_reader = patch.object(link_index_mod, "_windows_shortcut_targets", return_value={})
        target_reader.start()
        self.addCleanup(target_reader.stop)
        link_index_mod._SHORTCUT_SCAN_CACHE["signature"] = None
        link_index_mod._SHORTCUT_SCAN_CACHE["leaves"] = []
        link_index_mod._FEATURE_CONFIG_CACHE["signature"] = None
        link_index_mod._FEATURE_CONFIG_CACHE["data"] = None

    def test_index_cache_files_are_isolated_from_the_application_workspace(self) -> None:
        isolated = Path(self._index_db_temp.name).resolve()
        for cache_path in (
            link_index_mod._shortcut_scan_cache_path(),
            link_index_mod._resource_scan_cache_path(),
            link_index_mod._resource_scan_node_dir(),
        ):
            self.assertTrue(cache_path.is_relative_to(isolated))

    def test_generate_index_never_auto_binds_catalog_from_resource_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fp, settings, cfg, target, resources = _binding_case(root)
            doc = load_yaml(fp)
            doc[0]["custom"] = {"keep": [1, 2, 3]}
            fp.write_text(dump_yaml_string(doc), encoding="utf-8")
            before = fp.read_bytes()
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "_resource_fix_items_from_cache", return_value=resources,
            ) as resource_cache, patch.object(link_index_mod, "_create_windows_shortcut") as create:
                preview = generate_link_index_from_ui_body({"preview": True}, settings=settings)
                self.assertEqual(preview["catalog_bindings"]["summary"]["mapped_press_count"], 0)
                self.assertTrue(any(issue["code"] == "missing-catalog-binding" for issue in preview["catalog_bindings"]["issues"]))
                result = generate_link_index_from_ui_body({}, settings=settings)
                create.assert_not_called()
                resource_cache.assert_not_called()
            self.assertEqual(fp.read_bytes(), before)
            self.assertEqual(result["writes"], [])
            self.assertEqual(result["generated"][0]["target_path"], "")
            self.assertEqual(load_yaml(fp)[0]["custom"], {"keep": [1, 2, 3]})

    def test_shortcut_generation_waits_for_explicit_catalog_binding(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fp, settings, cfg, target, resources = _binding_case(root)
            before = fp.read_bytes()
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "_resource_fix_items_from_cache", return_value=resources,
            ), patch.object(link_index_mod, "_create_windows_shortcut") as create:
                preview = generate_link_index_files_from_ui_body({"preview": True, "incremental": True}, settings=settings)["file_generation"]
                self.assertEqual(preview["creatable"], 0)
                self.assertEqual(preview["skipped_unbound_count"], 1)
                result = generate_link_index_files_from_ui_body({
                    "incremental": True, "confirm_incremental": True, "plan_id": preview["plan_id"],
                }, settings=settings)
                self.assertEqual(result["file_generation"]["created"], 0)
                self.assertEqual(fp.read_bytes(), before)
                self.assertFalse((root / "finish").exists())
                create.assert_not_called()
                entry = link_index_mod._load_link_index_db()["items"][0]
                link_index_mod.apply_link_index_target_fixes_from_ui_body({"refresh_payload": False, "items": [{
                    "source": "index_db", "entry_key": entry["entry_key"], "target_path": str(target),
                }]}, settings=settings)
                bound_bytes = fp.read_bytes()
                preview = generate_link_index_files_from_ui_body({"preview": True, "incremental": True}, settings=settings)["file_generation"]
                self.assertEqual(preview["creatable"], 1)
                result = generate_link_index_files_from_ui_body({
                    "incremental": True, "confirm_incremental": True, "plan_id": preview["plan_id"],
                }, settings=settings)
                self.assertEqual(result["file_generation"]["created"], 1)
                self.assertEqual(fp.read_bytes(), bound_bytes)
                create.assert_called_once()

    def test_unrelated_index_target_is_blocked_before_catalog_index_or_output_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fp, settings, cfg, _, _ = _binding_case(root)
            wrong = root / "media" / "Different Work" / "Different Work_BDRip(VCB)"
            wrong.mkdir(parents=True)
            finish = root / "finish"
            finish.mkdir()
            keep = finish / "keep.lnk"
            keep.write_bytes(b"untouched")
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "_resource_fix_items_from_cache", return_value=[],
            ), patch.object(link_index_mod, "_clear_directory_contents") as clear, patch.object(link_index_mod, "_create_windows_shortcut") as create:
                entries = link_index_mod._index_entries_from_works(link_index_mod._load_catalog_works(settings))
                entries[0].update({"target_path": str(wrong), "target_source": "index_db_previous", "target_exists": True})
                link_index_mod._save_index_entries(entries, catalog_root=link_index_mod._settings_catalog_root_key(settings))
                before_catalog, before_index = fp.read_bytes(), link_index_mod._link_index_db_path().read_bytes()
                preview = generate_link_index_files_from_ui_body({"preview": True}, settings=settings)["file_generation"]
                self.assertGreater(preview["blocked_count"], 0)
                self.assertEqual(preview["creatable"], 0)
                with self.assertRaisesRegex(ValueError, "冲突"):
                    generate_link_index_files_from_ui_body({"confirm_clear": True, "confirm_clear_twice": True}, settings=settings)
                clear.assert_not_called()
                create.assert_not_called()
                self.assertEqual(fp.read_bytes(), before_catalog)
                self.assertEqual(link_index_mod._link_index_db_path().read_bytes(), before_index)
                self.assertEqual(keep.read_bytes(), b"untouched")

    def test_manual_target_fix_lands_in_catalog_and_index_without_touching_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fp, settings, cfg, target, _ = _binding_case(root)
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "_resource_fix_items_from_cache", return_value=[],
            ), patch.object(link_index_mod, "_create_windows_shortcut") as create:
                entries = link_index_mod._index_entries_from_works(link_index_mod._load_catalog_works(settings))
                link_index_mod._save_index_entries(entries, catalog_root=link_index_mod._settings_catalog_root_key(settings))
                result = link_index_mod.apply_link_index_target_fixes_from_ui_body({"refresh_payload": False, "items": [{
                    "source": "index_db", "entry_key": entries[0]["entry_key"], "target_path": str(target),
                }]}, settings=settings)
                create.assert_not_called()
                indexed = load_yaml(link_index_mod._link_index_db_path())["items"][0]
                self.assertEqual(indexed["target_source"], "catalog")
                self.assertEqual(Path(indexed["target_path"]), target)
                self.assertEqual(len(result["writes"]), 1)
                collection = load_yaml(fp)[0]["attributes"][1]["data"]
                self.assertEqual(Path(collection["path"]), target.parent)
                self.assertEqual(collection["collectioned"][0]["press_path"], target.name)

    def test_manual_target_conflicting_with_valid_catalog_is_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fp, settings, cfg, target, resources = _binding_case(root)
            other = target.parent / "Other_BDRip(VCB)"
            other.mkdir()
            fp.write_text(_catalog_yaml_mapped("Binding Work", target.parent.as_posix(), target.name), encoding="utf-8")
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "_resource_fix_items_from_cache", return_value=resources,
            ), patch.object(link_index_mod, "_create_windows_shortcut") as create:
                generated = generate_link_index_from_ui_body({}, settings=settings)
                before_catalog, before_index = fp.read_bytes(), link_index_mod._link_index_db_path().read_bytes()
                with self.assertRaisesRegex(ValueError, "已有有效数据库.*冲突"):
                    link_index_mod.apply_link_index_target_fixes_from_ui_body({"items": [{
                        "source": "index_db", "entry_key": generated["generated"][0]["entry_key"], "target_path": str(other),
                    }]}, settings=settings)
                self.assertEqual(fp.read_bytes(), before_catalog)
                self.assertEqual(link_index_mod._link_index_db_path().read_bytes(), before_index)
                create.assert_not_called()

    def test_index_commit_failure_never_changes_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fp, settings, cfg, _, resources = _binding_case(root)
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "_resource_fix_items_from_cache", return_value=resources,
            ):
                link_index_mod._save_link_index_db({"items": [], "catalog_root": link_index_mod._settings_catalog_root_key(settings)})
                index_path = link_index_mod._link_index_db_path()
                before_catalog, before_index = fp.read_bytes(), index_path.read_bytes()
                original_write = persistence.atomic_write_bytes

                def fail_index(target: Path, content: bytes) -> None:
                    if target == index_path:
                        raise OSError("simulated index commit failure")
                    original_write(target, content)

                with patch.object(link_index_mod, "_save_link_index_db", side_effect=OSError("simulated index commit failure")):
                    with self.assertRaisesRegex(OSError, "index commit failure"):
                        generate_link_index_from_ui_body({}, settings=settings)
                self.assertEqual(fp.read_bytes(), before_catalog)
                self.assertEqual(index_path.read_bytes(), before_index)

    def test_repairing_one_work_preserves_other_index_evidence_but_never_creates_unbound_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fp, settings, cfg, target, _ = _binding_case(root)
            other_target = root / "media" / "Unbound Work" / "Unbound Work_BDRip"
            other_target.mkdir(parents=True)
            doc = load_yaml(fp)
            doc.extend(link_index_mod.load_yaml_string(_catalog_yaml_with_press("Unbound Work", [("BDRip", "VCB")])))
            fp.write_text(dump_yaml_string(doc), encoding="utf-8")
            created = []
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "_resource_fix_items_from_cache", return_value=[],
            ), patch.object(link_index_mod, "_create_windows_shortcut", side_effect=lambda link, destination: created.append(destination)):
                entries = link_index_mod._index_entries_from_works(link_index_mod._load_catalog_works(settings))
                entries[1].update({"target_path": str(other_target), "target_source": "resource_format_fallback", "target_exists": True})
                link_index_mod._save_index_entries(entries, catalog_root=link_index_mod._settings_catalog_root_key(settings))
                index_path = link_index_mod._link_index_db_path()
                original_index = index_path.read_bytes()
                link_index_mod.apply_link_index_target_fixes_from_ui_body({"refresh_payload": False, "items": [{
                    "source": "index_db", "entry_key": entries[0]["entry_key"], "target_path": str(target),
                }]}, settings=settings)
                persisted = load_yaml(index_path)["items"]
                self.assertEqual(persisted[0]["target_source"], "catalog")
                self.assertEqual(Path(persisted[1]["target_path"]), other_target)
                self.assertEqual(persisted[1]["target_source"], "resource_format_fallback", "do not launder unconfirmed matching evidence")
                history = list((root / "History").glob("link-index__saved-*.yaml"))
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0].read_bytes(), original_index)
                preview = generate_link_index_files_from_ui_body({"preview": True, "incremental": True}, settings=settings)["file_generation"]
                self.assertEqual(preview["creatable"], 1)
                self.assertEqual(preview["skipped_unbound_count"], 1)
                self.assertTrue(any(issue["code"] == "missing-catalog-binding" for issue in preview["catalog_bindings"]["issues"]))
                result = generate_link_index_files_from_ui_body({"incremental": True, "confirm_incremental": True, "plan_id": preview["plan_id"]}, settings=settings)
                self.assertEqual(result["file_generation"]["created"], 1)
                self.assertEqual(created, [target])
                self.assertEqual(load_yaml(index_path)["items"][1]["target_path"], "")
                self.assertNotIn("path", load_yaml(fp)[1]["attributes"][1]["data"])

    def test_previous_index_target_requires_complete_work_and_press_identity_at_every_layer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fp, settings, cfg, target, _ = _binding_case(Path(td))
            with patch.object(link_index_mod, "_feature_config", return_value=cfg):
                works = link_index_mod._load_catalog_works(settings)
                original = link_index_mod._index_entries_from_works(works)[0]
                original.update({"target_path": str(target), "target_source": "index_db_previous"})
                substitutions = {
                    "yaml_source_rel": "different.yaml", "index_in_file": 7,
                    "work_key": "different.yaml#7", "name": "Different Work", "country": "korea",
                    "domain": "television", "release_type": "ova", "begin_date": "20980101", "end_date": "20980331",
                    "press_key": "0:main::BDRip:Other", "press_format": "DVDRip", "press_group": "Other",
                }
                for field, value in substitutions.items():
                    with self.subTest(field=field):
                        mismatched = {**original, field: value}
                        previous = {"catalog_root": link_index_mod._settings_catalog_root_key(settings), "items": [mismatched]}
                        self.assertEqual(link_index_mod._matching_previous_index_items(works, previous, catalog_root=previous["catalog_root"]), [])
                        regenerated = link_index_mod._index_entries_from_works(works, previous_items=[mismatched], prefer_previous_target=True)
                        self.assertEqual(regenerated[0]["target_path"], "")
                        direct = link_index_mod._index_entry_from_work_press(works[0], works[0]["press"][0], [], previous=mismatched, prefer_previous_target=True)
                        self.assertEqual(direct["target_path"], "")
                        self.assertEqual(mismatched[field], value, "validation must not rewrite the evidence")
                for invalid_index in (False, True, "0", 0.0):
                    with self.subTest(index_type=type(invalid_index).__name__, value=invalid_index):
                        self.assertEqual(link_index_mod._index_entries_from_works(
                            works, previous_items=[{**original, "index_in_file": invalid_index}], prefer_previous_target=True,
                        )[0]["target_path"], "")
                self.assertEqual(link_index_mod._index_entries_from_works(
                    works, previous_items=[original], prefer_previous_target=True,
                )[0]["target_path"], str(target))

    def test_duplicate_previous_entry_keys_are_rejected_as_a_group_even_when_one_identity_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, settings, cfg, target, _ = _binding_case(Path(td))
            with patch.object(link_index_mod, "_feature_config", return_value=cfg):
                works = link_index_mod._load_catalog_works(settings)
                entry = link_index_mod._index_entries_from_works(works)[0]
                entry.update({"target_path": str(target), "target_source": "index_db_previous"})
                for duplicate in (dict(entry), {**entry, "press_group": "Wrong"}, {**entry, "target_path": str(target.parent)}):
                    with self.subTest(duplicate=duplicate):
                        raw = [entry, duplicate]
                        previous = {"catalog_root": link_index_mod._settings_catalog_root_key(settings), "items": raw}
                        self.assertEqual(link_index_mod._matching_previous_index_items(works, previous, catalog_root=previous["catalog_root"]), [])
                        regenerated = link_index_mod._index_entries_from_works(works, previous_items=raw, prefer_previous_target=True)
                        self.assertEqual(regenerated[0]["target_path"], "")
                        _, issues = link_index_mod._validated_previous_index_items(works, raw)
                        self.assertEqual(issues[0]["code"], "history-index-duplicate")

    def test_historical_identity_conflicts_cannot_be_hidden_by_a_fresh_resource_candidate(self) -> None:
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                fp, settings, cfg, target, resources = _binding_case(root)
                with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                    link_index_mod, "_resource_fix_items_from_cache", return_value=resources,
                ), patch.object(link_index_mod, "_create_windows_shortcut") as create, patch.object(link_index_mod, "_clear_directory_contents") as clear:
                    entry = link_index_mod._index_entries_from_works(link_index_mod._load_catalog_works(settings))[0]
                    entry.update({"target_path": str(target), "target_source": "index_db_previous"})
                    wrong = {**entry, "yaml_source_rel": "wrong.yaml", "press_group": "Wrong"}
                    previous = [entry, wrong] if duplicate else [wrong]
                    link_index_mod._save_index_entries(previous, catalog_root=link_index_mod._settings_catalog_root_key(settings))
                    before_catalog, before_index = fp.read_bytes(), link_index_mod._link_index_db_path().read_bytes()
                    preview = generate_link_index_files_from_ui_body({"preview": True}, settings=settings)["file_generation"]
                    self.assertGreater(preview["blocked_count"], 0)
                    self.assertTrue(any(issue["code"] == "missing-catalog-binding" for issue in preview["catalog_bindings"]["issues"]))
                    refreshed = generate_link_index_from_ui_body({}, settings=settings)
                    self.assertEqual(refreshed["generated"][0]["target_path"], "")
                    before_index = link_index_mod._link_index_db_path().read_bytes()
                    with self.assertRaisesRegex(ValueError, "冲突"):
                        generate_link_index_files_from_ui_body({}, settings=settings)
                    self.assertEqual(fp.read_bytes(), before_catalog)
                    self.assertEqual(link_index_mod._link_index_db_path().read_bytes(), before_index)
                    self.assertFalse((root / "finish").exists())
                    create.assert_not_called()
                    clear.assert_not_called()

    def test_year_qualified_resources_distinguish_remakes_without_inventing_group_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            names = ["うる星やつら_BDRip", "うる星やつら_BDRip(DBDR)",
                     "うる星やつら 2022_BDRip", "うる星やつら 2022_BDRip(DBDR)"]
            resources = _resource_fixture_rows(Path(td), names)
            resource_index = link_index_mod._resource_items_by_work_key(resources)
            cases = [
                ("1981", "VCB", 0, "resource_format_fallback"),
                ("1981", "DBDR", 1, "resource_exact_press"),
                ("2022", "JSUM", 2, "resource_format_fallback"),
                ("2022", "DBD", 2, "resource_format_fallback"),
                ("2022", "DBDR", 3, "resource_exact_press"),
            ]
            for year, group, expected, source in cases:
                for index in (None, resource_index):
                    with self.subTest(year=year, group=group, indexed=index is not None):
                        work = {"name": "うる星やつら", "begin_date": year + "1013", "year": year}
                        choice = link_index_mod._resource_candidate_for_work_press(
                            work, {"press_format": "BDRip", "press_group": group}, resources, index,
                        )
                        self.assertIsNotNone(choice)
                        self.assertEqual(choice["resource_name"], names[expected])
                        self.assertEqual(choice["match_source"], source)
            # DBD is not declared equivalent to the catalog-only DBDR spelling.
            self.assertIsNone(link_index_mod._resource_candidate_for_work_press(
                {"name": "うる星やつら", "begin_date": "20221013"},
                {"press_format": "BDRip", "press_group": "DBD"}, [resources[3]],
            ))

    def test_matching_year_directory_prevents_unqualified_old_version_from_winning_by_group(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resources = _resource_fixture_rows(Path(td), ["Versioned Work_BDRip(VCB)", "Versioned Work (2022)_BDRip"])
            choice = link_index_mod._resource_candidate_for_work_press(
                {"name": "Versioned Work", "begin_date": "20221013"},
                {"press_format": "BDRip", "press_group": "VCB"}, resources,
                link_index_mod._resource_items_by_work_key(resources),
            )
            self.assertEqual(choice["resource_name"], "Versioned Work (2022)_BDRip")
            self.assertEqual(choice["match_source"], "resource_format_fallback")

    def test_resource_year_requires_known_matching_broadcast_year_but_literal_title_year_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resources = _resource_fixture_rows(Path(td), ["Versioned Work [2022]_BDRip(VCB)", "Space 1999_BDRip(VCB)"])
            for date in ("19810101", "20240101", "", "XXXXXXXX", "00000000"):
                with self.subTest(date=date):
                    self.assertIsNone(link_index_mod._resource_candidate_for_work_press(
                        {"name": "Versioned Work", "begin_date": date, "year": "2022"},
                        {"press_format": "BDRip", "press_group": "VCB"}, resources,
                    ))
            literal = link_index_mod._resource_candidate_for_work_press(
                {"name": "Space 1999", "begin_date": "20990101"},
                {"press_format": "BDRip", "press_group": "VCB"}, resources,
            )
            self.assertEqual(literal["resource_name"], "Space 1999_BDRip(VCB)")

    def test_release_group_alias_never_matches_a_different_press_format(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resources = _resource_fixture_rows(Path(td), ["Example_DVDRip(VCB)", "Example_DVDRip(CK)", "Example_BDRip(VCB-Studio)"])
            for group, candidate in (("VCB", resources[0]), ("VCBM", resources[0]), ("CK", resources[1])):
                with self.subTest(group=group):
                    self.assertIsNone(link_index_mod._resource_candidate_for_work_press(
                        {"name": "Example", "begin_date": "20200101"},
                        {"press_format": "BDRip", "press_group": group}, [candidate],
                    ))
            matched = link_index_mod._resource_candidate_for_work_press(
                {"name": "Example", "begin_date": "20200101"},
                {"press_format": "BDRip", "press_group": "VCBM"}, [resources[2]],
            )
            self.assertEqual(matched["match_source"], "resource_exact_press")
            self.assertEqual(matched["resource_name"], "Example_BDRip(VCB-Studio)")

    def test_cached_unqualified_work_name_cannot_hide_the_actual_directory_release_year(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            resources = _resource_fixture_rows(Path(td), ["うる星やつら_BDRip(DBDR)", "うる星やつら 2022_BDRip(DBDR)"])
            resources[1]["work_name"] = "うる星やつら"
            # Even both cached title fields being stale must not defeat the
            # explicit year still present in the real target directory path.
            resources[1]["name"] = "うる星やつら_BDRip(DBDR)"
            index = link_index_mod._resource_items_by_work_key(resources)
            for supplied_index in (None, index):
                with self.subTest(indexed=supplied_index is not None):
                    old = link_index_mod._resource_candidate_for_work_press(
                        {"name": "うる星やつら", "begin_date": "19811014"},
                        {"press_format": "BDRip", "press_group": "DBDR"}, resources, supplied_index,
                    )
                    new = link_index_mod._resource_candidate_for_work_press(
                        {"name": "うる星やつら", "begin_date": "20221013"},
                        {"press_format": "BDRip", "press_group": "DBDR"}, resources, supplied_index,
                    )
                    self.assertEqual(Path(old["target_path"]), Path(resources[0]["path"]))
                    self.assertEqual(Path(new["target_path"]), Path(resources[1]["path"]))

    def test_current_config_json_omits_legacy_media_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            config = {
                "paths": {
                    "media_root": str(base / "legacy-media"),
                    "resource_roots": [str(base / "library")],
                    "shortcut_root": str(base / "finish"),
                }
            }
            with patch.object(link_index_mod, "_feature_config", return_value=config):
                payload = link_index_mod.collection_link_index_config_json()

            self.assertNotIn("media_root", payload)
            self.assertEqual(payload["resource_roots"], [str((base / "library").resolve())])
            self.assertEqual(payload["shortcut_roots"], [str((base / "finish").resolve())])

    def test_shortcut_root_profile_routes_korea_television_without_changing_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            default_root = base / "japan" / "Finish"
            korea_root = base / "korea" / "Finish"
            target = base / "media" / "K Drama" / "K Drama_1080p"
            target.mkdir(parents=True)
            config = {
                "paths": {
                    "shortcut_root": str(default_root),
                    "shortcut_roots": [
                        {
                            "match": {
                                "domain": "television",
                                "country": "korea",
                                "release_type": "tv",
                            },
                            "root": str(korea_root),
                        }
                    ],
                }
            }
            press = {
                "press_key": "0:main::1080p:----",
                "press_format": "1080p",
                "press_group": "----",
                "press_path": target.name,
                "label": "1080p-----",
            }
            base_work = {
                "work_key": "catalog.yaml#0",
                "yaml_source_rel": "catalog.yaml",
                "index_in_file": 0,
                "name": "K Drama",
                "path": str(target.parent),
                "year": "2026",
                "year_label": "[2026]",
                "begin_date": "20260101",
                "end_date": "20260201",
                "date_range_label": "[20260101][20260201]",
                "release_type": "tv",
            }
            with patch.object(link_index_mod, "_feature_config", return_value=config):
                korea_item = link_index_mod._index_entry_from_work_press(
                    {**base_work, "domain": "television", "country": "korea"},
                    press,
                    [],
                )
                japan_item = link_index_mod._index_entry_from_work_press(
                    {**base_work, "domain": "animation", "country": "japan"},
                    press,
                    [],
                )
            self.assertEqual(Path(korea_item["shortcut_root"]), korea_root.resolve())
            self.assertEqual(Path(japan_item["shortcut_root"]), default_root.resolve())
            self.assertTrue(Path(korea_item["shortcut_path"]).is_relative_to(korea_root.resolve()))
            self.assertTrue(Path(japan_item["shortcut_path"]).is_relative_to(default_root.resolve()))

    def test_scan_shortcut_leaves_marks_link_only_when_target_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = root / "finish"
            target = media / "Good Work" / "BDRip"
            target.mkdir(parents=True)
            (finish / "[2099]").mkdir(parents=True)
            good = finish / "[2099]" / "Good.lnk"
            bad = finish / "[2099]" / "Broken.lnk"
            good.write_text("", encoding="utf-8")
            bad.write_text("", encoding="utf-8")
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                }
            }

            target_infos = {
                str(good.resolve()): {"target_path": str(target), "target_resolved": True, "error": ""},
                str(bad.resolve()): {
                    "target_path": str(media / "Missing Work" / "BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                leaves = link_index_mod._scan_shortcut_leaves(refresh_targets=True)

            by_name = {item["name"]: item for item in leaves}
            self.assertTrue(by_name["Good.lnk"]["shortcut_exists"])
            self.assertTrue(by_name["Good.lnk"]["target_exists"])
            self.assertFalse(by_name["Broken.lnk"]["target_exists"])

            changed_infos = {
                str(good.resolve()): {
                    "target_path": str(media / "Moved Work" / "BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
                str(bad.resolve()): {
                    "target_path": str(media / "Missing Work" / "BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
            }
            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=changed_infos,
            ):
                cached = link_index_mod._scan_shortcut_leaves()
                refreshed = link_index_mod._scan_shortcut_leaves(refresh_targets=True)

            cached_by_name = {item["name"]: item for item in cached}
            refreshed_by_name = {item["name"]: item for item in refreshed}
            self.assertTrue(cached_by_name["Good.lnk"]["target_exists"])
            self.assertFalse(refreshed_by_name["Good.lnk"]["target_exists"])

    def test_scan_shortcut_leaves_uses_persistent_cache_without_reparsing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = root / "finish"
            target = media / "Good Work" / "BDRip"
            target.mkdir(parents=True)
            shortcut = finish / "[2099]" / "Good.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                }
            }
            target_infos = {
                str(shortcut.resolve()): {"target_path": str(target), "target_resolved": True, "error": ""},
            }

            with (
                patch("collection_detail.link_index._feature_config", return_value=cfg),
                patch("collection_detail.link_index._windows_shortcut_targets", return_value=target_infos),
                patch("collection_detail.link_index.feature_data_root", return_value=root / "data"),
            ):
                first = link_index_mod._scan_shortcut_leaves(refresh_targets=True)

            self.assertTrue((root / "data" / "cache" / "link-index-shortcut-scan-cache.yaml").is_file())
            self.assertEqual(first[0]["target_path"], str(target))
            link_index_mod._SHORTCUT_SCAN_CACHE["signature"] = None
            link_index_mod._SHORTCUT_SCAN_CACHE["leaves"] = []

            with (
                patch("collection_detail.link_index._feature_config", return_value=cfg),
                patch(
                    "collection_detail.link_index._windows_shortcut_targets",
                    side_effect=AssertionError("persistent cache should avoid target parsing"),
                ),
                patch("collection_detail.link_index.feature_data_root", return_value=root / "data"),
            ):
                cached = link_index_mod._scan_shortcut_leaves()

            self.assertEqual(cached[0]["target_path"], str(target))
            self.assertTrue(cached[0]["target_exists"])

    def test_readonly_shortcut_observer_refreshes_target_facts_without_cache_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            finish, target = root / "finish", root / "target"
            finish.mkdir()
            target.mkdir()
            shortcut = finish / "Known.lnk"
            shortcut.write_text("fixture", encoding="utf-8")
            cfg = {"paths": {"shortcut_root": str(finish)}}
            infos = {str(shortcut): {"target_path": str(target), "target_resolved": True}}
            with (
                patch.object(link_index_mod, "_feature_config", return_value=cfg),
                patch.object(link_index_mod, "_windows_shortcut_targets", return_value=infos) as reader,
                patch.object(link_index_mod, "_save_shortcut_scan_cache", side_effect=AssertionError("GET cannot publish")),
                patch.object(link_index_mod, "_load_shortcut_scan_cache", side_effect=AssertionError("GET uses memory only")),
            ):
                first = link_index_mod._scan_shortcut_leaves(publish_cache=False)
                self.assertTrue(first[0]["target_exists"])
                target.rmdir()
                cached = link_index_mod._scan_shortcut_leaves(publish_cache=False)
                self.assertFalse(cached[0]["target_exists"])
                self.assertEqual(reader.call_count, 1)
                forced = link_index_mod._scan_shortcut_leaves(publish_cache=False, refresh_targets=True)
                self.assertFalse(forced[0]["target_exists"])
                self.assertEqual(reader.call_count, 2)

    def test_shortcut_observer_skips_reparse_roots_subtrees_and_link_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            finish = root / "finish"
            blocked = finish / "blocked"
            blocked.mkdir(parents=True)
            good = finish / "Good.lnk"
            alias = finish / "Alias.lnk"
            hidden = blocked / "Hidden.lnk"
            for item in (good, alias, hidden):
                item.write_text("fixture", encoding="utf-8")
            original_lstat = Path.lstat
            original_iterdir = Path.iterdir

            def guarded_lstat(path: Path, *args, **kwargs):
                if path == blocked:
                    return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
                if path == alias:
                    return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
                return original_lstat(path, *args, **kwargs)

            def guarded_iterdir(path: Path):
                if path == blocked:
                    raise AssertionError("unsafe subtree must not be entered")
                return original_iterdir(path)

            with (
                patch.object(link_index_mod, "_feature_config", return_value={"paths": {"shortcut_root": str(finish)}}),
                patch.object(Path, "lstat", guarded_lstat),
                patch.object(Path, "iterdir", guarded_iterdir),
                patch.object(link_index_mod, "_windows_shortcut_targets", return_value={}) as reader,
            ):
                leaves = link_index_mod._scan_shortcut_leaves(publish_cache=False)
                self.assertEqual([item["shortcut_path"] for item in leaves], [str(good)])
                reader.assert_called_once_with([good])
                with patch.object(link_index_mod, "_feature_config", return_value={"paths": {"shortcut_root": str(blocked)}}):
                    self.assertEqual(link_index_mod._scan_shortcut_leaves(publish_cache=False), [])

    def test_windows_shortcut_targets_decodes_unicode_target_paths(self) -> None:
        shortcut = Path(r"C:\Links\BDRip(VCB).lnk")
        target = r"Y:\涼宮ハルヒの憂鬱\涼宮ハルヒの憂鬱 2009_BDRip(VCB)"
        payload = json.dumps(
            [{"path": str(shortcut), "target": target, "error": ""}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        proc = type(
            "Proc",
            (),
            {
                "returncode": 0,
                "stdout": base64.b64encode(payload.encode("utf-8")).decode("ascii"),
            },
        )()

        with patch("collection_detail.link_index.subprocess.run", return_value=proc):
            result = self._actual_shortcut_targets([shortcut])

        self.assertEqual(result[str(shortcut.resolve())]["target_path"], target)
        self.assertTrue(result[str(shortcut.resolve())]["target_resolved"])

    def test_resource_libraries_default_to_legacy_media_root_and_scan_two_levels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = media / "Finish"
            (media / "Series A" / "Work A_BDRip").mkdir(parents=True)
            (media / "Series A" / "Work A_BDRip" / "ep01.mkv").write_bytes(b"x")
            (media / "Series A" / "Work B_720p-HKGX").mkdir(parents=True)
            (media / "Series A" / "not-a-resource.txt").write_text("x", encoding="utf-8")
            (media / "$RECYCLE.BIN" / "Deleted Work_BDRip").mkdir(parents=True)
            (media / "Loose").mkdir(parents=True)
            (finish / "[2099]").mkdir(parents=True)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_excludes": {str(media): ["$recycle"]},
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ):
                payload = scan_resource_libraries_payload()
                cached = resource_libraries_cached_payload()
                root_node = resource_libraries_node_payload("root:0")["node"]
                series_a_loaded = resource_libraries_node_payload("root:0/Series A")["node"]
                work_a = resource_libraries_node_payload("root:0/Series A/Work A_BDRip")["node"]

            self.assertEqual(payload["summary"]["root_count"], 1)
            self.assertEqual(payload["summary"]["series_count"], 2)
            self.assertEqual(payload["summary"]["item_count"], 2)
            self.assertEqual(payload["summary"]["file_count"], 2)
            self.assertEqual(payload["summary"]["dir_count"], 4)
            self.assertEqual(payload["summary"]["size"], 2)
            self.assertEqual(payload["summary"]["direct_child_count"], 2)
            self.assertEqual(payload["summary"]["total_child_count"], 6)
            self.assertTrue(cached["cached"])
            self.assertEqual(cached["summary"]["item_count"], 2)
            self.assertTrue(str(cached["cache_path"]).endswith("resource-library-scan-cache.yaml"))
            self.assertFalse(cached["tree"]["children"][0]["children_loaded"])
            self.assertEqual(root_node["size"], 2)
            self.assertEqual(root_node["direct_child_count"], 2)
            self.assertEqual(root_node["total_child_count"], 6)
            self.assertIsInstance(root_node["mtime"], int)
            series_nodes = root_node["children"]
            series_a = next(node for node in series_nodes if node["name"] == "Series A")
            self.assertFalse(series_a["children_loaded"])
            self.assertEqual(series_a["size"], 2)
            self.assertEqual(series_a["direct_child_count"], 3)
            self.assertEqual(series_a["total_child_count"], 4)
            self.assertEqual(series_a_loaded["children"][0]["name"], "Work A_BDRip")
            self.assertEqual(series_a_loaded["files"][0]["name"], "not-a-resource.txt")
            self.assertEqual(series_a_loaded["size"], 2)
            self.assertEqual(series_a_loaded["direct_child_count"], 3)
            self.assertEqual(series_a_loaded["total_child_count"], 4)
            self.assertEqual(work_a["files"][0]["name"], "ep01.mkv")
            self.assertEqual(work_a["size"], 1)
            self.assertEqual(work_a["direct_child_count"], 1)
            self.assertEqual(work_a["total_child_count"], 1)
            self.assertNotIn("Finish", {root["name"] for root in payload["roots"][0]["series"]})
            self.assertNotIn("$RECYCLE.BIN", {root["name"] for root in payload["roots"][0]["series"]})
            first = payload["items"][0]
            self.assertEqual(first["series_name"], "Series A")
            self.assertEqual(first["work_name"], "Work A")
            self.assertEqual(first["press_info"], "BDRip")
            self.assertEqual(first["relpath"], "Series A/Work A_BDRip")

    def test_resource_libraries_scan_defers_deep_resource_children(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = media / "Finish"
            deep = media / "Series A" / "Work A_BDRip" / "Disc 1"
            deep.mkdir(parents=True)
            (deep / "ep01.mkv").write_bytes(b"x")
            (finish / "[2099]").mkdir(parents=True)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ):
                payload = scan_resource_libraries_payload()
                work_a = resource_libraries_node_payload("root:0/Series A/Work A_BDRip")["node"]
                disc_1 = resource_libraries_node_payload("root:0/Series A/Work A_BDRip/Disc 1")["node"]

            self.assertEqual(payload["summary"]["item_count"], 1)
            self.assertEqual(work_a["children"][0]["name"], "Disc 1")
            self.assertFalse(work_a["children"][0]["children_loaded"])
            self.assertEqual(work_a["children"][0]["size"], 1)
            self.assertEqual(work_a["size"], 1)
            self.assertEqual(work_a["total_child_count"], 2)
            self.assertEqual(work_a["files"], [])
            self.assertEqual(disc_1["files"][0]["name"], "ep01.mkv")
            self.assertEqual(disc_1["size"], 1)

    def test_save_resource_library_roots_writes_feature_config(self) -> None:
        self._default_config_patch.stop()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "config.yaml"
            cfg_path.write_text(
                textwrap.dedent(
                    """
                    version: 1
                    paths:
                      media_root: "E:/Old"
                    link_index:
                      scan_max_dirs: 2000
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            lib_a = root / "Media A"
            lib_b = root / "Media B"

            with patch("collection_detail.link_index.feature_config_path", return_value=cfg_path):
                result = save_resource_library_roots_from_ui_body(
                    {
                        "roots": [
                            {"path": str(lib_a), "excludes": ["$recycle", "System Volume Information"]},
                            str(lib_b),
                            {"path": str(lib_a), "excludes": ["ignored-duplicate"]},
                        ]
                    },
                )

            raw = load_yaml(cfg_path)
            self.assertEqual(raw["paths"]["resource_roots"], [str(lib_a.resolve()), str(lib_b.resolve())])
            self.assertEqual(
                raw["paths"]["resource_excludes"],
                {str(lib_a.resolve()): ["$recycle", "System Volume Information"]},
            )
            self.assertEqual(result["config"]["resource_roots"], [str(lib_a.resolve()), str(lib_b.resolve())])
            self.assertEqual(result["config"]["resource_excludes"][str(lib_a.resolve())], ["$recycle", "System Volume Information"])

    def test_resource_cache_never_auto_binds_missing_catalog_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            shortcut = finish / "[2098]" / "[20980101][20980331] Air Gear" / "BDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("shortcut", encoding="utf-8")
            missing_target = root / "missing" / "Air Gear_BDRip"
            resource = root / "resource"
            fixed_target = resource / "Air Gear" / "Air Gear_BDRip"
            fixed_target.mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press("Air Gear", [("BDRip", "----")]), encoding="utf-8")
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_roots": [str(resource)],
                },
                "link_index": {"shortcut_name": "{press_format}"},
            }
            st = _settings(db, fp)

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value={str(shortcut.resolve()): {"target_path": str(missing_target), "target_resolved": True}},
            ):
                scan_resource_libraries_payload()
                payload = generate_link_index_from_ui_body({}, settings=st)

            self.assertEqual(payload["plan_summary"]["ready"], 0)
            self.assertEqual(payload["plan_summary"]["target_fixable"], 0)
            tree_link = _find_link_by_relpath(payload["tree"], "[2099]/[20990101][20990331] Air Gear/BDRip.lnk")
            self.assertIsNotNone(tree_link)
            assert tree_link is not None
            self.assertEqual(tree_link["target_path"], "")
            self.assertEqual(tree_link["source"], "index_db")

    def test_resource_group_matches_do_not_become_authoritative_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            resource = root / "resource"
            work_name = "Haruhi 2009"
            shortcut_dir = finish / "[2099]" / f"[20990101][20990331] {work_name}"
            shortcut_plain = shortcut_dir / "BDRip.lnk"
            shortcut_vcb = shortcut_dir / "BDRip(VCB).lnk"
            shortcut_dir.mkdir(parents=True)
            shortcut_plain.write_text("shortcut", encoding="utf-8")
            shortcut_vcb.write_text("shortcut", encoding="utf-8")
            fixed_plain = resource / work_name / f"{work_name}_BDRip(CASO)"
            fixed_vcb = resource / work_name / f"{work_name}_BDRip(VCB)"
            fixed_plain.mkdir(parents=True)
            fixed_vcb.mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press(work_name, [("BDRip", "VCB"), ("BDRip", "CASO")]), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_roots": [str(resource)],
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{date_range_label} {name}"],
                    "shortcut_name": "{press_format}{press_group_suffix}",
                },
            }
            target_infos = {
                str(shortcut_plain.resolve()): {
                    "target_path": str(root / "missing" / work_name / f"{work_name}_BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
                str(shortcut_vcb.resolve()): {
                    "target_path": str(root / "missing" / work_name / f"{work_name}_BDRip(VCB)"),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                scan_resource_libraries_payload()
                payload = generate_link_index_from_ui_body({}, settings=st)

            self.assertEqual(payload["plan_summary"]["ready"], 0)
            self.assertEqual(payload["plan_summary"]["target_fixable"], 0)
            plain_node = _find_link_by_relpath(payload["tree"], f"[2099]/[20990101][20990331] {work_name}/BDRip(CASO).lnk")
            vcb_node = _find_link_by_relpath(payload["tree"], f"[2099]/[20990101][20990331] {work_name}/BDRip(VCB).lnk")
            self.assertIsNotNone(plain_node)
            self.assertIsNotNone(vcb_node)
            assert plain_node is not None
            assert vcb_node is not None
            self.assertEqual(plain_node["target_path"], "")
            self.assertEqual(vcb_node["target_path"], "")

    def test_format_only_resource_fallback_remains_unbound_until_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            resource = root / "resource"
            work_name = "Haruhi 2009"
            series_name = "Haruhi"
            shortcut_dir = finish / "[2099]" / f"[20990101][20990331] {work_name}"
            shortcut_plain = shortcut_dir / "BDRip.lnk"
            shortcut_dir.mkdir(parents=True)
            shortcut_plain.write_text("shortcut", encoding="utf-8")
            fixed_plain = resource / series_name / f"{work_name}_BDRip"
            older_plain = resource / series_name / f"{series_name}_BDRip"
            fixed_plain.mkdir(parents=True)
            older_plain.mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml_with_press(work_name, [("BDRip", "VCB"), ("BDRip", "CASO")]), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                    "resource_roots": [str(resource)],
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{date_range_label} {name}"],
                    "shortcut_name": "{press_format}{press_group_suffix}",
                },
            }
            resource_items = [
                {
                    "root": str(resource),
                    "series_name": series_name,
                    "name": f"{work_name}_BDRip",
                    "work_name": "",
                    "press_info": "BDRip",
                    "relpath": f"{series_name}/{work_name}_BDRip",
                    "path": str(fixed_plain),
                },
                {
                    "root": str(resource),
                    "series_name": series_name,
                    "name": f"{series_name}_BDRip",
                    "work_name": series_name,
                    "press_info": "BDRip",
                    "relpath": f"{series_name}/{series_name}_BDRip",
                    "path": str(older_plain),
                }
            ]
            target_infos = {
                str(shortcut_plain.resolve()): {
                    "target_path": str(root / "missing" / series_name / f"{work_name}_BDRip"),
                    "target_resolved": True,
                    "error": "",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._resource_fix_items_from_cache",
                return_value=resource_items,
            ), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                payload = generate_link_index_from_ui_body({}, settings=st)

            self.assertEqual(payload["plan_summary"]["ready"], 0)
            self.assertEqual(payload["plan_summary"]["target_fixable"], 0)
            self.assertEqual(payload["catalog_bindings"]["summary"]["blocking_issue_count"], 2)
            self.assertTrue(any(issue["code"] == "missing-catalog-binding" for issue in payload["catalog_bindings"]["issues"]))
            plain_node = _find_link_by_relpath(payload["tree"], f"[2099]/[20990101][20990331] {work_name}/BDRip(VCB).lnk")
            self.assertIsNotNone(plain_node)
            assert plain_node is not None
            self.assertEqual(plain_node["target_path"], "")

    def test_preview_uses_catalog_mapping_to_build_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            (media / "Absolute Duo" / "BDRip VCB").mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{domain_label}", "{country_label}", "{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = collection_link_index_payload(st)
                work = payload["works"][0]
                press0 = work["press"][0]
                out = preview_link_index_from_ui_body(
                    {
                        "works": [
                            {
                                "yaml_source_rel": work["yaml_source_rel"],
                                "index_in_file": work["index_in_file"],
                                "path": "Absolute Duo",
                                "press": [
                                    {
                                        "press_key": press0["press_key"],
                                        "press_path": "BDRip VCB",
                                    },
                                ],
                            },
                        ],
                    },
                    settings=st,
                )

            self.assertEqual(out["plan_summary"]["ready"], 1)
            plan0 = out["plan"][0]
            self.assertTrue(plan0["shortcut_path"].endswith(r"动画\日本\[2099]\Absolute Duo\BDRip-VCB.lnk"))
            self.assertTrue(plan0["target_path"].endswith(r"media\Absolute Duo\BDRip VCB"))
            self.assertEqual(out["tree"]["children"][0]["name"], "动画")
            self.assertIsNotNone(
                _find_link_by_relpath(out["tree"], "鍔ㄧ敾/鏃ユ湰/[2099]/Absolute Duo/BDRip-VCB.lnk")
            )

    def test_default_shortcut_layout_matches_existing_finish_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(
                _catalog_yaml_with_press("Mapped Work", [("BDRip", "VCB"), ("DVDRip", "----")]),
                encoding="utf-8",
            )
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(root / "media"),
                    "shortcut_root": str(root / "finish"),
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = preview_link_index_from_ui_body(
                    {
                        "works": [
                            {
                                "yaml_source_rel": "[JP][TVInfo][2098].yaml",
                                "index_in_file": 0,
                                "path": "Mapped Work",
                                "press": [
                                    {"press_key": "0:main::BDRip:VCB", "press_path": "Mapped Work_BDRip"},
                                    {"press_key": "1:main::DVDRip:----", "press_path": "Mapped Work_DVDRip"},
                                ],
                            }
                        ]
                    },
                    settings=st,
                )

            relpaths = sorted(item["shortcut_relpath"] for item in payload["plan"])
            self.assertEqual(
                relpaths,
                [
                    "[2099]/[20990101][20990331] Mapped Work/BDRip(VCB).lnk",
                    "[2099]/[20990101][20990331] Mapped Work/DVDRip.lnk",
                ],
            )

    def test_default_shortcut_layout_compacts_iso_catalog_dates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(
                _catalog_yaml_with_press_dates(
                    "Mapped Work",
                    [("BDRip", "VCB")],
                    "2099-01-01",
                    "2099-03-31",
                ),
                encoding="utf-8",
            )
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(root / "media"),
                    "shortcut_root": str(root / "finish"),
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = preview_link_index_from_ui_body(
                    {
                        "works": [
                            {
                                "yaml_source_rel": "[JP][TVInfo][2099].yaml",
                                "index_in_file": 0,
                                "path": "Mapped Work",
                                "press": [
                                    {
                                        "press_key": "0:main::BDRip:VCB",
                                        "press_path": "Mapped Work_BDRip",
                                    }
                                ],
                            }
                        ]
                    },
                    settings=st,
                )

            self.assertEqual(payload["works"][0]["begin_date"], "20990101")
            self.assertEqual(payload["works"][0]["end_date"], "20990331")
            self.assertEqual(
                payload["plan"][0]["shortcut_relpath"],
                "[2099]/[20990101][20990331] Mapped Work/BDRip(VCB).lnk",
            )


    def test_save_and_generate_write_catalog_path_and_press_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            (media / "Absolute Duo" / "BDRip VCB").mkdir(parents=True)
            (media / "Absolute Duo" / "720p DMG").mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = collection_link_index_payload(st)
                work = payload["works"][0]
                body = {
                    "works": [
                        {
                            "yaml_source_rel": work["yaml_source_rel"],
                            "index_in_file": work["index_in_file"],
                            "path": "Absolute Duo",
                            "press": [
                                {"press_key": work["press"][0]["press_key"], "press_path": "BDRip VCB"},
                                {"press_key": work["press"][1]["press_key"], "press_path": "720p DMG"},
                            ],
                        },
                    ],
                }
                saved = save_link_index_from_ui_body(body, settings=st)
                created: list[tuple[Path, Path]] = []

                def fake_shortcut(shortcut_path: Path, target_path: Path) -> None:
                    created.append((shortcut_path, target_path))

                with patch("collection_detail.link_index._create_windows_shortcut", side_effect=fake_shortcut):
                    generated = generate_link_index_from_ui_body(body, settings=st)

            saved_doc = load_yaml(fp)
            coll = saved_doc[0]["attributes"][1]["data"]
            self.assertEqual(coll["path"], "Absolute Duo")
            self.assertEqual(coll["collectioned"][0]["press_path"], "BDRip VCB")
            self.assertEqual(coll["collectioned"][1]["press_path"], "720p DMG")
            self.assertEqual(saved["plan_summary"]["ready"], 2)
            self.assertEqual(generated["index_db"]["item_count"], 2)
            self.assertEqual(generated["plan_summary"]["created"], 0)
            self.assertEqual(len(created), 0)

    def test_file_generation_reconciles_new_korean_catalog_and_cleans_copied_path_without_database_edits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            jp = db / "[JP][TVInfo][2098].yaml"
            kr = db / "[KR][TVInfo][2026].yaml"
            jp_target = root / "media" / "Existing Work" / "BDRip VCB"
            kr_target = root / "media" / "New Korean Work" / "New Korean Work_1080p"
            jp_target.mkdir(parents=True)
            kr_target.mkdir(parents=True)
            jp.write_text(_catalog_yaml_mapped("Existing Work", str(jp_target.parent).replace("\\", "/"), jp_target.name), encoding="utf-8")
            cfg = {"paths": {"shortcut_root": str(root / "JP"), "shortcut_roots": [{
                "match": {"domain": "television", "country": "korea", "release_type": "tv"}, "root": str(root / "KR"),
            }]}, "link_index": {"layout_levels": ["{year_label}", "{date_range_label} {name}"], "shortcut_name": "{press_format}{press_group_suffix}"}}
            created = []
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "feature_data_root", return_value=root / "feature",
            ), patch.object(link_index_mod, "_create_windows_shortcut", side_effect=lambda link, target: created.append((link, target))):
                generate_link_index_from_ui_body({}, settings=_settings(db, jp))
                index_path = link_index_mod._link_index_db_path()
                previous_index = index_path.read_bytes()
                korean = load_yaml(jp)
                korean[0]["attributes"][0]["data"] = {"start": "20260515", "end": ""}
                coll = korean[0]["attributes"][1]["data"]
                coll.update({"domain": "television", "path": '\u202a"' + str(kr_target.parent) + '"\u202c',
                             "collectioned": [{"press_format": "1080p", "press_group": "", "press_path": kr_target.name}]})
                korean[0]["attributes"][2]["data"] = "korea"
                korean[0]["attributes"][3]["data"] = "New Korean Work"
                kr.write_text(dump_yaml_string(korean), encoding="utf-8")
                original_catalog = kr.read_bytes()
                settings = _settings(db, jp, kr)
                preview = generate_link_index_files_from_ui_body({"preview": True}, settings=settings)
                self.assertEqual(preview["file_generation"]["total"], 2)
                self.assertEqual(preview["file_generation"]["creatable"], 2)
                self.assertTrue(preview["file_generation"]["catalog_refreshed"])
                self.assertEqual(index_path.read_bytes(), previous_index, "preview may not update stored index")
                self.assertEqual(created, [])
                result = generate_link_index_files_from_ui_body({}, settings=settings)
                self.assertEqual(result["file_generation"]["created"], 2)
                expected = root / "KR" / "[2026]" / "[20260515] New Korean Work" / "1080p.lnk"
                self.assertIn((expected.resolve(), kr_target.resolve()), created)
                self.assertEqual(kr.read_bytes(), original_catalog, "reading legacy path wrappers must not rewrite catalog")
                self.assertEqual(len(load_yaml(index_path)["items"]), 2)
                # A removed record must not survive through the old saved index.
                jp.write_text("[]\n", encoding="utf-8")
                items, cached = link_index_mod._link_index_file_generation_plan(settings)
                self.assertTrue(cached)
                self.assertEqual([item["name"] for item in items], ["New Korean Work"])

    def test_file_generation_valid_catalog_overrides_historical_manual_index_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            target = root / "media" / "Mapped Work" / "BDRip VCB"
            manual_target = root / "media" / "Manual Choice" / "BDRip VCB"
            target.mkdir(parents=True)
            manual_target.mkdir(parents=True)
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(_catalog_yaml_mapped("Mapped Work", str(target.parent).replace("\\", "/"), target.name), encoding="utf-8")
            settings = _settings(db, fp)
            cfg = {"paths": {"resource_roots": [str(root / "media")], "shortcut_root": str(root / "finish")}}
            stale_resources = [{
                "path": str(target), "root": str(root / "media"), "series_name": "Mapped Work",
                "work_name": "Mapped Work", "name": "Mapped Work_BDRip(VCB)", "press_info": "BDRip(VCB)",
            }]
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "feature_data_root", return_value=root / "feature",
            ), patch.object(link_index_mod, "_resource_fix_items_from_cache", return_value=[]):
                generate_link_index_from_ui_body({}, settings=settings)
                index_path = link_index_mod._link_index_db_path()
                saved = load_yaml(index_path)
                saved["items"][0].update({"target_source": "manual_fix", "target_path": str(manual_target.resolve())})
                link_index_mod._save_link_index_db(saved)
                before_index, before_catalog = index_path.read_bytes(), fp.read_bytes()
                with patch.object(link_index_mod, "_resource_fix_items_from_cache", return_value=stale_resources):
                    items, cached = link_index_mod._link_index_file_generation_plan(settings)
                self.assertTrue(cached)
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0]["target_path"], str(target.resolve()))
                self.assertEqual(items[0]["target_source"], "catalog")
                self.assertTrue(items[0]["target_exists"])
                self.assertEqual(index_path.read_bytes(), before_index)
                self.assertEqual(fp.read_bytes(), before_catalog)

    def test_file_generation_catalog_path_changes_override_manual_target_and_stale_resources(self) -> None:
        for changed_field in ("path", "press_path"):
            with self.subTest(changed_field=changed_field), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                db = root / "db"
                db.mkdir()
                old_target = root / "media" / "Mapped Work" / "BDRip VCB"
                manual_target = root / "media" / "Manual Choice" / "BDRip VCB"
                new_target = (
                    root / "media" / "Updated Work Directory" / old_target.name
                    if changed_field == "path" else old_target.parent / "Updated_BDRip(VCB)"
                )
                for target in (old_target, manual_target, new_target):
                    target.mkdir(parents=True)
                fp = db / "[JP][TVInfo][2098].yaml"
                fp.write_text(_catalog_yaml_mapped("Mapped Work", str(old_target.parent).replace("\\", "/"), old_target.name), encoding="utf-8")
                settings = _settings(db, fp)
                cfg = {"paths": {"resource_roots": [str(root / "media")], "shortcut_root": str(root / "finish")}}
                stale_resources = [{
                    "path": str(old_target), "root": str(root / "media"), "series_name": "Mapped Work",
                    "work_name": "Mapped Work", "name": "Mapped Work_BDRip(VCB)", "press_info": "BDRip(VCB)",
                }]
                with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                    link_index_mod, "feature_data_root", return_value=root / "feature",
                ), patch.object(link_index_mod, "_resource_fix_items_from_cache", return_value=[]):
                    generate_link_index_from_ui_body({}, settings=settings)
                    index_path = link_index_mod._link_index_db_path()
                    saved = load_yaml(index_path)
                    saved["items"][0].update({"target_source": "manual_fix", "target_path": str(manual_target.resolve())})
                    link_index_mod._save_link_index_db(saved)
                    fp.write_text(_catalog_yaml_mapped("Mapped Work", str(new_target.parent).replace("\\", "/"), new_target.name), encoding="utf-8")
                    before_index, before_catalog = index_path.read_bytes(), fp.read_bytes()
                    with patch.object(link_index_mod, "_resource_fix_items_from_cache", return_value=stale_resources):
                        items, cached = link_index_mod._link_index_file_generation_plan(settings)
                    self.assertTrue(cached)
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["target_path"], str(new_target.resolve()))
                    self.assertEqual(items[0]["target_source"], "catalog")
                    self.assertTrue(items[0]["target_exists"])
                    self.assertEqual(items[0]["resource_candidate"]["target_path"], str(old_target.resolve()))
                    self.assertEqual(index_path.read_bytes(), before_index)
                    self.assertEqual(fp.read_bytes(), before_catalog)

    def test_file_generation_second_root_backup_failure_preserves_all_outputs_and_index(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            output_roots = [root / "JP", root / "KR"]
            targets = [root / "media" / "JP", root / "media" / "KR"]
            for output, target in zip(output_roots, targets):
                output.mkdir()
                target.mkdir(parents=True)
                (output / "keep.lnk").write_text(str(target), encoding="utf-8")
            items = [{"shortcut_root": str(output), "shortcut_relpath": "new.lnk", "target_path": str(target)}
                     for output, target in zip(output_roots, targets)]
            cfg = {"paths": {"shortcut_root": str(output_roots[0]), "shortcut_roots": [{
                "match": {"domain": "television", "country": "korea", "release_type": "tv"},
                "root": str(output_roots[1]),
            }]}}
            original_backup = link_index_mod._backup_shortcut_root
            completed_backups = []

            def fail_second_backup(output, backup_dir):
                if output == output_roots[1]:
                    raise OSError("simulated second root backup failure")
                backup_path = original_backup(output, backup_dir)
                completed_backups.append(Path(backup_path))
                return backup_path

            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "feature_data_root", return_value=root / "feature",
            ), patch.object(link_index_mod, "_catalog_binding_generation_context", return_value=([], items, True, _empty_binding_plan())):
                link_index_mod._save_link_index_db({"items": [{"name": "old index sentinel"}]})
                index_path = link_index_mod._link_index_db_path()
                before_index = index_path.read_bytes()
                before_outputs = [(output / "keep.lnk").read_bytes() for output in output_roots]
                with patch.object(link_index_mod, "_backup_shortcut_root", side_effect=fail_second_backup) as backup, patch.object(
                    link_index_mod, "_clear_directory_contents", wraps=link_index_mod._clear_directory_contents,
                ) as clear, patch.object(link_index_mod, "_save_index_entries", wraps=link_index_mod._save_index_entries) as save, patch.object(
                    link_index_mod, "_create_windows_shortcut",
                ) as create:
                    with self.assertRaisesRegex(OSError, "second root backup failure"):
                        generate_link_index_files_from_ui_body({
                            "confirm_clear": True, "confirm_clear_twice": True,
                            "backup_dir": str(root / "backups"),
                        }, settings=_settings(db))
                    self.assertEqual(backup.call_count, 2)
                    clear.assert_not_called()
                    save.assert_not_called()
                    create.assert_not_called()
                self.assertEqual(index_path.read_bytes(), before_index)
                self.assertEqual([(output / "keep.lnk").read_bytes() for output in output_roots], before_outputs)
                self.assertEqual(len(completed_backups), 1)
                self.assertEqual((completed_backups[0] / "keep.lnk").read_bytes(), before_outputs[0])

    def test_file_generation_rejects_stale_preview_plan_before_any_output_or_index_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            finish = root / "finish"
            finish.mkdir()
            existing_link = finish / "keep.lnk"
            existing_link.write_bytes(b"existing shortcut sentinel")
            old_target = root / "media" / "Mapped Work" / "BDRip VCB"
            new_target = old_target.parent / "Updated_BDRip(VCB)"
            old_target.mkdir(parents=True)
            new_target.mkdir()
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(_catalog_yaml_mapped("Mapped Work", str(old_target.parent).replace("\\", "/"), old_target.name), encoding="utf-8")
            settings = _settings(db, fp)
            cfg = {"paths": {"shortcut_root": str(finish)}}
            with patch.object(link_index_mod, "_feature_config", return_value=cfg), patch.object(
                link_index_mod, "feature_data_root", return_value=root / "feature",
            ):
                generate_link_index_from_ui_body({}, settings=settings)
                index_path = link_index_mod._link_index_db_path()
                before_index = index_path.read_bytes()
                preview = generate_link_index_files_from_ui_body({"preview": True}, settings=settings)
                plan_id = preview["file_generation"]["plan_id"]
                self.assertTrue(plan_id)
                fp.write_text(_catalog_yaml_mapped("Mapped Work", str(new_target.parent).replace("\\", "/"), new_target.name), encoding="utf-8")
                changed_catalog = fp.read_bytes()
                with patch.object(link_index_mod, "_backup_shortcut_root") as backup, patch.object(
                    link_index_mod, "_clear_directory_contents",
                ) as clear, patch.object(link_index_mod, "_save_index_entries") as save, patch.object(
                    link_index_mod, "_create_windows_shortcut",
                ) as create:
                    with self.assertRaisesRegex(ValueError, "计划|预览"):
                        generate_link_index_files_from_ui_body({
                            "plan_id": plan_id, "confirm_clear": True, "confirm_clear_twice": True,
                            "backup_dir": str(root / "backups"),
                        }, settings=settings)
                    backup.assert_not_called()
                    clear.assert_not_called()
                    save.assert_not_called()
                    create.assert_not_called()
                self.assertEqual(index_path.read_bytes(), before_index)
                self.assertEqual(fp.read_bytes(), changed_catalog)
                self.assertEqual(existing_link.read_bytes(), b"existing shortcut sentinel")
                self.assertEqual(list(finish.iterdir()), [existing_link])
                self.assertFalse((root / "backups").exists())

    def test_generate_link_index_files_requires_confirm_and_can_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            backup_root = root / "backup"
            target = media / "Mapped Work" / "BDRip VCB"
            target.mkdir(parents=True)
            finish.mkdir(parents=True)
            (finish / "old.txt").write_text("old", encoding="utf-8")
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(_catalog_yaml_mapped("Mapped Work", "Mapped Work", "BDRip VCB"), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            created: list[tuple[Path, Path]] = []

            def fake_shortcut(shortcut_path: Path, target_path: Path) -> None:
                created.append((shortcut_path, target_path))
                shortcut_path.parent.mkdir(parents=True, exist_ok=True)
                shortcut_path.write_text(str(target_path), encoding="utf-8")

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ), patch("collection_detail.link_index._create_windows_shortcut", side_effect=fake_shortcut):
                generate_link_index_from_ui_body({}, settings=st)
                preview = generate_link_index_files_from_ui_body({"preview": True}, settings=st)
                self.assertTrue(preview["file_generation"]["root_non_empty"])
                with self.assertRaises(ValueError):
                    generate_link_index_files_from_ui_body({}, settings=st)
                result = generate_link_index_files_from_ui_body(
                    {
                        "confirm_clear": True,
                        "confirm_clear_twice": True,
                        "backup_dir": str(backup_root),
                    },
                    settings=st,
                )

            backup_path = Path(result["file_generation"]["backup_path"])
            link_path = finish / "[2098]" / "Mapped Work" / "BDRip-VCB.lnk"
            self.assertEqual(result["file_generation"]["created"], 1)
            self.assertEqual(result["file_generation"]["removed_count"], 1)
            self.assertTrue((backup_path / "old.txt").is_file())
            self.assertFalse((finish / "old.txt").exists())
            self.assertTrue(link_path.is_file())
            self.assertEqual(link_path.read_text(encoding="utf-8"), str(target.resolve()))
            self.assertEqual(created, [(link_path.resolve(), target.resolve())])

    def test_save_accepts_absolute_work_path_but_keeps_press_path_relative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            real = root / "real-media" / "Absolute Duo"
            (real / "Absolute Duo_BDRip").mkdir(parents=True)
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)

            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = collection_link_index_payload(st)
                work = payload["works"][0]
                body = {
                    "works": [
                        {
                            "yaml_source_rel": work["yaml_source_rel"],
                            "index_in_file": work["index_in_file"],
                            "path": str(real),
                            "press": [
                                {
                                    "press_key": work["press"][0]["press_key"],
                                    "press_path": "Absolute Duo_BDRip",
                                },
                            ],
                        },
                    ],
                }
                saved = save_link_index_from_ui_body(body, settings=st)

            saved_doc = load_yaml(fp)
            coll = saved_doc[0]["attributes"][1]["data"]
            self.assertEqual(Path(coll["path"]), real)
            self.assertEqual(coll["collectioned"][0]["press_path"], "Absolute Duo_BDRip")
            self.assertEqual(saved["plan_summary"]["ready"], 1)

    def test_payload_scans_all_db_yaml_and_marks_unmapped_disk_links(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            (media / "Mapped Work" / "BDRip VCB").mkdir(parents=True)
            (finish / "Loose").mkdir(parents=True)
            (finish / "Loose" / "Orphan.lnk").write_text("", encoding="utf-8")
            fp_visible = db / "[JP][TVInfo][2099].yaml"
            fp_extra = db / "[JP][TVInfo][2098].yaml"
            fp_visible.write_text(_catalog_yaml(), encoding="utf-8")
            fp_extra.write_text(_catalog_yaml_mapped("Mapped Work", "Mapped Work", "BDRip VCB"), encoding="utf-8")
            st = _settings(db, fp_visible)

            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                payload = collection_link_index_payload(st)

            self.assertEqual(len(payload["works"]), 2)
            self.assertEqual(payload["plan_summary"]["total"], 3)
            self.assertEqual(payload["plan_summary"]["ready"], 1)
            self.assertEqual(payload["plan_summary"]["empty_target_path"], 2)
            self.assertEqual(payload["plan_summary"]["unmapped_on_disk"], 1)
            self.assertFalse(payload["disk_summary"]["db_match_cached"])
            self.assertEqual(payload["mapping_summary"]["unconfigured_press"], 2)

    def test_generate_keeps_empty_target_items_in_index_db_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                },
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.feature_data_root",
                return_value=root / "data",
            ):
                generated = generate_link_index_from_ui_body({}, settings=st)

            index_doc = load_yaml(Path(generated["index_db"]["path"]))
            self.assertEqual(generated["index_db"]["item_count"], 2)
            self.assertEqual(generated["plan_summary"]["empty_target_path"], 2)
            self.assertEqual(len(index_doc["items"]), 2)
            self.assertTrue(all(not item.get("target_path") for item in index_doc["items"]))
            tree_link = _find_link_by_relpath(generated["tree"], "[2099]/Absolute Duo/BDRip-VCB.lnk")
            self.assertIsNotNone(tree_link)
            assert tree_link is not None
            self.assertEqual(tree_link["target_path"], "")

    def test_same_target_alias_does_not_satisfy_a_different_planned_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Mapped Work" / "BDRip VCB"
            target.mkdir(parents=True)
            shortcut = finish / "[2098]" / "[20980101] Mapped Work" / "BDRip.lnk"
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(_catalog_yaml_mapped("Mapped Work", "Mapped Work", "BDRip VCB"), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {"media_root": str(media), "shortcut_root": str(finish)},
                "link_index": {
                    "layout_levels": ["{year_label}", "{name}"],
                    "shortcut_name": "{press_format}-{press_group}",
                },
            }
            target_infos = {
                str(shortcut.resolve()): {
                    "target_path": str(target),
                    "target_resolved": True,
                    "error": "",
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                return_value=target_infos,
            ):
                payload = collection_link_index_payload(st)

            self.assertEqual(payload["plan_summary"]["unmapped_on_disk"], 1)
            tree_link = _find_link_by_relpath(payload["tree"], "[2098]/Mapped Work/BDRip-VCB.lnk")
            self.assertIsNotNone(tree_link)
            assert tree_link is not None
            self.assertFalse(tree_link["shortcut_exists"])
            self.assertFalse(tree_link["link_exists"])
            self.assertFalse(tree_link["db_linked"])
            self.assertEqual(tree_link["target_path"], str(target))

    def test_generate_preserves_physical_alias_without_renaming_it(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            media = root / "media"
            finish = root / "finish"
            target = media / "Mapped Work" / "BDRip VCB"
            target.mkdir(parents=True)
            old_shortcut = finish / "[2098]" / "[20980101] Mapped Work" / "BDRip.lnk"
            old_shortcut.parent.mkdir(parents=True)
            old_shortcut.write_text("shortcut", encoding="utf-8")
            expected_shortcut = finish / "[2098]" / "[20980101] Mapped Work" / "BDRip(VCB).lnk"
            fp = db / "[JP][TVInfo][2098].yaml"
            fp.write_text(_catalog_yaml_mapped("Mapped Work", "Mapped Work", "BDRip VCB"), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {"media_root": str(media), "shortcut_root": str(finish)},
            }

            def fake_targets(paths: list[Path]) -> dict[str, dict[str, str]]:
                return {
                    str(path.resolve()): {
                        "target_path": str(target),
                        "target_resolved": True,
                        "error": "",
                    }
                    for path in paths
                }

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index._windows_shortcut_targets",
                side_effect=fake_targets,
            ):
                result = generate_link_index_from_ui_body({}, settings=st)

            self.assertTrue(old_shortcut.exists())
            self.assertFalse(expected_shortcut.exists())
            self.assertEqual(result["index_db"]["item_count"], 1)
            self.assertEqual(result["plan_summary"]["renamed"], 0)
            self.assertEqual(result["plan_summary"]["created"], 0)
            self.assertEqual(result["plan_summary"]["unmapped_on_disk"], 1)
            self.assertEqual(result["plan"][0]["shortcut_relpath"], "[2098]/[20980101][20980331] Mapped Work/BDRip(VCB).lnk")

    def test_lite_payload_reloads_current_catalog_without_page_payload_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "db"
            db.mkdir()
            fp = db / "[JP][TVInfo][2099].yaml"
            fp.write_text(_catalog_yaml(), encoding="utf-8")
            st = _settings(db, fp)
            cfg = {
                "paths": {
                    "media_root": str(root / "media"),
                    "shortcut_root": str(root / "finish"),
                }
            }

            with patch("collection_detail.link_index._feature_config", return_value=cfg):
                first = collection_link_index_payload(st, lite=True)

            with patch("collection_detail.link_index._feature_config", return_value=cfg), patch(
                "collection_detail.link_index.CatalogRepository.load_works",
                autospec=True,
                side_effect=link_index_mod.CatalogRepository.load_works,
            ) as read_catalog:
                second = collection_link_index_payload(st, lite=True)

            self.assertEqual(read_catalog.call_count, 1)
            self.assertIsNot(first, second)
            self.assertNotIn("works", second)
            self.assertNotIn("plan", second)

    def test_open_shortcut_allows_target_outside_configured_roots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            finish = root / "finish"
            target = root / "resource" / "Absolute Duo" / "BDRip"
            shortcut = finish / "[2099]" / "Absolute Duo" / "BDRip.lnk"
            media.mkdir()
            target.mkdir(parents=True)
            shortcut.parent.mkdir(parents=True)
            shortcut.write_text("", encoding="utf-8")
            cfg = {
                "paths": {
                    "media_root": str(media),
                    "shortcut_root": str(finish),
                }
            }

            with (
                patch("collection_detail.link_index._feature_config", return_value=cfg),
                patch("collection_detail.link_index._windows_shortcut_target", return_value=str(target)),
                patch("collection_detail.link_index.os.name", "nt"),
                patch("collection_detail.link_index.os.startfile", create=True) as startfile,
            ):
                result = open_link_index_path_from_ui_body({"path": str(shortcut)})

            self.assertEqual(Path(result["path"]), target.resolve())
            self.assertEqual(Path(result["source_path"]), shortcut.resolve())
            self.assertTrue(result["resolved_from_shortcut"])
            startfile.assert_called_once_with(str(target.resolve()))


if __name__ == "__main__":
    unittest.main()
