from __future__ import annotations

import ast
import asyncio
from contextlib import redirect_stderr
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from starlette.routing import Mount

from collection_detail import link_index
from collection_detail.catalog_repository import CatalogRepository
from work_catalog_yaml.cli import build_parser
from work_catalog_yaml.jp_tv.browse_app import build_jp_tv_browse_app
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml
from test_api_runtime import request as app_request


REPOSITORY = Path(__file__).resolve().parents[3]
FRAMEWORK_PACKAGE = REPOSITORY / "apps" / "framework" / "backend" / "work_catalog_yaml"
FEATURE_PACKAGE = REPOSITORY / "apps" / "features" / "collection-detail" / "backend" / "collection_detail"


def imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
    return imports


def settings_for(db: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1, filesystem_root=db, resolved_default_readable=None,
        resolved_catalog_yaml_paths=(), enum_options={}, enum_labels={},
        enum_section_labels={}, app_features=(),
    )


def work_record(name: str, path: Path) -> dict:
    return {"attributes": [
        {"type": "date", "data": {"start": "20990101", "end": "20990331"}},
        {"type": "collection-type", "data": {
            "domain": "animation", "release_type": "tv", "path": path.as_posix(),
            "collectioned": [{"press_format": "BDRip", "press_group": "", "press_path": "shared_BDRip"}],
            "markers": [],
        }},
        {"type": "country", "data": "japan"},
        {"type": "name", "data": name},
    ]}


