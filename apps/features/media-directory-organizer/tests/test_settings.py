from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from media_directory_organizer.settings import load_organizer_settings


class OrganizerSettingsTest(unittest.TestCase):
    def test_relative_paths_share_workspace_base_and_expand_home_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            config = base / "organizer.yaml"
            config.write_text(
                "paths:\n"
                "  catalog_root: ~/catalog\n"
                "  allowed_resource_roots: [media, '~/videos', null, 10]\n"
                "  default_work_root: media/Example\n",
                encoding="utf-8",
            )
            with patch("work_catalog_yaml.layout.workspace_root", return_value=base):
                settings = load_organizer_settings(config)

            self.assertEqual(settings.catalog_root, (Path.home() / "catalog").resolve())
            self.assertEqual(settings.allowed_resource_roots, (
                (base / "media").resolve(), (Path.home() / "videos").resolve(),
            ))
            self.assertEqual(settings.default_work_root, (base / "media" / "Example").resolve())


if __name__ == "__main__":
    unittest.main()
