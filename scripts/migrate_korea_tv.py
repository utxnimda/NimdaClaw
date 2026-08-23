"""Import the existing Korean-TV shortcut library into collection-detail YAML.

The default mode is read-only.  ``--write-yaml`` writes catalog YAML inside the
workspace.  ``--apply-shortcuts`` performs the external shortcut migration only
after it has built and verified a complete staging tree; the old year folders
are moved to a timestamped sibling backup and are never deleted.
"""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

from ruamel.yaml import YAML


DEFAULT_SHORTCUT_ROOT = Path(r"E:\LinkVideo\[3D] Korea")
DEFAULT_MEDIA_ROOT = Path(r"G:\Video\电视剧\韩剧")
DEFAULT_DB_ROOT = Path(__file__).resolve().parents[1] / "data" / "features" / "collection-detail" / "db"

DATE_DIR_RE = re.compile(r"^\[(\d{8})\](?:\[(\d{8})\])?\s+(.+)$")
YEAR_DIR_RE = re.compile(r"^\[(?:\d{4}|200X)\]$")
VIDEO_RESOURCE_RE = re.compile(
    r"_(?P<format>(?:\d{3,4}[pP]|BDRip|HDTV|DVDRip)(?:\s+Ver\.\s*\d+)?)$",
    re.IGNORECASE,
)

# These three video resources exist in the media library but have no legacy
# shortcut from which dates could be recovered.  Dates were checked against
# their original Korean broadcast runs before this migration was written.
KNOWN_MISSING_DATES: dict[str, tuple[str, str]] = {
    "传说的魔女": ("20141025", "20150308"),
    "我的公主": ("20110105", "20110224"),
    "达利和土豆汤": ("20210922", "20211111"),
}

# A few legacy shortcut folders contain transposed or production-period dates.
# Keep the corrections explicit so a repeat migration remains deterministic.
KNOWN_DATE_CORRECTIONS: dict[tuple[str, str, str], tuple[str, str]] = {
    ("20161909", "20171209", "心里的声音"): ("20161107", "20170106"),
    ("20220700", "20230200", "我的女神室友斗娜"): ("20231020", "20231020"),
    ("20250929", "20250104", "善良的女人夫世美"): ("20250929", "20251104"),
}


def _powershell(command: str, *, stdin: str | None = None) -> str:
    encoded = base64.b64encode(command.encode("utf-16le")).decode("ascii")
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout or "PowerShell failed").strip())
    return proc.stdout.strip()


