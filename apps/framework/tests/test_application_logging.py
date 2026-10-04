from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from work_catalog_yaml.common.application_logging import (
    DailyApplicationFileHandler, configure_application_logging,
)
from work_catalog_yaml.desktop import _configure_desktop_logging


@contextmanager
def isolated_logger():
    logger = logging.Logger("nimda-test-application")
    logger.propagate = False
    try:
        yield logger
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


class ApplicationLoggingTest(unittest.TestCase):
    def test_midnight_switch_and_restart_append_without_replacing_history(self):
        with tempfile.TemporaryDirectory() as directory, isolated_logger() as logger:
            root = Path(directory) / "application"
            now = [datetime(2026, 10, 3, 23, 59, 59)]
            handler = configure_application_logging(root, filename="desktop.log", logger=logger, clock=lambda: now[0])
            logger.info("第一天")
            previous_stream = handler.stream
            first = root / "2026-10-03" / "desktop.log"
            original = first.read_bytes()
            now[0] = datetime(2026, 10, 4, 0, 0, 1)
            logger.info("第二天")
            second = root / "2026-10-04" / "desktop.log"
            self.assertTrue(previous_stream.closed)
            self.assertEqual(first.read_bytes(), original)
            self.assertIn("第二天", second.read_text(encoding="utf-8"))
            self.assertEqual(Path(handler.baseFilename), second)
            handler.close()
            logger.removeHandler(handler)
            configure_application_logging(root, filename="desktop.log", logger=logger, clock=lambda: now[0])
            logger.info("同一天重启")
            text = second.read_text(encoding="utf-8")
            self.assertEqual(text.count("第二天"), 1)
            self.assertEqual(text.count("同一天重启"), 1)

    def test_repeated_and_parallel_configuration_reuse_one_handler(self):
        with tempfile.TemporaryDirectory() as directory, isolated_logger() as logger:
            def configure(_index):
                return configure_application_logging(Path(directory), filename="desktop.log", logger=logger)
            with ThreadPoolExecutor(max_workers=4) as pool:
                handlers = list(pool.map(configure, range(12)))
                list(pool.map(lambda index: logger.info("行%d", index), range(30)))
            self.assertTrue(all(handler is handlers[0] for handler in handlers))
            self.assertEqual(len(logger.handlers), 1)
            lines = Path(handlers[0].baseFilename).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 30)
            self.assertEqual(len({line.split(": 行")[1] for line in lines}), 30)

    def test_chinese_exception_retains_message_and_traceback(self):
        with tempfile.TemporaryDirectory() as directory, isolated_logger() as logger:
            handler = configure_application_logging(Path(directory), logger=logger)
            try:
                raise ValueError("中文错误：路径不存在")
            except ValueError:
                logger.exception("处理失败")
            text = Path(handler.baseFilename).read_text(encoding="utf-8")
            self.assertIn("处理失败", text)
            self.assertIn("Traceback (most recent call last):", text)
            self.assertIn("ValueError: 中文错误：路径不存在", text)

    def test_desktop_wrapper_preserves_all_legacy_log_files(self):
        with tempfile.TemporaryDirectory() as directory, isolated_logger() as logger:
            workspace = Path(directory)
            old = workspace / "data" / "framework" / "logs"
            old.mkdir(parents=True)
            history = {old / name: ("旧日志 " + name).encode("utf-8")
                       for name in ("desktop.log", "desktop.log.1", "desktop.log.2", "desktop.log.3")}
            for path, content in history.items():
                path.write_bytes(content)
            with patch("work_catalog_yaml.common.application_logging.logging.getLogger", return_value=logger):
                actual = _configure_desktop_logging(workspace)
                self.assertEqual(actual, _configure_desktop_logging(workspace))
            logger.info("新应用日志")
            self.assertEqual(actual.parent.parent, old / "application")
            self.assertEqual(actual.name, "desktop.log")
            self.assertRegex(actual.parent.name, r"^\d{4}-\d{2}-\d{2}$")
            self.assertIn("新应用日志", actual.read_text(encoding="utf-8"))
            for path, content in history.items():
                self.assertEqual(path.read_bytes(), content)

    def test_unsafe_directory_is_rejected_before_creating_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "unsafe" / "application"
            with patch("work_catalog_yaml.common.log_files.ordinary_directory", return_value=False):
                with self.assertRaisesRegex(OSError, "不是普通目录"):
                    DailyApplicationFileHandler(root, "desktop.log")
            self.assertFalse(root.parent.exists())

    def test_existing_hardlink_is_never_appended(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 10, 3)
            daily = root / "2026-10-03"
            daily.mkdir()
            protected = root / "protected.txt"
            protected.write_text("原文件", encoding="utf-8")
            os.link(protected, daily / "desktop.log")
            with self.assertRaisesRegex(OSError, "普通文件"):
                DailyApplicationFileHandler(root, "desktop.log", clock=lambda: now)
            self.assertEqual(protected.read_text(encoding="utf-8"), "原文件")

    def test_filename_cannot_escape_daily_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in ("../desktop.log", "x/desktop.log", r"x\desktop.log", "D:desktop.log", ""):
                with self.subTest(filename=filename), self.assertRaises(ValueError):
                    DailyApplicationFileHandler(Path(directory), filename)


if __name__ == "__main__":
    unittest.main()
