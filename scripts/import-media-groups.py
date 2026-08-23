"""Regenerate the shared media group database from Note.h and the catalog."""
from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "apps" / "framework" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from work_catalog_yaml.media_groups import save_media_group_registry  # noqa: E402


def main() -> int:
    path, payload = save_media_group_registry()
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(path),
                "statistics": payload["statistics"],
                "corrections": payload["rules"]["member_corrections"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
