from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from work_catalog_yaml.jp_tv import browse_api
from media_directory_organizer.service import MediaRollbackError


class MediaRecoveryApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_media_apply_routes_expose_partial_recovery_details(self) -> None:
        cases = (
            (browse_api._post_media_directory_organizer_apply_api, "apply_organizer_from_ui_body"),
            (browse_api._post_media_directory_organizer_landing_apply_api, "apply_organizer_landing_from_ui_body"),
            (browse_api._post_media_directory_organizer_catalog_shortcut_repair_apply_api, "apply_organizer_catalog_shortcut_repair_from_ui_body"),
        )
        for endpoint, service_name in cases:
            with self.subTest(endpoint=endpoint.__name__):
                error = MediaRollbackError(
                    "移动失败且部分文件未恢复",
                    plan_id="reviewed-plan",
                    root="work",
                    recovery_moves=[{"source": "work/source/a.mkv", "target": "work/target/a.mkv", "error": "restore denied"}],
                    moved_file_count=2,
                    rolled_back_file_count=1,
                )
                error.catalog_recovery = {
                    "state": "preserved",
                    "rolled_back": False,
                    "recovery_files": [{"target": "db/work.yaml", "history_path": "history/work.yaml"}],
                }
                with patch.object(browse_api, service_name, side_effect=error):
                    response = endpoint.__wrapped__({})
                payload = json.loads(response.body)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(payload["state"], "partial")
                self.assertFalse(payload["ok"])
                self.assertFalse(payload["media"]["rollback_complete"])
                self.assertEqual(payload["media"]["recovery_moves"][0]["target"], "work/target/a.mkv")
                self.assertFalse(payload["catalog"]["rolled_back"])
                self.assertEqual(payload["catalog"]["recovery_files"][0]["history_path"], "history/work.yaml")
                self.assertTrue(payload["retry_requires_preview"])


if __name__ == "__main__":
    unittest.main()
