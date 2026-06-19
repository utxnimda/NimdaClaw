"""从浏览页写回 JP TV 数据 YAML，并在 ``History`` 目录保留带时间戳的备份。"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ruamel.yaml import YAML

from work_catalog_yaml.jp_tv.browse_settings import (
    JpTvBrowseSettings,
    resolve_safe_yaml_under_root,
)
from work_catalog_yaml.jp_tv.validate import (
    TV_JP_DOMAIN_KEY,
    TV_JP_PRESS_FORMAT_KEY,
    TV_JP_PRESS_GROUP_KEY,
    TV_JP_PRESS_PATH_KEY,
    TV_JP_RELEASE_TYPE_KEY,
    load_jp_tv_entries_from_yaml,
)
from work_catalog_yaml.layout import feature_data_root
from work_catalog_yaml.yaml_io import dump_yaml_string, load_yaml_string


def _assert_save_target_allowed(target: Path, settings: JpTvBrowseSettings) -> None:
    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root，禁止写盘")
    root = settings.filesystem_root.resolve()
    target_r = target.resolve()
    target_r.relative_to(root)
    if not target_r.is_file():
        raise ValueError("目标文件不存在或不是普通文件")


def history_catalog_root(settings: JpTvBrowseSettings) -> Path:
    """保存前备份目录：collection-detail feature data 下的 ``history``。"""
    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root")
    db = settings.filesystem_root.resolve()
    feature_db = (feature_data_root("collection-detail") / "db").resolve()
    try:
        db.relative_to(feature_db)
        return (feature_data_root("collection-detail") / "history").resolve()
    except ValueError:
        return (db.parent / "History").resolve()


def history_snapshot_name(yaml_path: Path, *, now: datetime | None = None) -> str:
    """``{stem}__saved-{YYYYMMDD-HHMMSS}{suffix}``（写入 ``History/``）。"""
    dt = now or datetime.now()
    stamp = dt.strftime("%Y%m%d-%H%M%S")
    return f"{yaml_path.stem}__saved-{stamp}{yaml_path.suffix}"


def current_year_catalog_relpath(*, now: datetime | None = None) -> str:
    """新增行固定写入当前日期所在年份的数据文件。"""
    dt = now or datetime.now()
    return f"[JP][TVInfo][{dt:%Y}].yaml"


def _works_list_mut(doc: Any) -> list[Any]:
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for k in ("works", "entries"):
            wl = doc.get(k)
            if isinstance(wl, list):
                return cast(list[Any], wl)
    raise ValueError("YAML 根须为作品数组或含 works / entries 的对象")


def _find_attr_list(work: Any) -> list[Any] | None:
    if not isinstance(work, dict):
        return None
    attrs = work.get("attributes")
    return cast(list[Any], attrs) if isinstance(attrs, list) else None


def _set_scalar_attr(work: dict[str, Any], typ: str, data: Any) -> None:
    attrs = _find_attr_list(work)
    if attrs is None:
        raise ValueError("作品缺少 attributes")
    for i, a in enumerate(attrs):
        if isinstance(a, dict) and a.get("type") == typ:
            na = dict(a)
            na["data"] = data
            attrs[i] = na
            return
    attrs.append({"type": typ, "data": data})


def _clean_rel_path(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip().replace("\\", "/").strip("/")


def _collection_ordered_to_coll_data(
    ordered: Any,
    *,
    domain: str,
    release_type: str,
    path: Any,
    markers: Any,
) -> dict[str, Any]:
    domain_s = domain.strip()
    release_type_s = release_type.strip()
    if not domain_s:
        raise ValueError("domain 不能为空")
    if not release_type_s:
        raise ValueError("release_type 不能为空")

    mk: list[str] = []
    raw_mk = markers if isinstance(markers, list) else []
    for x in raw_mk:
        if isinstance(x, str):
            s = x.strip()
            if s:
                mk.append(s)

    if not isinstance(ordered, list):
        ordered = []

    main: list[dict[str, str]] = []
    cont_map: dict[int, list[dict[str, str]]] = {}
    titles: dict[int, str] = {}

    for it in ordered:
        if not isinstance(it, dict):
            continue
        fm_raw = it.get(TV_JP_PRESS_FORMAT_KEY)
        gp_raw = it.get(TV_JP_PRESS_GROUP_KEY)
        fm = fm_raw.strip() if isinstance(fm_raw, str) else ""
        gp = gp_raw.strip() if isinstance(gp_raw, str) else ""
        if not fm and not gp:
            continue
        pair = {
            TV_JP_PRESS_FORMAT_KEY: fm or "",
            TV_JP_PRESS_GROUP_KEY: gp or "",
        }
        press_path = _clean_rel_path(it.get(TV_JP_PRESS_PATH_KEY))
        if press_path:
            pair[TV_JP_PRESS_PATH_KEY] = press_path
        seg = it.get("segment")
        if seg == "continuation":
            try:
                bi = int(it.get("continuation_index"))
            except (TypeError, ValueError):
                bi = 0
            cont_map.setdefault(bi, []).append(pair)
            ttl = it.get("continuation_title")
            if isinstance(ttl, str) and ttl.strip():
                titles.setdefault(bi, ttl.strip())
        else:
            main.append(pair)

    out: dict[str, Any] = {
        TV_JP_DOMAIN_KEY: domain_s,
        TV_JP_RELEASE_TYPE_KEY: release_type_s,
    }
    path_s = _clean_rel_path(path)
    if path_s:
        out["path"] = path_s
    out["collectioned"] = main
    out["markers"] = mk
    if cont_map:
        seq: list[dict[str, Any]] = []
        for bi in sorted(cont_map.keys()):
            blk: dict[str, Any] = {"collectioned": cont_map[bi]}
            if bi in titles:
                blk["title"] = titles[bi]
            seq.append(blk)
        out["continuations"] = seq
    return out


def _row_ref_from_body_item(raw: Any, *, label: str) -> tuple[str, int]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} 须为对象")
    ysr = raw.get("yaml_source_rel")
    rel_s = ysr.strip().replace("\\", "/") if isinstance(ysr, str) else ""
    if not rel_s:
        raise ValueError(f"{label}.yaml_source_rel 须为非空字符串")
    idx = raw.get("index_in_file")
    try:
        ii = int(idx)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}.index_in_file 非法：{idx!r}") from exc
    return rel_s, ii


def _row_rel_from_body_item(raw: Any, *, label: str) -> str:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} 须为对象")
    ysr = raw.get("yaml_source_rel")
    rel_s = ysr.strip().replace("\\", "/") if isinstance(ysr, str) else ""
    if not rel_s:
        raise ValueError(f"{label}.yaml_source_rel 须为非空字符串")
    return rel_s


def _new_work_from_row_patch(patch: dict[str, Any]) -> dict[str, Any]:
    date_patch = patch.get("date") or {}
    start = date_patch.get("start") if isinstance(date_patch, dict) else ""
    end = date_patch.get("end") if isinstance(date_patch, dict) else ""
    domain_raw = patch.get(TV_JP_DOMAIN_KEY)
    release_raw = patch.get(TV_JP_RELEASE_TYPE_KEY)
    country_raw = patch.get("country")
    name_raw = patch.get("name")

    if not isinstance(domain_raw, str) or not domain_raw.strip():
        raise ValueError("新增行 domain 不能为空")
    if not isinstance(release_raw, str) or not release_raw.strip():
        raise ValueError("新增行 release_type 不能为空")
    country_s = country_raw.strip() if isinstance(country_raw, str) and country_raw.strip() else "japan"
    name_s = name_raw if isinstance(name_raw, str) else ""

    coll_data = _collection_ordered_to_coll_data(
        patch.get("collectioned_ordered"),
        domain=domain_raw,
        release_type=release_raw,
        path=patch.get("path"),
        markers=patch.get("markers"),
    )
    return {
        "attributes": [
            {
                "type": "date",
                "data": {
                    "start": start.strip() if isinstance(start, str) else "",
                    "end": end.strip() if isinstance(end, str) else "",
                },
            },
            {
                "type": "collection-type",
                "data": coll_data,
            },
            {
                "type": "country",
                "data": country_s,
            },
            {
                "type": "name",
                "data": name_s,
            },
        ],
    }


def _apply_row_patch_to_work(work: Any, patch: dict[str, Any]) -> None:
    if not isinstance(work, dict):
        raise ValueError("作品必须为对象")

    attrs = _find_attr_list(work)
    if attrs is None:
        raise ValueError("作品缺少 attributes")

    date_patch = patch.get("date") or {}
    start = date_patch.get("start") if isinstance(date_patch, dict) else None
    end = date_patch.get("end") if isinstance(date_patch, dict) else None
    if isinstance(start, str) and isinstance(end, str):
        _set_scalar_attr(work, "date", {"start": start.strip(), "end": end.strip()})

    name_raw = patch.get("name")
    if isinstance(name_raw, str):
        _set_scalar_attr(work, "name", name_raw)

    country_raw = patch.get("country")
    if isinstance(country_raw, str) and country_raw.strip():
        _set_scalar_attr(work, "country", country_raw.strip())

    domain_raw = patch.get(TV_JP_DOMAIN_KEY)
    release_raw = patch.get(TV_JP_RELEASE_TYPE_KEY)
    markers_raw = patch.get("markers")
    ordered_raw = patch.get("collectioned_ordered")

    if isinstance(domain_raw, str) and isinstance(release_raw, str):
        coll_data = _collection_ordered_to_coll_data(
            ordered_raw,
            domain=domain_raw,
            release_type=release_raw,
            path=patch.get("path"),
            markers=markers_raw,
        )
        _set_scalar_attr(work, "collection-type", coll_data)


def browse_save_yaml_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
    now: datetime | None = None,
) -> list[tuple[Path, str]]:
    """校验 body，按 ``yaml_source_rel`` 写回一至多个数据文件并写入 ``History/`` 快照。

    返回每项 ``(写入的绝对路径, 备份文件名（在 History/ 内）)``。
    """
    rows_raw = body.get("rows")
    rows = rows_raw if isinstance(rows_raw, list) else []
    if rows_raw is not None and not isinstance(rows_raw, list):
        raise ValueError("rows 须为数组")

    new_rows_raw = body.get("new_rows")
    new_rows = new_rows_raw if isinstance(new_rows_raw, list) else []
    if new_rows_raw is not None and not isinstance(new_rows_raw, list):
        raise ValueError("new_rows 须为数组")

    deleted_rows_raw = body.get("deleted_rows")
    deleted_rows = deleted_rows_raw if isinstance(deleted_rows_raw, list) else []
    if deleted_rows_raw is not None and not isinstance(deleted_rows_raw, list):
        raise ValueError("deleted_rows 须为数组")

    if not rows and not new_rows and not deleted_rows:
        raise ValueError(
            "缺少 rows / new_rows / deleted_rows（或为空）；请勾选「编辑模式」后提交，并使用「从配置加载默认文件」打开（不要使用仅上传预览）",
        )

    if settings.filesystem_root is None:
        raise ValueError("未配置 filesystem_root，禁止写盘")

    client_path = body.get("path")
    singles = tuple(settings.resolved_catalog_yaml_paths)
    only_target = Path(singles[0]).resolve() if len(singles) == 1 else None
    clip: Path | None = None
    if isinstance(client_path, str) and client_path.strip():
        clip = Path(client_path.strip()).expanduser().resolve()
        if only_target is not None and clip.resolve() != only_target.resolve():
            raise ValueError("请求的 path 与当前可写 YAML 不一致，请刷新后重试")

    by_rel: dict[str, dict[int, dict[str, Any]]] = {}
    new_by_rel: dict[str, list[dict[str, Any]]] = {}
    delete_by_rel: dict[str, set[int]] = {}

    for ri, rp in enumerate(rows):
        rel_s, ii = _row_ref_from_body_item(rp, label=f"rows[{ri}]")

        tgt_probe = resolve_safe_yaml_under_root(settings.filesystem_root, rel_s)

        if clip is not None and len(singles) == 1 and tgt_probe.resolve() != clip.resolve():
            raise ValueError("rows 内含与当前打开的 YAML 不一致的 yaml_source_rel")

        by_rel.setdefault(rel_s, {})[ii] = rp

    new_rel_s = current_year_catalog_relpath(now=now)
    for ni, nr in enumerate(new_rows):
        if not isinstance(nr, dict):
            raise ValueError(f"new_rows[{ni}] 须为对象")
        rel_s = new_rel_s
        tgt_probe = resolve_safe_yaml_under_root(settings.filesystem_root, rel_s)
        if clip is not None and len(singles) == 1 and tgt_probe.resolve() != clip.resolve():
            raise ValueError("new_rows 当前年份目标文件与当前打开的 YAML 不一致")
        new_by_rel.setdefault(rel_s, []).append(nr)

    for di, dr in enumerate(deleted_rows):
        rel_s, ii = _row_ref_from_body_item(dr, label=f"deleted_rows[{di}]")
        tgt_probe = resolve_safe_yaml_under_root(settings.filesystem_root, rel_s)
        if clip is not None and len(singles) == 1 and tgt_probe.resolve() != clip.resolve():
            raise ValueError("deleted_rows 内含与当前打开的 YAML 不一致的 yaml_source_rel")
        delete_by_rel.setdefault(rel_s, set()).add(ii)

    sorted_rels = sorted(set(by_rel.keys()) | set(new_by_rel.keys()) | set(delete_by_rel.keys()))
    if not sorted_rels:
        raise ValueError("无可写文件路径")

    out: list[tuple[Path, str]] = []
    for rel_s in sorted_rels:
        target = resolve_safe_yaml_under_root(settings.filesystem_root, rel_s).expanduser().resolve()
        new_seq = new_by_rel.get(rel_s, [])
        target_existed = target.is_file()
        if target_existed:
            _assert_save_target_allowed(target, settings)
            raw_text = target.read_text(encoding="utf-8")
            doc = load_yaml_string(raw_text)
        else:
            if not new_seq:
                _assert_save_target_allowed(target, settings)
            target.parent.mkdir(parents=True, exist_ok=True)
            doc = []
        works = _works_list_mut(doc)

        idx_map = by_rel.get(rel_s, {})
        delete_set = delete_by_rel.get(rel_s, set())
        for ii in sorted(idx_map.keys()):
            if ii in delete_set:
                continue
            rp = idx_map[ii]
            if ii < 0 or ii >= len(works):
                raise ValueError(f"{rel_s}: index_in_file 越界：{ii}")
            _apply_row_patch_to_work(works[ii], rp)

        for ii in sorted(delete_set, reverse=True):
            if ii < 0 or ii >= len(works):
                raise ValueError(f"{rel_s}: 删除 index_in_file 越界：{ii}")
            del works[ii]

        for nr in new_seq:
            works.append(_new_work_from_row_patch(nr))

        new_text = dump_yaml_string(doc)
        try:
            load_jp_tv_entries_from_yaml(load_yaml_string(new_text))
        except Exception as e:
            raise ValueError(f"{rel_s} 写回后的 YAML 校验失败：{e}") from e

        hist_root = history_catalog_root(settings)
        hist_root.mkdir(parents=True, exist_ok=True)
        hist_name = ""
        if target_existed:
            hist_name = history_snapshot_name(target)
            hist_fp = hist_root / hist_name
            shutil.copy2(target, hist_fp)

        target.write_text(new_text, encoding="utf-8")
        out.append((target, hist_name))

    return out


_ENUM_EDIT_KEYS = {TV_JP_PRESS_FORMAT_KEY, TV_JP_PRESS_GROUP_KEY}


def _yaml_rt() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.default_flow_style = False
    y.allow_unicode = True
    y.width = 10_000_000
    y.indent(mapping=2, sequence=2, offset=0)
    return y


def _enum_item_value(item: Any) -> str | None:
    if isinstance(item, str):
        s = item.strip()
        return s if s else None
    if isinstance(item, bool):
        return None
    if isinstance(item, (int, float)):
        s = str(item).strip()
        return s if s else None
    if isinstance(item, dict):
        for k in ("value", "code"):
            v = item.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        keys = list(item.keys())
        if len(keys) == 1 and isinstance(keys[0], str) and keys[0].strip():
            return keys[0].strip()
    return None


def _set_enum_item_value(item: Any, new_value: str) -> Any:
    if isinstance(item, str):
        return new_value
    if isinstance(item, dict):
        for k in ("value", "code"):
            if k in item:
                item[k] = new_value
                return item
        keys = list(item.keys())
        if len(keys) == 1 and isinstance(keys[0], str):
            old_key = keys[0]
            item[new_value] = item.pop(old_key)
            return item
    return {"value": new_value}


def _find_or_create_enum_values(raw: Any, enum_key: str) -> list[Any]:
    if not isinstance(raw, dict):
        raise ValueError("浏览配置须为对象，无法编辑 enum")
    enums = raw.get("enum")
    if not isinstance(enums, list):
        enums = []
        raw["enum"] = enums
    for blk in enums:
        if not isinstance(blk, dict):
            continue
        name = blk.get("name")
        if isinstance(name, str) and name.strip() == enum_key:
            vals = blk.get("values")
            if not isinstance(vals, list):
                vals = []
                blk["values"] = vals
            return vals
    blk_new: dict[str, Any] = {"name": enum_key, "values": []}
    enums.append(blk_new)
    return cast(list[Any], blk_new["values"])


def _enum_values_set(values: list[Any]) -> set[str]:
    out: set[str] = set()
    for item in values:
        v = _enum_item_value(item)
        if v:
            out.add(v)
    return out


def _apply_one_enum_edit_to_values(values: list[Any], edit: dict[str, str]) -> bool:
    action = edit["action"]
    old = edit.get("value", "")
    new = edit.get("new_value", "")
    changed = False

    if action == "add":
        if new and new not in _enum_values_set(values):
            values.append(new)
            changed = True
        return changed

    if action == "delete":
        kept: list[Any] = []
        for item in values:
            if _enum_item_value(item) == old:
                changed = True
                continue
            kept.append(item)
        if changed:
            values[:] = kept
        return changed

    if action == "rename":
        seen_new = new in _enum_values_set(values)
        kept2: list[Any] = []
        for item in values:
            if _enum_item_value(item) != old:
                kept2.append(item)
                continue
            if seen_new:
                changed = True
                continue
            kept2.append(_set_enum_item_value(item, new))
            seen_new = True
            changed = True
        if changed:
            values[:] = kept2
        return changed

    return False


def _normalize_enum_edits(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("edits 须为非空数组")
    out: list[dict[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"edits[{i}] 须为对象")
        enum_key = item.get("enum_key")
        action = item.get("action")
        if not isinstance(enum_key, str) or enum_key.strip() not in _ENUM_EDIT_KEYS:
            raise ValueError(f"edits[{i}].enum_key 仅支持 press_format / press_group")
        if not isinstance(action, str) or action.strip() not in {"add", "delete", "rename"}:
            raise ValueError(f"edits[{i}].action 非法")
        act = action.strip()
        val = item.get("value")
        new_val = item.get("new_value")
        val_s = val.strip() if isinstance(val, str) else ""
        new_s = new_val.strip() if isinstance(new_val, str) else ""
        if act in {"delete", "rename"} and not val_s:
            raise ValueError(f"edits[{i}].value 不能为空")
        if act in {"add", "rename"} and not new_s:
            raise ValueError(f"edits[{i}].new_value 不能为空")
        if act == "rename" and val_s == new_s:
            continue
        out.append(
            {
                "enum_key": enum_key.strip(),
                "action": act,
                "value": val_s,
                "new_value": new_s,
            },
        )
    if not out:
        raise ValueError("没有可应用的枚举变更")
    return out


def _apply_enum_renames_to_doc(doc: Any, renames: list[dict[str, str]]) -> int:
    works = _works_list_mut(doc)
    changed = 0
    for work in works:
        attrs = _find_attr_list(work)
        if attrs is None:
            continue
        coll: dict[str, Any] | None = None
        for a in attrs:
            if isinstance(a, dict) and a.get("type") == "collection-type" and isinstance(a.get("data"), dict):
                coll = cast(dict[str, Any], a["data"])
                break
        if coll is None:
            continue

        row_lists: list[Any] = [coll.get("collectioned")]
        conts = coll.get("continuations")
        if isinstance(conts, list):
            for blk in conts:
                if isinstance(blk, dict):
                    row_lists.append(blk.get("collectioned"))

        for rows in row_lists:
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                for rn in renames:
                    k = rn["enum_key"]
                    if row.get(k) == rn["value"]:
                        row[k] = rn["new_value"]
                        changed += 1
    return changed


def browse_apply_enum_edits_from_ui_body(
    body: dict[str, Any],
    *,
    settings: JpTvBrowseSettings,
    config_path: Path | None,
) -> dict[str, Any]:
    edits = _normalize_enum_edits(body.get("edits"))
    if config_path is None or not config_path.is_file():
        raise ValueError("当前未使用可写的浏览配置文件，无法编辑枚举")
    if config_path.name == "browse_config.default.yaml":
        raise ValueError("当前使用包内兜底配置，不能直接编辑枚举；请先使用工程配置文件")

    y = _yaml_rt()
    with config_path.open(encoding="utf-8") as fp:
        raw_cfg = y.load(fp) or {}
    if not isinstance(raw_cfg, dict):
        raise ValueError(f"浏览配置须为对象：{config_path}")

    config_changed = False
    for ed in edits:
        vals = _find_or_create_enum_values(raw_cfg, ed["enum_key"])
        if _apply_one_enum_edit_to_values(vals, ed):
            config_changed = True

    if config_changed:
        with config_path.open("w", encoding="utf-8") as fp:
            y.dump(raw_cfg, fp)

    renames = [ed for ed in edits if ed["action"] == "rename"]
    data_writes: list[dict[str, Any]] = []
    if renames and settings.filesystem_root is not None:
        hist_root = history_catalog_root(settings)
        for abs_s in settings.resolved_catalog_yaml_paths:
            target = Path(abs_s).resolve()
            _assert_save_target_allowed(target, settings)
            raw_text = target.read_text(encoding="utf-8")
            doc = load_yaml_string(raw_text)
            touched = _apply_enum_renames_to_doc(doc, renames)
            if touched <= 0:
                continue
            new_text = dump_yaml_string(doc)
            try:
                load_jp_tv_entries_from_yaml(load_yaml_string(new_text))
            except Exception as e:
                raise ValueError(f"{target.name} 枚举同步后的 YAML 校验失败：{e}") from e
            hist_root.mkdir(parents=True, exist_ok=True)
            hist_name = history_snapshot_name(target)
            shutil.copy2(target, hist_root / hist_name)
            target.write_text(new_text, encoding="utf-8")
            data_writes.append(
                {
                    "path": str(target),
                    "history_file": hist_name,
                    "changes": touched,
                },
            )

    return {
        "config_path": str(config_path.resolve()),
        "config_changed": config_changed,
        "edits_applied": edits,
        "data_writes": data_writes,
    }


def annotate_save_capabilities(
    payload: dict[str, Any],
    *,
    yaml_disk_abs: str | None,
    settings: JpTvBrowseSettings,
    catalog_default: bool = False,
    catalog_disk_abs_paths: tuple[str, ...] | None = None,
) -> None:
    """为浏览 payload 增加 ``save`` 字段。"""
    if not payload.get("ok"):
        return

    def _hint_sibling_history() -> str:
        if settings.filesystem_root is None:
            return ""
        try:
            return str(history_catalog_root(settings))
        except (OSError, ValueError):
            return ""

    sibling_hist = _hint_sibling_history()

    if catalog_default and settings.resolved_catalog_yaml_paths:
        if catalog_disk_abs_paths is not None and len(catalog_disk_abs_paths) > 0:
            abs_list = [str(Path(p).resolve()) for p in catalog_disk_abs_paths]
        else:
            abs_list = [str(Path(p).resolve()) for p in settings.resolved_catalog_yaml_paths]
        mf = len(abs_list) > 1
        payload["save"] = {
            "enabled": True,
            "multi_file": mf,
            "target_path": abs_list[0],
            "target_paths": abs_list,
            "history_hint": sibling_hist,
            "help": "保存时按数据文件的 yaml_source_rel 写回 DB；每次保存先在 collection-detail 的 history 中写入带时间戳的备份。",
        }
        return

    if yaml_disk_abs:
        fp = Path(yaml_disk_abs)
        if fp.is_file():
            legacy_hist = str(fp.resolve().parent / "History")
            payload["save"] = {
                "enabled": True,
                "multi_file": False,
                "target_path": str(fp.resolve()),
                "target_paths": [str(fp.resolve())],
                "history_hint": sibling_hist or legacy_hist,
                "help": "编辑后保存会先在 collection-detail 的 history 中写入备份（若未配置 DB 路径则退化到目标文件同目录 History）。",
            }
            return

    payload["save"] = {
        "enabled": False,
        "multi_file": False,
        "target_path": None,
        "target_paths": [],
        "history_hint": sibling_hist,
        "reason": "仅「从配置加载数据」打开的会话可写入磁盘（上传预览不可直接保存以防误覆盖）。",
    }
