"""Compatibility wrapper for the collection-detail link index module."""
from __future__ import annotations

from work_catalog_yaml.layout import ensure_feature_backend_paths

ensure_feature_backend_paths()

from collection_detail.link_index import *  # noqa: F401,F403
