from __future__ import annotations

import os
import socket
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from work_catalog_yaml.desktop import (
    DesktopServer,
    configure_desktop_workspace,
    load_desktop_configuration,
    resolve_desktop_workspace,
)
from work_catalog_yaml.layout import (
    application_root,
    feature_config_path,
    feature_data_root,
    framework_frontend_root,
    workspace_root,
)


def _test_app() -> Starlette:
    async def health(_request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/", health)])


class DesktopAppTest(unittest.TestCase):
    def test_workspace_environment_override_is_used_by_desktop_and_layout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("apps", "config", "data"):
                (root / name).mkdir()
            with patch.dict(
                os.environ,
                {
                    "NIMDA_APPLICATION_ROOT": str(root),
                    "NIMDA_WORKSPACE_ROOT": str(root),
                },
            ):
                self.assertEqual(resolve_desktop_workspace(), root.resolve())
                self.assertEqual(workspace_root(), root.resolve())

    def test_packaged_application_and_shared_workspace_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            app_root = base / "package"
            shared_root = base / "shared"
            (app_root / "apps" / "framework" / "frontend").mkdir(parents=True)
            (shared_root / "config" / "features" / "collection-detail").mkdir(parents=True)
            (shared_root / "data" / "features" / "collection-detail").mkdir(parents=True)
            original_cwd = Path.cwd()
            try:
                with patch.dict(os.environ, {}, clear=False):
                    configure_desktop_workspace(app_root, shared_root)
                    self.assertEqual(application_root(), app_root.resolve())
                    self.assertEqual(workspace_root(), shared_root.resolve())
                    self.assertEqual(
                        framework_frontend_root(),
                        (app_root / "apps" / "framework" / "frontend").resolve(),
                    )
                    self.assertEqual(
                        feature_config_path("collection-detail"),
                        (shared_root / "config" / "features" / "collection-detail" / "config.yaml").resolve(),
                    )
                    self.assertEqual(
                        feature_data_root("collection-detail"),
                        (shared_root / "data" / "features" / "collection-detail").resolve(),
                    )
                    self.assertEqual(Path.cwd(), shared_root.resolve())
            finally:
                os.chdir(original_cwd)

    def test_desktop_config_selects_relative_shared_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            app_root = base / "package"
            shared_root = base / "shared"
            (app_root / "apps").mkdir(parents=True)
            (shared_root / "config").mkdir(parents=True)
            (shared_root / "data").mkdir(parents=True)
            config_path = app_root / "nimda-desktop.yaml"
            config_path.write_text(
                "version: 1\npaths:\n  workspace_root: '../shared'\n",
                encoding="utf-8",
            )

            configuration = load_desktop_configuration(app_root)

            self.assertEqual(configuration.application_root, app_root.resolve())
            self.assertEqual(configuration.workspace_root, shared_root.resolve())
            self.assertEqual(configuration.config_path, config_path.resolve())

    def test_frozen_desktop_requires_its_bootstrap_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_root = Path(td)
            (app_root / "apps").mkdir()
            with patch.object(sys, "frozen", True, create=True):
                with self.assertRaisesRegex(FileNotFoundError, "nimda-desktop.yaml"):
                    load_desktop_configuration(app_root)

    def test_packaging_spec_does_not_copy_workspace_config_or_databases(self) -> None:
        repo_root = Path(__file__).resolve().parents[4]
        spec = (repo_root / "packaging" / "nimda-desktop.spec").read_text(encoding="utf-8")
        self.assertNotIn('add_tree(datas, repo_root / "config"', spec)
        self.assertNotIn('repo_root / "data" / "features"', spec)

    def test_server_stops_and_releases_its_port(self) -> None:
        server = DesktopServer(
            _test_app(),
            preferred_port=0,
            startup_timeout=5,
            shutdown_timeout=5,
        )
        url = server.start()
        self.addCleanup(server.stop)

        with urllib.request.urlopen(url, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")

        port = server.port
        self.assertTrue(server.stop())
        self.assertTrue(server.stop())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            self.assertNotEqual(probe.connect_ex((server.host, port)), 0)

    def test_occupied_preferred_port_is_not_reused_or_stopped(self) -> None:
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        occupied_port = int(occupied.getsockname()[1])

        server = DesktopServer(
            _test_app(),
            preferred_port=occupied_port,
            startup_timeout=5,
            shutdown_timeout=5,
        )
        server.start()
        self.addCleanup(server.stop)

        self.assertNotEqual(server.port, occupied_port)
        self.assertEqual(occupied.getsockname()[1], occupied_port)


if __name__ == "__main__":
    unittest.main()
