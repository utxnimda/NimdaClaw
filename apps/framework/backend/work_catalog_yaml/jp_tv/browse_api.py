"""Legacy import bridge; collection business/API code lives with its feature.

Keep module identity so external callers and test patches address the same API.
The application composition imports ``collection_detail.web`` directly.
"""
from __future__ import annotations

import sys
from work_catalog_yaml.layout import ensure_feature_backend_paths

ensure_feature_backend_paths()
from collection_detail import web as _implementation
from collection_info import web as _collection_info

# Only the legacy aggregate API exposes both features. Production composition
# imports each feature directly, without a peer dependency in collection-detail.
_implementation._get_collection_records_api = _collection_info._get_collection_records_api
_implementation._post_collection_records_api = _collection_info._post_collection_records_api

sys.modules[__name__] = _implementation
