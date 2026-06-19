from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings
from work_catalog_yaml.jp_tv.collection_records import (
    collection_records_payload,
    save_collection_records_from_ui_body,
    scan_finish_years,
)
from work_catalog_yaml.yaml_io import load_yaml_string


def _settings(db: Path) -> JpTvBrowseSettings:
    return JpTvBrowseSettings(
        version=1,
        filesystem_root=db,
        resolved_default_readable=None,
        resolved_catalog_yaml_paths=(),
        enum_options={},
        enum_labels={},
        enum_section_labels={},
        app_features=(),
    )


class JpTvCollectionRecordsTest(unittest.TestCase):
    def test_scan_finish_years_uses_finish_directory_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("[198X]", "[1990]", "[197X]", "notes"):
                (root / name).mkdir()
            (root / "[2025]").write_text("not a directory", encoding="utf-8")

            years = scan_finish_years(root)

            self.assertEqual([it["key"] for it in years], ["197X", "198X", "1990"])
            self.assertEqual([it["label"] for it in years], ["[197X]", "[198X]", "[1990]"])

    def test_save_and_load_collection_records_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data_root = Path(td)
            db = data_root / "DB"
            finish = data_root / "Finish"
            db.mkdir()
            finish.mkdir()
            for name in ("[2025]", "[2024]"):
                (finish / name).mkdir()

            with patch.dict(os.environ, {"JP_TV_COLLECTION_FINISH_DIR": str(finish)}):
                result = save_collection_records_from_ui_body(
                    {
                        "records": [
                            {
                                "domain": "animation",
                                "country": "japan",
                                "release_type": "tv",
                                "completed_years": ["2025", "[2024]", "2023"],
                            },
                        ],
                    },
                    settings=_settings(db),
                )

                target = data_root / "CollectionInfo" / "collection-info.yaml"
                self.assertEqual(Path(result["path"]), target.resolve())
                raw = load_yaml_string(target.read_text(encoding="utf-8"))
                self.assertEqual(raw["records"][0]["completed_years"], ["2024", "2025"])

                payload = collection_records_payload(_settings(db))
                self.assertEqual(payload["records"][0]["completed_years"], ["2024", "2025"])


if __name__ == "__main__":
    unittest.main()