def read_shortcuts(root: Path) -> list[dict[str, str]]:
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = [IO.Path]::GetFullPath($env:NIMDA_KOREA_SHORTCUT_ROOT)
$shell = New-Object -ComObject WScript.Shell
$rows = @(
  Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.lnk' |
    Where-Object { $_.FullName -notlike "$root\Finish\*" } |
    ForEach-Object {
      $shortcut = $shell.CreateShortcut($_.FullName)
      [pscustomobject]@{ path = $_.FullName; target = $shortcut.TargetPath }
    }
)
$rows | ConvertTo-Json -Compress
"""
    old = os.environ.get("NIMDA_KOREA_SHORTCUT_ROOT")
    os.environ["NIMDA_KOREA_SHORTCUT_ROOT"] = str(root)
    try:
        raw = _powershell(script)
    finally:
        if old is None:
            os.environ.pop("NIMDA_KOREA_SHORTCUT_ROOT", None)
        else:
            os.environ["NIMDA_KOREA_SHORTCUT_ROOT"] = old
    value = json.loads(raw or "[]")
    rows = value if isinstance(value, list) else [value]
    return [
        {"path": str(row.get("path") or ""), "target": str(row.get("target") or "")}
        for row in rows
        if isinstance(row, dict) and row.get("path")
    ]


def _path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _media_inventory(media_root: Path) -> tuple[dict[str, list[Path]], list[Path]]:
    leaf_index: dict[str, list[Path]] = defaultdict(list)
    resources: list[Path] = []
    for work_dir in sorted((p for p in media_root.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
        for child in sorted((p for p in work_dir.iterdir() if p.is_dir()), key=lambda p: p.name.casefold()):
            leaf_index[child.name.casefold()].append(child)
            resources.append(child)
    return dict(leaf_index), resources


def _candidate_for_broken(
    target: Path,
    work_name: str,
    press_name: str,
    leaf_index: dict[str, list[Path]],
    media_root: Path,
) -> Path | None:
    by_leaf = leaf_index.get(target.name.casefold(), [])
    if len(by_leaf) == 1:
        return by_leaf[0]
    try:
        work_dir = next(
            p for p in media_root.iterdir() if p.is_dir() and p.name.casefold() == work_name.casefold()
        )
    except StopIteration:
        return None
    expected_names = {
        f"{work_name}_{press_name}".casefold(),
        f"{work_dir.name}_{press_name}".casefold(),
    }
    matches = [p for p in work_dir.iterdir() if p.is_dir() and p.name.casefold() in expected_names]
    return matches[0] if len(matches) == 1 else None


def build_catalog(shortcut_root: Path, media_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = shortcut_root.resolve()
    media = media_root.resolve()
    leaf_index, resources = _media_inventory(media)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    target_to_group: dict[str, tuple[str, str, str]] = {}
    date_hints_by_parent: dict[str, tuple[str, str, str]] = {}
    skipped: list[dict[str, str]] = []
    repaired: list[dict[str, str]] = []

    legacy_rows = read_shortcuts(root)
    for row in legacy_rows:
        shortcut = Path(row["path"])
        try:
            rel = shortcut.resolve().relative_to(root)
        except ValueError:
            skipped.append({"shortcut": str(shortcut), "reason": "outside shortcut root"})
            continue
        parts = rel.parts
        if len(parts) != 3 or not YEAR_DIR_RE.fullmatch(parts[0]):
            skipped.append({"shortcut": str(shortcut), "reason": "unexpected shortcut layout"})
            continue
        match = DATE_DIR_RE.fullmatch(parts[1])
        if not match:
            skipped.append({"shortcut": str(shortcut), "reason": "unparseable date folder"})
            continue
        start, end, work_name = match.group(1), match.group(2) or "", match.group(3).strip()
        start, end = KNOWN_DATE_CORRECTIONS.get((start, end, work_name), (start, end))
        for label, value in (("start", start), ("end", end)):
            if value:
                try:
                    datetime.strptime(value, "%Y%m%d")
                except ValueError as exc:
                    raise ValueError(f"invalid {label} date for {work_name}: {value}") from exc
        if end and end < start:
            raise ValueError(f"broadcast end precedes start for {work_name}: {start} -> {end}")
        press_name = shortcut.stem
        target = Path(row.get("target") or "")
        resolved: Path | None = None
        if row.get("target") and target.is_dir():
            try:
                target.resolve().relative_to(media)
                resolved = target.resolve()
            except ValueError:
                resolved = _candidate_for_broken(target, work_name, press_name, leaf_index, media)
        elif row.get("target"):
            resolved = _candidate_for_broken(target, work_name, press_name, leaf_index, media)
        if resolved is None:
            # A removed press version can still carry the only legacy dates for
            # a work whose parent directory remains in the current media root.
            target_parent = target.parent
            if target_parent.is_dir():
                try:
                    target_parent.resolve().relative_to(media)
                    date_hints_by_parent[_path_key(target_parent)] = (start, end, work_name)
                except ValueError:
                    pass
            skipped.append(
                {
                    "shortcut": str(shortcut),
                    "target": str(target),
                    "reason": "target missing and no unique media replacement",
                }
            )
            continue
        if _path_key(resolved) != _path_key(target):
            repaired.append({"shortcut": str(shortcut), "old_target": str(target), "new_target": str(resolved)})
        group_key = (start, end, work_name)
        group = groups.setdefault(
            group_key,
            {
                "start": start,
                "end": end,
                "name": work_name,
                "path": str(resolved.parent),
                "press": [],
            },
        )
        if _path_key(group["path"]) != _path_key(resolved.parent):
            raise ValueError(f"one work maps to multiple media parents: {work_name}")
        press_item = {
            "press_format": press_name,
            "press_group": "----",
            "press_path": resolved.name,
            "target": str(resolved),
        }
        if _path_key(resolved) not in {_path_key(item["target"]) for item in group["press"]}:
            group["press"].append(press_item)
        target_to_group[_path_key(resolved)] = group_key

    added_resources: list[dict[str, str]] = []
    for resource in resources:
        if _path_key(resource) in target_to_group:
            continue
        fmt_match = VIDEO_RESOURCE_RE.search(resource.name)
        if not fmt_match:
            continue
        parent = resource.parent
        candidate_groups = [key for key, value in groups.items() if _path_key(value["path"]) == _path_key(parent)]
        if len(candidate_groups) == 1:
            group_key = candidate_groups[0]
        else:
            legacy_hint = date_hints_by_parent.get(_path_key(parent))
            dates = KNOWN_MISSING_DATES.get(parent.name)
            if legacy_hint is not None:
                group_key = legacy_hint
            elif dates is not None:
                group_key = (dates[0], dates[1], parent.name)
            else:
                skipped.append(
                    {
                        "target": str(resource),
                        "reason": "video resource has no shortcut and no verified broadcast dates",
                    }
                )
                continue
            groups[group_key] = {
                "start": group_key[0],
                "end": group_key[1],
                "name": group_key[2],
                "path": str(parent),
                "press": [],
            }
        group = groups[group_key]
        press_format = fmt_match.group("format")
        group["press"].append(
            {
                "press_format": press_format,
                "press_group": "----",
                "press_path": resource.name,
                "target": str(resource),
            }
        )
        target_to_group[_path_key(resource)] = group_key
        added_resources.append({"work": group["name"], "press": press_format, "target": str(resource)})

    works = sorted(groups.values(), key=lambda item: (item["start"], item["name"].casefold()))
    for work in works:
        work["press"].sort(key=lambda item: (item["press_format"].casefold(), item["press_path"].casefold()))
    report = {
        "legacy_shortcut_count": len(legacy_rows),
        "work_count": len(works),
        "press_count": sum(len(work["press"]) for work in works),
        "repaired_count": len(repaired),
        "repaired": repaired,
        "added_resource_count": len(added_resources),
        "added_resources": added_resources,
        "skipped_count": len(skipped),
        "skipped": skipped,
    }
    return works, report


def _yaml_document(works: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for work in works:
        collectioned = [
            {
                "press_format": item["press_format"],
                "press_group": item["press_group"],
                "press_path": item["press_path"],
            }
            for item in work["press"]
        ]
        result.append(
            {
                "attributes": [
                    {"type": "date", "data": {"start": work["start"], "end": work["end"]}},
                    {
                        "type": "collection-type",
                        "data": {
                            "domain": "television",
                            "release_type": "tv",
                            "path": work["path"],
                            "collectioned": collectioned,
                            "markers": [],
                        },
                    },
                    {"type": "country", "data": "korea"},
                    {"type": "name", "data": work["name"]},
                ]
            }
        )
    return result


def write_yaml(works: list[dict[str, Any]], db_root: Path) -> list[str]:
    by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for work in works:
        by_year[str(work["start"])[:4]].append(work)
    yaml = YAML()
    yaml.allow_unicode = True
    yaml.default_flow_style = False
    yaml.width = 4096
    db_root.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    expected: set[Path] = set()
    for year, rows in sorted(by_year.items()):
        path = db_root / f"[KR][TVInfo][{year}].yaml"
        expected.add(path.resolve())
        with path.open("w", encoding="utf-8", newline="\n") as fp:
            yaml.dump(_yaml_document(rows), fp)
        written.append(str(path.resolve()))
    stale = [p for p in db_root.glob("[[]KR[]][[]TVInfo[]][[]*[]].yaml") if p.resolve() not in expected]
    if stale:
        raise RuntimeError("stale KR YAML files must be reviewed manually: " + ", ".join(str(p) for p in stale))
    return written


def shortcut_plan(works: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for work in works:
        date_label = f"[{work['start']}]" + (f"[{work['end']}]" if work["end"] else "")
        for press in work["press"]:
            rel = Path(f"[{str(work['start'])[:4]}]") / f"{date_label} {work['name']}" / f"{press['press_format']}.lnk"
            key = str(rel).casefold()
            if key in seen:
                raise ValueError(f"duplicate shortcut output path: {rel}")
            seen.add(key)
            rows.append({"relpath": str(rel), "target": press["target"]})
    return rows


def _create_shortcuts(root: Path, plan: list[dict[str, str]]) -> None:
    payload = json.dumps(
        [{"path": str(root / row["relpath"]), "target": row["target"]} for row in plan],
        ensure_ascii=False,
    )
    script = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
$rows = [Console]::In.ReadToEnd() | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
foreach ($row in $rows) {
  $parent = [IO.Path]::GetDirectoryName([string]$row.path)
  [IO.Directory]::CreateDirectory($parent) | Out-Null
  $shortcut = $shell.CreateShortcut($row.path)
  $shortcut.TargetPath = $row.target
  $shortcut.Save()
}
"""
    _powershell(script, stdin=payload)


