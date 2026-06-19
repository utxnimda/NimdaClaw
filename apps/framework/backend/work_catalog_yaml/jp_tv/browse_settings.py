"""JP TV 浏览页的本地服务端配置（默认 YAML、根目录约束）。"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from work_catalog_yaml.layout import (
    feature_config_path,
    framework_config_path,
    workspace_catalog_data_root,
    workspace_config_root,
)
from work_catalog_yaml.yaml_io import load_yaml

try:
    from ruamel.yaml.error import YAMLError
except ImportError:  # pragma: no cover
    YAMLError = Exception

_ENV_CONFIG = "JP_TV_BROWSE_CONFIG_PATH"
_CWD_CONFIG_NAME = "jp-tv-browse.config.yaml"


def _configs_under_catalog_data_root(data_root: Path) -> tuple[Path, ...]:
    """Legacy compatibility: ``work-catalog-data/Config/jp-tv-browse.config.yaml``."""
    return (data_root / "Config" / _CWD_CONFIG_NAME,)


def _configs_under_app_config_root(config_root: Path) -> tuple[Path, ...]:
    """Legacy monolithic app config location."""
    return (config_root / "catalog-browser" / _CWD_CONFIG_NAME,)


def _collection_detail_configs_under_config_root(config_root: Path) -> tuple[Path, ...]:
    return (config_root / "features" / "collection-detail" / "config.yaml",)


def _app_config_roots_under(anchor: Path) -> tuple[Path, ...]:
    return (anchor / "config",)


def _catalog_data_roots_under(anchor: Path) -> tuple[Path, ...]:
    return (
        anchor / "data" / "work-catalog",
        anchor / "work-catalog-data",
    )


_EMPTY_ENUM: Mapping[str, tuple[str, ...]] = MappingProxyType({})
_EMPTY_ENUM_LABELS: Mapping[str, Mapping[str, str]] = MappingProxyType({})
_EMPTY_ENUM_SECTION_LABELS: Mapping[str, str] = MappingProxyType({})

_DEFAULT_APP_FEATURES: tuple[Mapping[str, Any], ...] = (
    MappingProxyType({"id": "collection-detail", "label": "作品数据", "order": 10}),
    MappingProxyType({"id": "collection-info", "label": "收集情况", "order": 20}),
)


@dataclass(frozen=True)
class JpTvBrowseSettings:
    version: int
    filesystem_root: Path | None
    resolved_default_readable: str | None  # 首条可用路径；多文件时为字典序第一个
    # ``filesystem_root`` 下 ``*.yaml`` 扫描得到的绝对路径（已排序）
    resolved_catalog_yaml_paths: tuple[str, ...]
    # browse 下拉：jp-tv-browse.config.yaml ``enum[].name`` → 允许的取值序列
    enum_options: Mapping[str, tuple[str, ...]]
    # browse 展示：同上 name → YAML 取值 → 可读文案（仅有 desc/description 时出现）
    enum_labels: Mapping[str, Mapping[str, str]]
    # ``enum[].`` 顶层 desc/description/label/title → 表格列头等界面字段名
    enum_section_labels: Mapping[str, str]
    # app 级 UI 配置：功能页签的显示名、顺序等
    app_features: tuple[Mapping[str, Any], ...]

    def default_load_enabled(self) -> bool:
        return self.filesystem_root is not None and bool(self.resolved_catalog_yaml_paths)


def _default_yaml_path_candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    return [here / "browse_config.default.yaml"]


def browse_config_candidates_hmsg() -> str:
    cwd = Path.cwd().resolve()
    return (
        "配置查找顺序（先命中即用）：环境变量 "
        f"{_ENV_CONFIG}"
        " → "
        f"当前目录 {cwd / _CWD_CONFIG_NAME}"
        " → "
        f"{cwd / 'config' / 'features' / 'collection-detail' / 'config.yaml'}"
        " → "
        "自当前目录逐级向上查找 `<某层>/config/features/collection-detail/config.yaml` → "
        "兼容旧单文件配置 "
        f"{cwd / 'config' / 'catalog-browser' / _CWD_CONFIG_NAME}"
        " → "
        f"{cwd / 'work-catalog-data' / 'Config' / _CWD_CONFIG_NAME}"
        " → "
        "自当前目录逐级向上查找 `<某层>/work-catalog-data/Config/jp-tv-browse.config.yaml` / "
        "`<某层>/data/work-catalog/Config/jp-tv-browse.config.yaml` → "
        "包内 browse_config.default.yaml（无工程配置时的兜底）。"
        " 必需项只有 `paths.filesystem_root`（数据 DB 目录）；将加载该目录下全部 `*.yaml`。"
        " 表格枚举等可由各数据 YAML 顶层 `browse` 与配置文件中的 `enum`（若有）合并提供。"
        " 保存前备份写在 `data/features/collection-detail/history/`。"
    )


def _find_config_in_parent_trees(*, max_up: int = 16) -> Path | None:
    """从 cwd 起向父目录回溯查找 workspace/app 配置，旧数据目录配置仅作兼容。"""
    here = Path.cwd().resolve()
    for _ in range(max_up + 1):
        for config_root in _app_config_roots_under(here):
            if not config_root.is_dir():
                continue
            for cand in _collection_detail_configs_under_config_root(config_root):
                if cand.is_file():
                    return cand.resolve()
            for cand in _configs_under_app_config_root(config_root):
                if cand.is_file():
                    return cand.resolve()
        for data_root in _catalog_data_roots_under(here):
            if not data_root.is_dir():
                continue
            for cand in _configs_under_catalog_data_root(data_root):
                if cand.is_file():
                    return cand.resolve()
        parent = here.parent
        if parent == here:
            break
        here = parent
    return None


def browse_config_resolve_path(cli_path: str | Path | None) -> Path | None:
    if cli_path is not None:
        return Path(cli_path).resolve()
    ev = os.environ.get(_ENV_CONFIG)
    if ev:
        return Path(ev.strip()).resolve()
    cwd_p = Path.cwd() / _CWD_CONFIG_NAME
    if cwd_p.is_file():
        return cwd_p.resolve()
    modern_feature_cfg = feature_config_path("collection-detail")
    if modern_feature_cfg.is_file():
        return modern_feature_cfg.resolve()
    for cwd_config_root in (*_app_config_roots_under(Path.cwd()), workspace_config_root()):
        if not cwd_config_root.is_dir():
            continue
        for cand in _collection_detail_configs_under_config_root(cwd_config_root):
            if cand.is_file():
                return cand.resolve()
        for cand in _configs_under_app_config_root(cwd_config_root):
            if cand.is_file():
                return cand.resolve()
    for cwd_data_root in (*_catalog_data_roots_under(Path.cwd()), workspace_catalog_data_root()):
        if not cwd_data_root.is_dir():
            continue
        for cand in _configs_under_catalog_data_root(cwd_data_root):
            if cand.is_file():
                return cand.resolve()
    walk = _find_config_in_parent_trees()
    if walk is not None:
        return walk
    for c in _default_yaml_path_candidates():
        if c.is_file():
            return c.resolve()
    return None


def _str_or_blank(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _filesystem_root_from_raw(raw: Any) -> Path | None:
    if not isinstance(raw, dict):
        return None
    paths = raw.get("paths")
    if not isinstance(paths, dict):
        paths = raw
    root_s = _str_or_blank(paths.get("filesystem_root"))
    if not root_s:
        return None
    return Path(root_s).expanduser()


def _default_app_features_by_id() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in _DEFAULT_APP_FEATURES:
        fid = _str_or_blank(item.get("id"))
        if fid:
            out[fid] = dict(item)
    return out


def _coerce_app_feature_order(raw: Any, fallback: int) -> int:
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return int(raw.strip())
        except ValueError:
            return fallback
    return fallback


def _app_feature_from_raw(
    feature_id: str,
    raw: Any,
    fallback: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    fid = _str_or_blank(feature_id)
    if not fid:
        return None
    base = dict(fallback or {})
    if not base:
        base = {"id": fid, "label": fid, "order": 1000}
    base["id"] = fid
    if isinstance(raw, str):
        lab = raw.strip()
        if lab:
            base["label"] = lab
    elif isinstance(raw, Mapping):
        lab = _scalar_display_from_mapping(raw) or _str_or_blank(raw.get("name"))
        if lab:
            base["label"] = lab
        base["order"] = _coerce_app_feature_order(raw.get("order"), int(base.get("order", 1000)))
    base["label"] = _str_or_blank(base.get("label")) or fid
    base["order"] = _coerce_app_feature_order(base.get("order"), 1000)
    return {
        "id": str(base["id"]),
        "label": str(base["label"]),
        "order": int(base["order"]),
    }


def _app_features_from_raw(raw: dict[str, Any]) -> tuple[Mapping[str, Any], ...]:
    defaults = _default_app_features_by_id()
    features_by_id = {fid: dict(item) for fid, item in defaults.items()}
    app_raw = raw.get("app")
    features_raw: Any = None
    if isinstance(app_raw, Mapping):
        features_raw = app_raw.get("features")

    if isinstance(features_raw, list):
        seen_order: list[str] = []
        for item in features_raw:
            if not isinstance(item, Mapping):
                continue
            fid = _str_or_blank(item.get("id"))
            if not fid:
                continue
            parsed = _app_feature_from_raw(fid, item, features_by_id.get(fid))
            if parsed is None:
                continue
            if fid not in seen_order:
                seen_order.append(fid)
            features_by_id[fid] = parsed
        ordered_ids = seen_order + [fid for fid in defaults.keys() if fid not in seen_order]
    elif isinstance(features_raw, Mapping):
        for fid_raw, item in features_raw.items():
            fid = _str_or_blank(fid_raw)
            if not fid:
                continue
            parsed = _app_feature_from_raw(fid, item, features_by_id.get(fid))
            if parsed is not None:
                features_by_id[fid] = parsed
        ordered_ids = list(features_by_id.keys())
    else:
        ordered_ids = list(features_by_id.keys())

    out = [features_by_id[fid] for fid in ordered_ids if fid in features_by_id]
    out.sort(key=lambda it: (int(it.get("order", 1000)), str(it.get("id", ""))))
    return tuple(MappingProxyType(item) for item in out)


def _load_framework_app_config_raw() -> dict[str, Any]:
    p = framework_config_path()
    if not p.is_file():
        return {}
    try:
        raw = load_yaml(p)
    except YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _app_features_for_settings(feature_raw: dict[str, Any]) -> tuple[Mapping[str, Any], ...]:
    app_raw = _load_framework_app_config_raw()
    if app_raw:
        return _app_features_from_raw(app_raw)
    return _app_features_from_raw(feature_raw)


def _scalar_display_from_mapping(item: Mapping[str, Any]) -> str | None:
    for key in ("desc", "description", "label", "title"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _one_enum_values_entry(item: Any) -> tuple[str | None, str | None]:
    """返回值 ``(YAML 取值, 展示文案)；无展示覆盖时后者为 ``None``。"""
    if isinstance(item, str):
        s = item.strip()
        return (s if s else None, None)
    if isinstance(item, bool):
        return (None, None)
    if isinstance(item, (int, float)):
        sv = str(item).strip()
        return (sv if sv else None, None)
    if isinstance(item, dict):
        d = item
        v_raw = d.get("value") or d.get("code")
        val: str | None = None
        if isinstance(v_raw, str) and v_raw.strip():
            val = v_raw.strip()
        if val:
            dsc = _scalar_display_from_mapping(d)
            return (val, dsc if dsc else None)
        keys = tuple(d.keys())
        if len(keys) == 1:
            key = keys[0]
            if isinstance(key, str):
                nk = key.strip()
                if nk:
                    sub = d.get(key)
                    if isinstance(sub, str) and sub.strip():
                        return (nk, sub.strip())
                    return (nk, None)
        return (None, None)
    return (None, None)


def _extract_enum_values_and_labels(vals: Any) -> tuple[tuple[str, ...], Mapping[str, str]]:
    """解析 ``enum[] .values``；第二项仅在存在 ``desc``/``description`` 等时含对应键。"""
    if not isinstance(vals, list):
        return (), MappingProxyType({})
    ordered: list[str] = []
    labels_raw: dict[str, str] = {}
    seen: set[str] = set()
    for item in vals:
        val_opt, dsc_opt = _one_enum_values_entry(item)
        if not val_opt or val_opt in seen:
            continue
        seen.add(val_opt)
        ordered.append(val_opt)
        if dsc_opt:
            labels_raw.setdefault(val_opt, dsc_opt)
    lbl = MappingProxyType(labels_raw)
    return (tuple(ordered), lbl)


def _enum_section_labels_from_raw(raw: dict[str, Any]) -> dict[str, str]:
    """``enum[].name`` → 该块顶层展示名（取自 ``desc`` / ``description`` / ``label`` / ``title``）。"""
    enums = raw.get("enum")
    if not isinstance(enums, list):
        return {}
    out: dict[str, str] = {}
    for blk in enums:
        if not isinstance(blk, dict):
            continue
        name = blk.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        lab = _scalar_display_from_mapping(blk)
        if lab:
            out[name.strip()] = lab
    return out


def _enum_mappings_from_raw(raw: dict[str, Any]) -> tuple[dict[str, tuple[str, ...]], dict[str, Mapping[str, str]]]:
    enums = raw.get("enum")
    if not isinstance(enums, list):
        return {}, {}
    options_out: dict[str, tuple[str, ...]] = {}
    labels_nested: dict[str, dict[str, str]] = {}
    for blk in enums:
        if not isinstance(blk, dict):
            continue
        name = blk.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        nkey = name.strip()
        vals_tup, lbl_map = _extract_enum_values_and_labels(blk.get("values"))
        if vals_tup:
            options_out[nkey] = vals_tup
        if lbl_map:
            # lbl_map already MappingProxy; convert to plain dict merge
            for vk, dk in lbl_map.items():
                labels_nested.setdefault(nkey, {})[vk] = dk
    labels_frozen: dict[str, Mapping[str, str]] = {
        k: MappingProxyType(v) for k, v in sorted(labels_nested.items())
    }
    return options_out, labels_frozen


def jp_tv_browse_enum_options_json(settings: JpTvBrowseSettings) -> dict[str, list[str]]:
    """``/api/config`` JSON：name → values。"""
    return _enum_options_to_json(settings.enum_options)


def _enum_options_to_json(eo: Mapping[str, tuple[str, ...]]) -> dict[str, list[str]]:
    return {k: list(v) for k, v in sorted(eo.items())}


def jp_tv_browse_enum_labels_json(settings: JpTvBrowseSettings) -> dict[str, dict[str, str]]:
    """``/api/config`` JSON：name → {取值: 展示文案}。"""
    return _enum_labels_to_json(settings.enum_labels)


def _enum_labels_to_json(el: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for nk in sorted(el.keys()):
        inner = el[nk]
        out[nk] = {vk: dk for vk, dk in sorted(inner.items())}
    return out


def jp_tv_browse_enum_section_labels_json(settings: JpTvBrowseSettings) -> dict[str, str]:
    """``/api/config`` JSON：``enum[].name`` → 该枚举块顶层 desc（表格列头等）。"""
    return dict(sorted(settings.enum_section_labels.items()))


def jp_tv_browse_app_config_json(settings: JpTvBrowseSettings) -> dict[str, Any]:
    """``/api/config`` JSON：整体 app 配置。"""
    return {
        "features": [
            {
                "id": str(item.get("id", "")),
                "label": str(item.get("label", "")),
                "order": int(item.get("order", 1000)),
            }
            for item in settings.app_features
            if _str_or_blank(item.get("id"))
        ],
    }


def resolve_safe_yaml_under_root(root: Path, relpath: str) -> Path:
    """将 ``relpath`` 解析为 ``root`` 下的绝对路径，禁止跳出根目录。"""
    root_r = root.resolve()
    rel = relpath.replace("\\", "/").strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise ValueError("数据相对路径非法（不允许空、.. 或跳出根目录）")
    cand = (root_r / rel).resolve()
    cand.relative_to(root_r)
    return cand


def jp_tv_yaml_catalog_relpath(settings: JpTvBrowseSettings, yaml_abs: Path) -> str:
    """数据文件相对于 ``filesystem_root`` 的 posix 相对路径（供保存/API 对齐）。"""
    if settings.filesystem_root is None:
        raise ValueError("缺少 filesystem_root")
    return yaml_abs.resolve().relative_to(settings.filesystem_root.resolve()).as_posix()


def _merge_enum_option_sequences(
    previous: tuple[str, ...],
    addon: tuple[str, ...],
) -> tuple[str, ...]:
    """先后拼接：先前顺序保留；``addon`` 中未出现过的值追加到末尾。"""
    seen = set(previous)
    out = list(previous)
    for x in addon:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return tuple(out)


def merged_jp_tv_browse_enum_maps(
    settings: JpTvBrowseSettings,
) -> tuple[Mapping[str, tuple[str, ...]], Mapping[str, Mapping[str, str]], Mapping[str, str]]:
    """合并 Config 内置 ``enum`` 与各数据 YAML 顶层 ``browse`` 块中的 ``enum``。

    顺序：以 Config 为基底；再按 ``resolved_catalog_yaml_paths`` 字典序逐个叠
    加。选项列表做并集（文件内顺序接在已有列表后）；``values`` 的展示文案与
    枚举块列头：后出现的文件覆盖同名键。
    """
    eo_mutable: dict[str, list[str]] = {k: list(v) for k, v in settings.enum_options.items()}
    el_mutable: dict[str, dict[str, str]] = {
        nk: dict(inner) for nk, inner in settings.enum_labels.items()
    }
    esl = dict(settings.enum_section_labels)

    for abs_ps in sorted(settings.resolved_catalog_yaml_paths):
        p = Path(abs_ps)
        if not p.is_file():
            continue
        try:
            raw_any = load_yaml(p)
        except YAMLError:
            continue
        br: dict[str, Any] | None = None
        if isinstance(raw_any, dict):
            bx = raw_any.get("browse")
            if isinstance(bx, dict):
                br = bx
        if not br:
            continue
        opt_add, lbl_add = _enum_mappings_from_raw(br)
        sec_add = _enum_section_labels_from_raw(br)
        for nk, tup in opt_add.items():
            eo_mutable[nk] = list(
                _merge_enum_option_sequences(tuple(eo_mutable.get(nk, ())), tup),
            )
        for nk, sub in lbl_add.items():
            tgt = el_mutable.setdefault(nk, {})
            for vk, dk in sub.items():
                tgt[vk] = dk
        for nk, sl in sec_add.items():
            esl[nk] = sl

    eo_f = MappingProxyType({k: tuple(v) for k, v in sorted(eo_mutable.items())})
    el_f = MappingProxyType(
        {k: MappingProxyType(dict(sorted(v.items()))) for k, v in sorted(el_mutable.items())},
    )
    esl_f = MappingProxyType(dict(sorted(esl.items())))
    return eo_f, el_f, esl_f


def jp_tv_browse_merged_enum_bundle_for_api(settings: JpTvBrowseSettings) -> tuple[
    dict[str, list[str]],
    dict[str, dict[str, str]],
    dict[str, str],
]:
    """Config + 数据 ``browse.enum`` 合并后，转换为 ``/api/config`` 可用的三项 JSON。"""
    eo_m, el_m, esl_m = merged_jp_tv_browse_enum_maps(settings)
    return (
        _enum_options_to_json(eo_m),
        _enum_labels_to_json(el_m),
        dict(sorted(esl_m.items())),
    )


def load_jp_tv_browse_settings(path: Path | None = None) -> JpTvBrowseSettings:
    p = path or browse_config_resolve_path(None)
    if p is None or not p.is_file():
        return JpTvBrowseSettings(
            version=1,
            filesystem_root=None,
            resolved_default_readable=None,
            resolved_catalog_yaml_paths=(),
            enum_options=_EMPTY_ENUM,
            enum_labels=_EMPTY_ENUM_LABELS,
            enum_section_labels=_EMPTY_ENUM_SECTION_LABELS,
            app_features=_DEFAULT_APP_FEATURES,
        )
    try:
        raw = load_yaml(p)
    except YAMLError as e:
        hint = getattr(e, "problem", None) or str(e)
        where = getattr(getattr(e, "problem_mark", None), "line", None)
        line_msg = f"，约第 {where} 行" if where else ""
        raise ValueError(
            f"浏览配置 YAML 无法解析（{p}{line_msg}）：{hint}",
        ) from e
    if not isinstance(raw, dict):
        raise ValueError(f"浏览配置须为 YAML 对象：{p}")
    ver = raw.get("version")
    iv = int(ver) if isinstance(ver, int) else 1

    root = _filesystem_root_from_raw(raw)
    eo_pairs, el_pairs = _enum_mappings_from_raw(raw)
    sec_lbl = MappingProxyType(_enum_section_labels_from_raw(raw))
    enum_proxy: Mapping[str, tuple[str, ...]] = MappingProxyType(eo_pairs)
    lbl_proxy = MappingProxyType(el_pairs)
    app_features = _app_features_for_settings(raw)

    resolved_paths_list: list[str] = []
    resolved_read: str | None = None
    if root is not None:
        root_rr = root.resolve()
        if root_rr.is_dir():
            for cand in sorted(root_rr.glob("*.yaml")):
                if cand.is_file() and not cand.name.startswith("."):
                    resolved_paths_list.append(str(cand.resolve()))
            if resolved_paths_list:
                resolved_read = resolved_paths_list[0]

    return JpTvBrowseSettings(
        version=iv,
        filesystem_root=root,
        resolved_default_readable=resolved_read,
        resolved_catalog_yaml_paths=tuple(resolved_paths_list),
        enum_options=enum_proxy,
        enum_labels=lbl_proxy,
        enum_section_labels=sec_lbl,
        app_features=app_features,
    )


def get_resolved_browse_settings() -> tuple[JpTvBrowseSettings, Path | None]:
    """每次解析配置文件路径并从磁盘载入（避免改了 YAML 却长期命中旧缓存）。"""
    p = browse_config_resolve_path(None)
    return load_jp_tv_browse_settings(p), p


def reset_browse_settings_cache() -> None:
    """兼容旧调用；读取逻辑已不设进程内缓存，此函数无副作用。"""
    pass
