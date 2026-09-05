"""Stable identity shared by read-only planning and reviewed execution."""
from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_plan_id(payload: dict[str, Any]) -> str:
    stable = {
        "version": payload.get("version"),
        "root": payload["root"],
        "catalog_root": payload["catalog_root"],
        "classifier_fingerprint": payload.get("classifier_fingerprint"),
        "family_works": payload.get("family_works") or [],
        "assignments": payload["assignments"],
        "moves": payload["moves"],
        "unresolved_files": payload.get("unresolved_files") or [],
        "source_work_bindings": payload.get("source_work_bindings") or [],
        "issues": payload["issues"],
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = ["stable_plan_id"]
