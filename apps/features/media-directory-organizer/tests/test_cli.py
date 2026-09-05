from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from media_directory_organizer.cli import main


class OrganizerCliTest(unittest.TestCase):
    def test_preview_loads_korean_tv_records_without_moving_media(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "Korean Drama"
            source = root / "[VCB] Korean Drama [BDRip]" / "Korean Drama 01.mkv"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"original korean episode")
            catalog_root = base / "catalog"
            catalog_root.mkdir()
            catalog_file = catalog_root / "[KR][TVInfo][2025].yaml"
            quoted_root = str(root).replace("'", "''")
            catalog_file.write_text(
                "- attributes:\n"
                "  - type: name\n"
                "    data: Korean Drama\n"
                "  - type: country\n"
                "    data: korea\n"
                "  - type: date\n"
                "    data: {start: '20250101', end: '20250331'}\n"
                "  - type: collection-type\n"
                "    data:\n"
                "      domain: tv-drama\n"
                "      release_type: tv\n"
                f"      path: '{quoted_root}'\n"
                "      collectioned:\n"
                "      - press_format: BDRip\n"
                "        press_group: VCB\n"
                "        press_path: Korean Drama_BDRip\n",
                encoding="utf-8",
            )
            config = base / "organizer.yaml"
            quoted_base = str(base).replace("'", "''")
            config.write_text(
                f"paths:\n  allowed_resource_roots: ['{quoted_base}']\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main([
                    "preview", "--root", str(root), "--config", str(config),
                    "--catalog-root", str(catalog_root), "--json",
                ])
            plan = json.loads(output.getvalue())

            self.assertEqual(exit_code, 0, plan)
            self.assertTrue(plan["ready"], plan["issues"])
            self.assertEqual(plan["family_works"], ["Korean Drama"])
            self.assertEqual(plan["assignments"][0]["country"], "korea")
            self.assertEqual(plan["assignments"][0]["domain"], "tv-drama")
            self.assertEqual(len(plan["moves"]), 1)
            self.assertEqual(source.read_bytes(), b"original korean episode")
            self.assertFalse(Path(plan["moves"][0]["target"]).exists())


if __name__ == "__main__":
    unittest.main()
