"""Read-only timing of local API response builders and catalog loading.

No HTTP mutation route, media scan, cache publication or database write is run.
Timings are diagnostic, not machine-dependent pass/fail assertions.
"""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
from pathlib import Path
import pstats
import statistics
import sys
from time import perf_counter


sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "framework" / "backend"))

from work_catalog_yaml.layout import ensure_feature_backend_paths

ensure_feature_backend_paths()

from work_catalog_yaml.jp_tv import browse_api
from work_catalog_yaml.jp_tv.browse_settings import get_resolved_browse_settings
from work_catalog_yaml import yaml_io
from media_directory_organizer.catalog import MediaCatalog


def load_catalog():
    settings, _ = get_resolved_browse_settings()
    catalog = MediaCatalog.load(settings.filesystem_root, domain="", country="")
    return repr(catalog.works).encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--profile", choices=("config", "browse", "catalog"))
    args = parser.parse_args()
    if not 1 <= args.rounds <= 10:
        parser.error("rounds must be between 1 and 10")
    workloads = {
        "config": lambda: browse_api._get_config_api.__wrapped__({}).body,
        "browse": lambda: browse_api._get_browse_default_api.__wrapped__({}).body,
        "catalog": load_catalog,
    }
    results = {}
    for name, function in workloads.items():
        elapsed = []
        digests = []
        for _ in range(args.rounds):
            started = perf_counter()
            result = function()
            elapsed.append(round((perf_counter() - started) * 1000, 3))
            digests.append(hashlib.sha256(result).hexdigest())
        results[name] = {
            "milliseconds": elapsed,
            "median_ms": round(statistics.median(elapsed), 3),
            "bytes": len(result),
            "sha256": digests[-1],
            "stable": len(set(digests)) == 1,
        }
    results["yaml_cache"] = yaml_io._PARSED_YAML_CACHE.info()
    print(json.dumps(results, indent=2))
    if args.profile:
        profiler = cProfile.Profile()
        profiler.runcall(workloads[args.profile])
        pstats.Stats(profiler).strip_dirs().sort_stats("cumulative").print_stats(20)


if __name__ == "__main__":
    main()
