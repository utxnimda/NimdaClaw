from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from work_catalog_yaml.api_runtime import api_lifespan, install_api_queues, query_endpoint
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
    def test_invalid_explicit_application_root_does_not_silently_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(FileNotFoundError, "apps"):
                resolve_desktop_workspace(td)

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

    def test_shutdown_waits_for_started_api_work_before_releasing_server(self) -> None:
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        stop_finished = threading.Event()
        responses = []
        failures = []
        stop_results = []

        @query_endpoint
        def slow_write(_query):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test worker was not released")
            completed.set()
            return PlainTextResponse("saved")

        app = Starlette(routes=[Route("/", slow_write)], lifespan=api_lifespan)
        install_api_queues(app)
        server = DesktopServer(app, preferred_port=0, startup_timeout=5, shutdown_timeout=0.01)
        url = server.start()

        def client():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    responses.append(response.read())
            except Exception as exc:
                failures.append(exc)

        def close():
            try:
                stop_results.append(server.stop())
            finally:
                stop_finished.set()

        client_thread = threading.Thread(target=client)
        close_thread = threading.Thread(target=close)
        try:
            client_thread.start()
            self.assertTrue(started.wait(2))
            close_thread.start()
            self.assertFalse(stop_finished.wait(0.1))
            self.assertFalse(completed.is_set())
            self.assertTrue(app.state.api_disk_queue.snapshot()["closing"])
        finally:
            release.set()
            client_thread.join(6)
            if close_thread.ident is not None:
                close_thread.join(6)
            server.stop()

        self.assertFalse(client_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(responses, [b"saved"])
        self.assertEqual(stop_results, [True])
        self.assertTrue(completed.is_set())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            self.assertNotEqual(probe.connect_ex((server.host, server.port)), 0)

    def test_server_setup_failure_releases_listener_without_caller_cleanup(self) -> None:
        server = DesktopServer(_test_app(), preferred_port=0)
        with patch("uvicorn.Config", side_effect=ValueError("invalid server configuration")):
            with self.assertRaisesRegex(ValueError, "invalid server configuration"):
                server.start()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            self.assertNotEqual(probe.connect_ex((server.host, server.port)), 0)
        self.assertTrue(server.stop())

    def test_shutdown_waits_for_worker_even_after_server_thread_has_exited(self) -> None:
        started, release, completed = threading.Event(), threading.Event(), threading.Event()
        stop_finished = threading.Event()
        results = []
        app = _test_app()
        install_api_queues(app)
        server = DesktopServer(app)
        server._thread = threading.Thread(target=lambda: None)
        server._thread.start()
        server._thread.join()

        def write():
            started.set()
            if not release.wait(5):
                raise TimeoutError("test worker was not released")
            completed.set()

        async def disconnected_request():
            task = asyncio.create_task(app.state.api_disk_queue.run(write))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        def close():
            try:
                results.append(server.stop())
            finally:
                stop_finished.set()

        close_thread = threading.Thread(target=close)
        try:
            # The request and its loop go away, but the worker still owns its write.
            asyncio.run(disconnected_request())
            close_thread.start()
            self.assertFalse(stop_finished.wait(0.1))
            self.assertFalse(completed.is_set())
        finally:
            release.set()
            if close_thread.ident is not None:
                close_thread.join(6)
            server.stop()

        self.assertTrue(stop_finished.is_set())
        self.assertTrue(completed.is_set())
        self.assertEqual(results, [True])


if __name__ == "__main__":
    unittest.main()