def apply_shortcuts(shortcut_root: Path, works: list[dict[str, Any]]) -> dict[str, Any]:
    source = shortcut_root.resolve()
    plan = shortcut_plan(works)
    missing = [row["target"] for row in plan if not Path(row["target"]).is_dir()]
    if missing:
        raise FileNotFoundError("shortcut targets disappeared: " + ", ".join(missing[:10]))
    finish = source / "Finish"
    if finish.exists():
        if any(finish.iterdir()):
            raise FileExistsError(f"refusing to overwrite non-empty target: {finish}")
        finish.rmdir()
    unexpected = [
        p for p in source.iterdir()
        if not YEAR_DIR_RE.fullmatch(p.name) and p.name != "Finish" and not p.name.startswith(".korea-migration-")
    ]
    if unexpected:
        raise RuntimeError("unexpected items in legacy shortcut root: " + ", ".join(str(p) for p in unexpected))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    staging = source / f".korea-migration-{stamp}"
    backup = source.parent / f"{source.name}.Legacy-{stamp}"
    if staging.exists() or backup.exists():
        raise FileExistsError("migration staging or backup path already exists")
    staging.mkdir()
    _create_shortcuts(staging, plan)
    created = sorted(staging.rglob("*.lnk"))
    if len(created) != len(plan):
        raise RuntimeError(f"staging verification failed: expected {len(plan)}, got {len(created)}")

    backup.mkdir()
    moved: list[tuple[Path, Path]] = []
    try:
        for child in sorted((p for p in source.iterdir() if YEAR_DIR_RE.fullmatch(p.name)), key=lambda p: p.name):
            destination = backup / child.name
            shutil.move(str(child), str(destination))
            moved.append((destination, child))
        staging.rename(finish)
    except Exception:
        if staging.exists():
            for old, original in reversed(moved):
                if old.exists() and not original.exists():
                    shutil.move(str(old), str(original))
        raise
    return {
        "output_root": str(finish),
        "backup_root": str(backup),
        "created": len(created),
        "legacy_year_dirs_moved": len(moved),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shortcut-root", type=Path, default=DEFAULT_SHORTCUT_ROOT)
    parser.add_argument("--media-root", type=Path, default=DEFAULT_MEDIA_ROOT)
    parser.add_argument("--db-root", type=Path, default=DEFAULT_DB_ROOT)
    parser.add_argument("--write-yaml", action="store_true")
    parser.add_argument("--apply-shortcuts", action="store_true")
    args = parser.parse_args()

    works, report = build_catalog(args.shortcut_root, args.media_root)
    result: dict[str, Any] = {**report, "shortcut_plan_count": len(shortcut_plan(works))}
    if args.write_yaml:
        result["yaml_files"] = write_yaml(works, args.db_root)
    if args.apply_shortcuts:
        result["migration"] = apply_shortcuts(args.shortcut_root, works)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
