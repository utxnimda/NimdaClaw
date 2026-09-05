from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from collection_detail.save import CatalogMutationReceipt
from media_directory_organizer import landing
from media_directory_organizer.service import MediaRollbackError
from media_directory_organizer.settings import OrganizerSettings
from work_catalog_yaml.jp_tv.browse_settings import JpTvBrowseSettings


class LandingPartialMediaRecoveryTest(unittest.TestCase):
    def test_all_batch_and_repair_flows_preserve_only_on_partial_media_failure(self) -> None:
        for flow in ("multi", "shared", "mixed", "repair"):
            for partial in (False, True):
                with self.subTest(flow=flow, partial=partial), tempfile.TemporaryDirectory() as temp:
                    base = Path(temp)
                    reviewed = "1234567890abcdef"
                    receipt = CatalogMutationReceipt(
                        target=base / "catalog.yaml", target_existed=True,
                        previous_bytes=b"[]", history_path=base / "history.yaml",
                        work_ref={}, written_sha256="written-hash",
                    )
                    organizer = OrganizerSettings(
                        catalog_root=base, allowed_resource_roots=(base,),
                        format_markers={}, group_markers={}, group_suffixes={},
                    )
                    browse = JpTvBrowseSettings(
                        version=1, filesystem_root=base, resolved_default_readable=None,
                        resolved_catalog_yaml_paths=(), enum_options={}, enum_labels={},
                        enum_section_labels={}, app_features=(),
                    )
                    current = {
                        "root": str(base), "draft_works": [],
                        "shared_target_bindings": [], "source_work_bindings": {},
                        "organizer_plan": {"plan_id": reviewed},
                        "repair_plan_id": reviewed, "ready": True,
                    }
                    error = MediaRollbackError("partial") if partial else OSError("fully rolled back")
                    changes = [{"target": str(receipt.target), "action": "update"}]
                    with ExitStack() as stack:
                        for name in (
                            "_apply_catalog_work_append_batch", "_apply_catalog_shared_update_batch",
                            "_apply_mixed_catalog_changes",
                        ):
                            stack.enter_context(patch.object(landing, name, return_value=(changes, [receipt])))
                        stack.enter_context(patch.object(landing, "_catalog_work_refs_from_changes", return_value=[]))
                        stack.enter_context(patch.object(landing, "_normalize_shared_target_bindings", return_value=([], [], [], {}, [], {})))
                        stack.enter_context(patch.object(landing, "_normalize_mixed_source_bindings", return_value={"existing_patches": [], "draft_patches": []}))
                        stack.enter_context(patch.object(landing, "_preview_mixed_catalog_changes", return_value=[]))
                        stack.enter_context(patch.object(landing, "preview_catalog_shortcut_repair", return_value=current))
                        stack.enter_context(patch.object(landing, "_apply_catalog_repair_change", return_value=(changes, receipt)))
                        stack.enter_context(patch.object(landing, "apply_plan", side_effect=error))
                        rollback_batch = stack.enter_context(patch.object(landing, "_rollback_catalog_receipts"))
                        rollback_single = stack.enter_context(patch.object(landing, "rollback_catalog_yaml_mutation"))
                        shortcuts = stack.enter_context(patch.object(landing, "apply_scoped_shortcuts_for_work"))
                        with self.assertRaises(type(error)) as raised:
                            if flow == "multi":
                                landing._apply_multi_work_landing(current, reviewed=reviewed, browse_settings=browse)
                            elif flow == "shared":
                                landing._apply_shared_target_landing(current, reviewed=reviewed, organizer_settings=organizer, browse_settings=browse)
                            elif flow == "mixed":
                                landing._apply_mixed_source_landing(current, reviewed=reviewed, organizer_settings=organizer, browse_settings=browse)
                            else:
                                landing.apply_catalog_shortcut_repair(
                                    {
                                        "repair_plan_id": reviewed, "confirmation": reviewed,
                                        "acknowledge_catalog_write": True,
                                        "acknowledge_shortcuts": True, "acknowledge_move": True,
                                    },
                                    organizer_settings=organizer, browse_settings=browse,
                                    organizer_plan=current["organizer_plan"],
                                )
                    shortcuts.assert_not_called()
                    if partial:
                        rollback_batch.assert_not_called()
                        rollback_single.assert_not_called()
                        payload = raised.exception.to_payload()
                        self.assertEqual(payload["catalog"]["state"], "preserved")
                        self.assertEqual(payload["catalog"]["changes"], changes)
                        self.assertEqual(payload["catalog"]["recovery_files"][0]["history_path"], str(receipt.history_path))
                    elif flow == "repair":
                        rollback_single.assert_called_once_with(receipt)
                    else:
                        rollback_batch.assert_called_once_with([receipt])


if __name__ == "__main__":
    unittest.main()