class ArchitectureBoundaryTest(unittest.TestCase):
    def test_retired_organizer_has_no_code_config_or_script_entry(self):
        for relative in (
            "apps/features/media-directory-organizer/backend",
            "apps/features/media-directory-organizer/frontend",
            "config/features/media-directory-organizer/config.yaml",
            "scripts/organize-media-directories.ps1",
        ):
            with self.subTest(path=relative):
                self.assertFalse((REPOSITORY / relative).exists())
        framework_config = load_yaml(REPOSITORY / "config" / "framework" / "app.yaml")
        feature_ids = [feature["id"] for feature in framework_config["app"]["features"]]
        self.assertEqual(feature_ids, ["collection-detail", "collection-info", "directory-organizer"])
        parser = build_parser()
        self.assertNotIn("media-directory-organizer", parser.format_help())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
            parser.parse_args(["media-directory-organizer"])
        self.assertEqual(rejected.exception.code, 2)

    def test_framework_shared_layer_never_imports_features(self):
        # Composition/API adapters and legacy jp_tv compatibility shims are
        # intentionally not shared infrastructure. They may wire features.
        shared = [*FRAMEWORK_PACKAGE.glob("*.py"), *(FRAMEWORK_PACKAGE / "storage").rglob("*.py"),
                  *(FRAMEWORK_PACKAGE / "common").rglob("*.py")]
        self.assertTrue(shared)
        for path in shared:
            with self.subTest(module=path.relative_to(FRAMEWORK_PACKAGE)):
                forbidden = [name for name in imported_modules(path) if name.split(".", 1)[0] in {
                    "collection_detail", "collection_info", "media_directory_organizer", "directory_organizer",
                }]
                self.assertEqual(forbidden, [])

    def test_catalog_repository_does_not_depend_on_page_disk_or_indexes(self):
        dependencies = imported_modules(FEATURE_PACKAGE / "catalog_repository.py")
        for dependency in dependencies:
            with self.subTest(module=dependency):
                self.assertFalse(any(part in dependency.split(".") for part in (
                    "link_index", "resource_cache", "resource_tree", "windows_shortcuts", "filesystem",
                )))

    def test_repository_and_disk_imports_do_not_initialize_presentation(self):
        program = (
            "import sys\n"
            "from work_catalog_yaml.storage.disk_inventory import DiskInventory\n"
            "from collection_detail.catalog_repository import CatalogRepository\n"
            "from collection_detail.catalog_inventory import CatalogInventoryRepository\n"
            "for name in ('collection_detail.payload', 'collection_detail.link_index', "
            "'collection_detail.work_detail', 'work_catalog_yaml.jp_tv.browse_api'):\n"
            "    assert name not in sys.modules, name\n"
            "assert not any(name.startswith('media_directory_organizer') for name in sys.modules)\n"
            "print('fresh-inventory-import-ok')\n"
        )
        result = subprocess.run([sys.executable, "-c", program], cwd=REPOSITORY,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fresh-inventory-import-ok", result.stdout)

    def test_feature_http_modules_do_not_import_peer_features(self):
        for package, peer in (("collection_detail", "collection_info"), ("collection_info", "collection_detail")):
            path = REPOSITORY / "apps" / "features" / package.replace("_", "-") / "backend" / package / "web.py"
            with self.subTest(feature=package):
                self.assertFalse(any(name.split(".", 1)[0] == peer for name in imported_modules(path)))

    def test_saved_work_and_collection_counts_are_not_directory_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            db = temporary / "feature" / "db"
            db.mkdir(parents=True)
            media = temporary / "media" / "Shared work"
            media.mkdir(parents=True)
            (media / "shared_BDRip").mkdir()
            for index in range(5):
                (media / f"extra-{index}").mkdir()
            source = db / "catalog.yaml"
            source.write_text(dump_yaml_string([
                work_record("Shared work season 1", media),
                work_record("Shared work season 2", media),
            ]), encoding="utf-8")
            # Derived index and disk observations cannot supply catalog rows.
            (db / "index").mkdir()
            (db / "index" / "link-index.yaml").write_text("not: catalog\n", encoding="utf-8")
            (db / "link-index.yaml").write_text("not: catalog\n", encoding="utf-8")
            cache = temporary / "feature" / "cache"
            cache.mkdir()
            (cache / "resource-library-scan-cache.yaml").write_text("not: catalog\n", encoding="utf-8")
            repository = CatalogRepository(settings_for(db))
            self.assertEqual(repository.catalog_paths(), [source.resolve()])
            before = source.read_bytes()
            works = repository.load_works()
            self.assertEqual(len(works), 2)
            self.assertEqual(sum(len(work["press"]) for work in works), 2)
            self.assertEqual(len({work["path"] for work in works}), 1)
            self.assertNotEqual(works[0]["work_key"], works[1]["work_key"])
            self.assertEqual(source.read_bytes(), before)
            # DB-only remains DB-only: unavailable media must not erase records.
            original_is_dir = Path.is_dir
            with patch.object(Path, "is_dir", autospec=True, side_effect=lambda path: (
                False if path == media or media in path.parents else original_is_dir(path)
            )):
                self.assertEqual(repository.load_works(), works)
            # Disk-only remains disk-only: no automatic catalog creation.
            empty_db = temporary / "empty-db"
            empty_db.mkdir()
            self.assertEqual(CatalogRepository(settings_for(empty_db)).load_works(), [])

    def test_disk_caches_and_derived_index_have_distinct_storage_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            feature_data = Path(directory) / "collection-detail"
            with patch.object(link_index, "feature_data_root", return_value=feature_data):
                scan = link_index._resource_scan_cache_path()
                shortcut_scan = link_index._shortcut_scan_cache_path()
                nodes = link_index._resource_scan_node_dir()
                index = link_index._link_index_db_path()
            for path in (scan, shortcut_scan, nodes):
                self.assertEqual(path.parent, feature_data / "cache")
            self.assertEqual(index.parent, feature_data / "db" / "index")
            self.assertNotIn(index, (scan, shortcut_scan, nodes))


class RetiredOrganizerRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def test_library_status_get_preserves_read_only_contract_and_rejects_post(self):
        from collection_detail import library_status, web

        configured_settings = object()
        expected = {
            "ok": True, "read_only": True,
            "database": {"work_record_count": 2, "press_record_count": 3},
            "relationships": {"counts": {"both": 1, "db-only": 1}, "works": []},
            "resource_tree": {"cached": True, "directory_count": 20, "file_count": 100},
        }
        app = build_jp_tv_browse_app()
        async with app.router.lifespan_context(app):
            with patch.object(web, "get_resolved_browse_settings", return_value=(configured_settings, None)), patch.object(
                library_status, "library_status_payload", return_value=expected,
            ) as aggregate:
                status, payload = await app_request(app, "/api/collection-detail/library-status")
                self.assertEqual(status, 200)
                self.assertEqual(payload, expected)
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["read_only"])
                aggregate.assert_called_once_with(configured_settings)
                post_status, _payload = await app_request(
                    app, "/api/collection-detail/library-status", method="POST", body=b"{}",
                )
                self.assertEqual(post_status, 405)
                aggregate.assert_called_once()

    async def test_old_routes_are_absent_and_return_404(self):
        app = build_jp_tv_browse_app()
        self.assertFalse(any(
            "media-directory-organizer" in route.path
            for mount in app.routes if isinstance(mount, Mount)
            for route in getattr(mount.app, "routes", [])
        ))
        self.assertFalse(any("media-directory-organizer" in route.path for route in app.routes))
        async with app.router.lifespan_context(app):
            for path, method in (
                ("/api/media-directory-organizer/config", "GET"),
                ("/api/media-directory-organizer/preview", "POST"),
                ("/api/media-directory-organizer/apply", "POST"),
                ("/api/media-directory-organizer/landing/apply", "POST"),
                ("/features/media-directory-organizer/index.js", "GET"),
            ):
                with self.subTest(path=path):
                    messages = []
                    received = False

                    async def receive():
                        nonlocal received
                        if not received:
                            received = True
                            return {"type": "http.request", "body": b"{}", "more_body": False}
                        await asyncio.Event().wait()

                    async def send(message):
                        messages.append(message)

                    await app({
                        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                        "root_path": "", "query_string": b"", "server": ("127.0.0.1", 8765),
                        "client": ("127.0.0.1", 12345),
                        "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json")],
                    }, receive, send)
                    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
                    self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
