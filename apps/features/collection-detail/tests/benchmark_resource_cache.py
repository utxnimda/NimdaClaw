"""Synthetic resource-cache benchmark; all files live in TemporaryDirectory."""
from __future__ import annotations

import json
import statistics
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from collection_detail import link_index
from work_catalog_yaml.yaml_io import dump_yaml_string


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        media = root / "media"
        for series in range(40):
            for release in range(2):
                press = media / f"Series {series:03}" / f"Work {series:03}_{release}_BDRip"
                press.mkdir(parents=True)
                for episode in range(4):
                    (press / f"episode-{episode:02}.mkv").write_bytes(b"fixture")
        config = root / "config.yaml"
        config.write_text(dump_yaml_string({"paths": {"resource_roots": [str(media)], "shortcut_root": str(root / "shortcuts")}}), encoding="utf-8")
        with patch.object(link_index, "feature_data_root", return_value=root / "data"), patch.object(link_index, "feature_config_path", return_value=config):
            link_index._invalidate_feature_config_cache()
            start = perf_counter()
            snapshot = link_index.scan_resource_libraries_payload()
            scan_seconds = perf_counter() - start
            original_stat = Path.stat
            with patch.object(Path, "stat", autospec=True, side_effect=original_stat) as stats, patch.object(link_index.os, "scandir", wraps=link_index.os.scandir) as enumerations:
                start = perf_counter()
                link_index._resource_dir_node(media, media, "root:0", [], str(root / "shortcuts"), {"dirs": 0, "files": 0, "truncated": False}, 50000, max_depth=1)
                instrumented_scan_seconds = perf_counter() - start
                scan_counts = {"scan_path_stat_calls": stats.call_count, "scan_directory_enumerations": enumerations.call_count, "instrumented_scan_seconds": round(instrumented_scan_seconds, 4)}
            relpath = "root:0/Series 000/Work 000_0_BDRip"
            link_index.resource_libraries_node_payload(relpath)
            timings = []
            for _ in range(12):
                start = perf_counter()
                link_index.resource_libraries_node_payload(relpath)
                timings.append(perf_counter() - start)
            with patch.object(link_index, "load_yaml", wraps=link_index.load_yaml) as loads, patch.object(link_index, "collection_link_index_config_json", wraps=link_index.collection_link_index_config_json) as configs, patch.object(link_index, "_feature_config", wraps=link_index._feature_config) as feature_config:
                for _ in range(5):
                    link_index.resource_libraries_node_payload(relpath)
                counts = {"yaml_loads_for_5_expands": loads.call_count, "config_projections_for_5_expands": configs.call_count, "config_reads_for_5_expands": feature_config.call_count}
            print(json.dumps({"series": 40, "press_directories": 80, "files": 320, "manifest_bytes": Path(snapshot["cache_path"]).stat().st_size, "scan_seconds": round(scan_seconds, 4), "warm_expand_median_ms": round(statistics.median(timings) * 1000, 3), "warm_expand_total_ms": round(sum(timings) * 1000, 3), **scan_counts, **counts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
