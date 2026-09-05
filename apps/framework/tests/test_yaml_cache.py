from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ruamel.yaml.error import YAMLError

from work_catalog_yaml import yaml_io
from work_catalog_yaml.yaml_cache import YamlParseCache


class YamlCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        yaml_io._PARSED_YAML_CACHE.clear()

    def test_path_and_text_share_parse_but_not_mutable_results(self) -> None:
        source = "works:\n  - name: One\n    tags: [a, b]\n"
        with tempfile.TemporaryDirectory() as td, patch.object(yaml_io, "_yaml_reader", wraps=yaml_io._yaml_reader) as reader:
            path = Path(td) / "db.yaml"
            path.write_text(source, encoding="utf-8")
            first = yaml_io.load_yaml(path)
            first["works"][0]["tags"].append("changed")
            second = yaml_io.load_yaml_string(source)
            second["works"].clear()
            third = yaml_io.load_yaml(path)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(third, {"works": [{"name": "One", "tags": ["a", "b"]}]})

    def test_same_size_same_mtime_edit_is_seen_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "db.yaml"
            path.write_text("name: Old\n", encoding="utf-8")
            previous_stat = path.stat()
            self.assertEqual(yaml_io.load_yaml(path), {"name": "Old"})
            path.write_text("name: New\n", encoding="utf-8")
            os.utime(path, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
            self.assertEqual(path.stat().st_size, previous_stat.st_size)
            self.assertEqual(yaml_io.load_yaml(path), {"name": "New"})
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                yaml_io.load_yaml(path)

    def test_atomic_replacement_and_repair_after_invalid_yaml_are_seen(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "db.yaml"
            replacement = Path(td) / "replacement.yaml"
            path.write_text("name: Old\n", encoding="utf-8")
            self.assertEqual(yaml_io.load_yaml(path), {"name": "Old"})
            replacement.write_text("invalid: [\n", encoding="utf-8")
            replacement.replace(path)
            with self.assertRaises(YAMLError):
                yaml_io.load_yaml(path)
            replacement.write_text("name: New\n", encoding="utf-8")
            replacement.replace(path)
            self.assertEqual(yaml_io.load_yaml(path), {"name": "New"})

    def test_parse_errors_are_never_cached(self) -> None:
        with patch.object(yaml_io, "_yaml_reader", wraps=yaml_io._yaml_reader) as reader:
            for _ in range(2):
                with self.assertRaises(YAMLError):
                    yaml_io.load_yaml_string("broken: [")
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(yaml_io._PARSED_YAML_CACHE.info()["entries"], 0)

    def test_cache_eviction_and_oversized_document_bypass(self) -> None:
        cache = YamlParseCache(max_entries=2, max_bytes=4096)
        counts = []

        def load(source):
            return cache.parse(source, lambda: counts.append(source) or {"value": source})

        load("a")
        load("b")
        load("a")
        load("c")
        load("b")
        self.assertEqual(counts, ["a", "b", "c", "b"])
        self.assertEqual(cache.info()["entries"], 2)
        load("x" * 5000)
        self.assertEqual(cache.info()["entries"], 2)
        self.assertLessEqual(cache.info()["estimated_bytes"], 4096)

    def test_estimated_memory_budget_evicts_before_entry_limit(self) -> None:
        cache = YamlParseCache(max_entries=20, max_bytes=1300)
        for number in range(10):
            source = f"{number}: " + "x" * 400
            cache.parse(source, lambda: {"value": source})
        info = cache.info()
        self.assertLess(info["entries"], 10)
        self.assertLessEqual(info["estimated_bytes"], 1300)

    def test_yaml_aliases_remain_shared_only_inside_each_result(self) -> None:
        source = "first: &items [a]\nsecond: *items\n"
        first = yaml_io.load_yaml_string(source)
        self.assertIs(first["first"], first["second"])
        first["first"].append("changed")
        second = yaml_io.load_yaml_string(source)
        self.assertEqual(second["first"], ["a"])
        self.assertIs(second["first"], second["second"])

    def test_parallel_callers_cannot_pollute_retained_document(self) -> None:
        source = "items: [original]\n"
        yaml_io.load_yaml_string(source)

        def mutate(number):
            value = yaml_io.load_yaml_string(source)
            value["items"].append(number)
            return value

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(mutate, range(20)))
        self.assertTrue(all(len(value["items"]) == 2 for value in results))
        self.assertEqual(yaml_io.load_yaml_string(source), {"items": ["original"]})

    def test_caller_stream_is_read_from_current_position_and_not_closed(self) -> None:
        stream = StringIO("ignored\nname: One\n")
        stream.readline()
        self.assertEqual(yaml_io.load_yaml(stream), {"name": "One"})
        self.assertFalse(stream.closed)
        self.assertEqual(yaml_io.load_yaml(stream), None)
        self.assertEqual(yaml_io._PARSED_YAML_CACHE.info()["entries"], 0)


if __name__ == "__main__":
    unittest.main()
