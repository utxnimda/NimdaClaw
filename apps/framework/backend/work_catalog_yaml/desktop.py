"""Native Windows desktop shell for the Nimda local web application.

The GUI and Uvicorn run in one process.  Closing the final desktop window asks
Uvicorn to shut down and then lets the process exit, so no server process can
outlive the desktop application.
"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_APPLICATION_ROOT_ENV = "NIMDA_APPLICATION_ROOT"
_WORKSPACE_ROOT_ENV = "NIMDA_WORKSPACE_ROOT"
_DESKTOP_CONFIG_NAME = "nimda-desktop.yaml"
_SINGLE_INSTANCE_NAME = "Local\\Nimda.Desktop.Singleton"
_ERROR_ALREADY_EXISTS = 183
_LOG = logging.getLogger("nimda.desktop")


def _looks_like_application_root(path: Path) -> bool:
    return (path / "apps").is_dir()


def _looks_like_workspace(path: Path) -> bool:
    return all((path / name).is_dir() for name in ("config", "data"))


def resolve_desktop_workspace(explicit: str | Path | None = None) -> Path:
    """Resolve the application-resource root used by source and frozen runs."""
    candidates: list[Path] = []
    configured = os.environ.get(_APPLICATION_ROOT_ENV, "").strip()
    selected = explicit if explicit is not None else configured
    if selected:
        root = Path(selected).expanduser().resolve()
        if not _looks_like_application_root(root):
            raise FileNotFoundError(f"Nimda 应用资源目录无效：{root}（缺少 apps 目录）")
        return root
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    shared_workspace = os.environ.get(_WORKSPACE_ROOT_ENV, "").strip()
    if shared_workspace:
        candidates.append(Path(shared_workspace).expanduser())
    here = Path(__file__).resolve()
    candidates.extend(here.parents)
    candidates.append(Path.cwd())

    seen: set[str] = set()
    for candidate in candidates:
        try:
            root = candidate.resolve()
        except OSError:
            continue
        key = str(root).casefold()
        if key in seen:
            continue
        seen.add(key)
        if _looks_like_application_root(root):
            return root
    checked = "、".join(str(path) for path in candidates[:4])
    raise FileNotFoundError(f"找不到 Nimda 应用资源目录（已检查：{checked}）")


@dataclass(frozen=True)
class DesktopConfiguration:
    application_root: Path
    workspace_root: Path
    config_path: Path | None


def _resolve_configured_path(value: str, *, relative_to: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = relative_to / path
    return path.resolve()


def load_desktop_configuration(
    application_root: Path,
    desktop_config: str | Path | None = None,
) -> DesktopConfiguration:
    """Load the packaged desktop bootstrap config.

    ``nimda-desktop.yaml`` lives beside ``Nimda.exe`` and only selects the
    mutable workspace.  The application's normal configuration and databases
    remain under that shared workspace instead of being copied into the package.
    """
    application_root = application_root.resolve()
    config_path = (
        Path(desktop_config).expanduser().resolve()
        if desktop_config is not None
        else application_root / _DESKTOP_CONFIG_NAME
    )
    if not config_path.is_file():
        if desktop_config is not None or getattr(sys, "frozen", False):
            raise FileNotFoundError(
                f"找不到桌面版配置：{config_path}。请确认该文件与 Nimda.exe 位于同一目录。"
            )
        configured = os.environ.get(_WORKSPACE_ROOT_ENV, "").strip()
        workspace = Path(configured).expanduser().resolve() if configured else application_root
        if not _looks_like_workspace(workspace):
            raise ValueError(f"Nimda 工作区无效：{workspace}（需要 config 和 data 目录）")
        return DesktopConfiguration(application_root, workspace, None)

    from work_catalog_yaml.yaml_io import load_yaml

    raw = load_yaml(config_path)
    if not isinstance(raw, dict):
        raise ValueError(f"桌面版配置必须是 YAML 对象：{config_path}")
    paths = raw.get("paths")
    if not isinstance(paths, dict):
        paths = raw
    workspace_value = paths.get("workspace_root")
    if not isinstance(workspace_value, str) or not workspace_value.strip():
        raise ValueError(f"桌面版配置缺少 paths.workspace_root：{config_path}")
    workspace = _resolve_configured_path(workspace_value.strip(), relative_to=config_path.parent)
    if not _looks_like_workspace(workspace):
        raise ValueError(
            f"桌面版共享工作区无效：{workspace}（需要 config 和 data 目录；配置：{config_path}）"
        )
    return DesktopConfiguration(application_root, workspace, config_path.resolve())


def configure_desktop_workspace(application_root: Path, workspace_root: Path | None = None) -> None:
    from work_catalog_yaml.layout import ensure_feature_backend_paths

    application_root = application_root.resolve()
    workspace_root = (workspace_root or application_root).resolve()
    os.environ[_APPLICATION_ROOT_ENV] = str(application_root)
    os.environ[_WORKSPACE_ROOT_ENV] = str(workspace_root)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.chdir(workspace_root)
    ensure_feature_backend_paths(application_root)


def _configure_desktop_logging(root: Path) -> Path:
    from work_catalog_yaml.common.application_logging import configure_application_logging

    handler = configure_application_logging(root / "data" / "framework" / "logs" / "application",
                                            filename="desktop.log")
    return Path(handler.baseFilename)


def _bind_desktop_socket(host: str, preferred_port: int) -> socket.socket:
    ports = [preferred_port, 0] if preferred_port > 0 else [0]
    last_error: OSError | None = None
    for port in ports:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((host, port))
            listener.listen(2048)
            listener.set_inheritable(False)
            return listener
        except OSError as exc:
            last_error = exc
            listener.close()
    raise OSError(f"无法为 Nimda 桌面服务分配本机端口：{last_error}")


class DesktopServer:
    """Run one Uvicorn server in a background thread owned by the GUI process."""

    def __init__(
        self,
        app: Any,
        *,
        host: str = "127.0.0.1",
        preferred_port: int = 8765,
        startup_timeout: float = 15.0,
        shutdown_timeout: float = 8.0,
    ) -> None:
        self.app = app
        self.host = host
        self.preferred_port = preferred_port
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.port = 0
        self.url = ""
        self._listener: socket.socket | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._serve_error: BaseException | None = None
        self._stop_lock = threading.RLock()
        self._stopped = False

    def start(self) -> str:
        if self._thread and self._thread.is_alive():
            return self.url

        import uvicorn

        self._stopped = False
        self._server = None
        self._thread = None
        self._listener = _bind_desktop_socket(self.host, self.preferred_port)
        self.port = int(self._listener.getsockname()[1])
        self.url = f"http://{self.host}:{self.port}/"
        try:
            config = uvicorn.Config(
                self.app,
                host=self.host,
                port=self.port,
                loop="asyncio",
                http="h11",
                ws="none",
                log_config=None,
                log_level="info",
                access_log=False,
                server_header=False,
                timeout_graceful_shutdown=5,
            )
            self._server = uvicorn.Server(config)
            self._serve_error = None
            self._thread = threading.Thread(
                target=self._serve,
                name="nimda-uvicorn",
                daemon=True,
            )
            self._thread.start()
        except BaseException:
            self.stop()
            raise

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if bool(getattr(self._server, "started", False)):
                _LOG.info("Desktop server ready at %s", self.url)
                return self.url
            if self._serve_error is not None:
                break
            if not self._thread.is_alive():
                break
            time.sleep(0.05)

        error = self._serve_error
        self.stop()
        if error is not None:
            raise RuntimeError(f"Nimda 桌面服务启动失败：{error}") from error
        raise TimeoutError(f"Nimda 桌面服务在 {self.startup_timeout:g} 秒内未就绪")

    def _serve(self) -> None:
        try:
            self._server.run(sockets=[self._listener])
        except BaseException as exc:  # include uvicorn's SystemExit on startup failure
            self._serve_error = exc
            _LOG.exception("Desktop server stopped unexpectedly")
        finally:
            listener = self._listener
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass

    def stop(self) -> bool:
        from work_catalog_yaml.api_runtime import api_work_queues

        with self._stop_lock:
            if self._stopped:
                return True
            thread = self._thread
            server = self._server
            queues = api_work_queues(self.app)
            for queue in queues:
                queue.begin_shutdown()
            if server is not None:
                server.should_exit = True
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(self.shutdown_timeout)
            # The ASGI loop can fail before its worker completes. Waiting is
            # independent of server-thread liveness so the workspace mutex is
            # never released while this instance still owns an active write.
            if any(queue.snapshot()["running"] for queue in queues):
                _LOG.info("Waiting for started API work to finish before desktop exit")
                for queue in queues:
                    queue.wait_for_running()
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(2.0)
                if thread.is_alive() and server is not None:
                    _LOG.warning("Graceful shutdown timed out; forcing Uvicorn exit")
                    server.force_exit = True
                    thread.join(2.0)
            listener = self._listener
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            stopped = thread is None or not thread.is_alive()
            if stopped:
                self._stopped = True
                _LOG.info("Desktop server stopped")
            else:
                _LOG.error("Desktop server thread did not stop before application exit")
            return stopped


class SingleInstance:
    """Named Windows mutex that prevents concurrent catalog editors."""

    def __init__(self, name: str = _SINGLE_INSTANCE_NAME) -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        self._handle = int(handle)
        return True

    def release(self) -> None:
        if os.name != "nt" or self._handle is None:
            return
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(self._handle))
        self._handle = None


def _runtime_state_path(root: Path) -> Path:
    return root / "data" / "framework" / "runtime" / "desktop.json"


def _write_runtime_state(root: Path, server: DesktopServer) -> Path:
    path = _runtime_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "host": server.host,
        "port": server.port,
        "url": server.url,
        "workspace": str(root),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def _remove_runtime_state(path: Path) -> None:
    try:
        if not path.is_file():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("pid") or 0) == os.getpid():
            path.unlink(missing_ok=True)
    except (OSError, ValueError, json.JSONDecodeError):
        _LOG.warning("Could not remove desktop runtime state", exc_info=True)


def _show_error(title: str, message: str) -> None:
    _LOG.error("%s: %s", title, message)
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
            return
        except (AttributeError, OSError):
            pass
    print(f"{title}: {message}", file=sys.stderr)


def _show_information(title: str, message: str) -> None:
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
            return
        except (AttributeError, OSError):
            pass
    print(f"{title}: {message}")


def _load_application() -> Any:
    from work_catalog_yaml.jp_tv.browse_app import app

    return app


def _desktop_storage_path() -> Path:
    base = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".nimda"
    path = root / "Nimda" / "WebView2"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_desktop(
    *,
    workspace: str | Path | None = None,
    desktop_config: str | Path | None = None,
    preferred_port: int = 8765,
    debug: bool = False,
) -> int:
    try:
        application_root = resolve_desktop_workspace(workspace)
        configuration = load_desktop_configuration(application_root, desktop_config)
    except Exception as exc:
        _show_error("Nimda 启动失败", str(exc))
        return 1
    shared_workspace = configuration.workspace_root
    configure_desktop_workspace(application_root, shared_workspace)
    log_path = _configure_desktop_logging(shared_workspace)
    _LOG.info(
        "Starting Nimda desktop (application=%s, workspace=%s, desktop_config=%s)",
        application_root,
        shared_workspace,
        configuration.config_path or "source defaults",
    )

    instance = SingleInstance()
    if not instance.acquire():
        _show_information("Nimda", "Nimda 已经在运行。")
        return 2

    server: DesktopServer | None = None
    runtime_path: Path | None = None
    try:
        server = DesktopServer(_load_application(), preferred_port=preferred_port)
        server.start()
        atexit.register(server.stop)
        runtime_path = _write_runtime_state(shared_workspace, server)

        try:
            import webview
        except ImportError as exc:
            raise RuntimeError("缺少桌面依赖 pywebview；请运行桌面构建脚本。") from exc

        window = webview.create_window(
            "Nimda",
            server.url,
            width=1480,
            height=920,
            min_size=(1050, 680),
            background_color="#101827",
            text_select=True,
        )
        from work_catalog_yaml.common.native_dialogs import set_directory_picker

        def pick_directory(initial):
            from work_catalog_yaml.paths import normalize_copied_path
            directory = normalize_copied_path(initial)
            if directory and not Path(directory).is_dir():
                directory = ""
            selected = window.create_file_dialog(webview.FOLDER_DIALOG, directory=directory, allow_multiple=False)
            return selected[0] if selected else ""

        set_directory_picker(pick_directory)
        window.events.closed += lambda *_args: server.stop()
        webview.start(
            gui="edgechromium",
            debug=debug,
            private_mode=False,
            storage_path=str(_desktop_storage_path()),
        )
        return 0
    except KeyboardInterrupt:
        _LOG.info("Desktop application interrupted")
        return 130
    except Exception as exc:
        _LOG.exception("Nimda desktop failed")
        _show_error("Nimda 启动失败", f"{exc}\n\n日志：{log_path}")
        return 1
    finally:
        from work_catalog_yaml.common.native_dialogs import set_directory_picker
        set_directory_picker(None)
        if server is not None:
            server.stop()
            atexit.unregister(server.stop)
        if runtime_path is not None:
            _remove_runtime_state(runtime_path)
        instance.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nimda native desktop application")
    parser.add_argument("--workspace", help="Nimda application resource root (source-mode override)")
    parser.add_argument(
        "--desktop-config",
        help=f"desktop bootstrap config (default: {_DESKTOP_CONFIG_NAME} beside Nimda.exe)",
    )
    parser.add_argument("--port", type=int, default=8765, help="preferred loopback port")
    parser.add_argument("--debug", action="store_true", help="enable WebView developer tools")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_desktop(
        workspace=args.workspace,
        desktop_config=args.desktop_config,
        preferred_port=args.port,
        debug=args.debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())
