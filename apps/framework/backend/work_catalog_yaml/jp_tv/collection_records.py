"""Compatibility wrapper for the collection-info backend service module."""
from __future__ import annotations

from work_catalog_yaml.layout import ensure_feature_backend_paths

ensure_feature_backend_paths()

from collection_info.service import *  # noqa: F401,F403
