"""Compatibility wrapper for the collection-detail backend save module."""
from __future__ import annotations

from work_catalog_yaml.layout import ensure_feature_backend_paths

ensure_feature_backend_paths()

from collection_detail.save import *  # noqa: F401,F403
