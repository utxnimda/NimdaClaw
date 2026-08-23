"""Read the collection-detail database as directory-organization evidence."""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from work_catalog_yaml.jp_tv.load import load_jp_tv_yaml_file
from work_catalog_yaml.jp_tv.validate import (
    entry_collection_type_data,
    entry_country_slug,
    entry_display_name,
    entry_domain_slug,
    entry_release_type_slug,
)


def normalized_identity(value: str) -> str:
    """Return a comparison key that tolerates width, case, spacing and decoration."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def normalized_value(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(Path(path).expanduser())))


@dataclass(frozen=True)
class PressRecord:
    press_format: str
    press_group: str
    press_path: str = ""


@dataclass(frozen=True)
class CatalogWork:
    name: str
    path: str
    domain: str
    country: str
    release_type: str
    presses: tuple[PressRecord, ...]
    source_file: str

    @property
    def identity(self) -> str:
        return normalized_identity(self.name)

    def presses_for(self, press_format: str, press_group: str = "") -> tuple[PressRecord, ...]:
        fmt_key = normalized_value(press_format)
        group_key = normalized_value(press_group)
        return tuple(
            press
            for press in self.presses
            if normalized_value(press.press_format) == fmt_key
            and (not group_key or normalized_value(press.press_group) == group_key)
        )


@dataclass(frozen=True)
class MediaCatalog:
    works: tuple[CatalogWork, ...]
    catalog_root: Path

    @classmethod
    def load(
        cls,
        catalog_root: str | Path,
        *,
        domain: str = "animation",
        country: str = "japan",
    ) -> "MediaCatalog":
        root = Path(catalog_root).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"作品数据库目录不存在：{root}")
        works: list[CatalogWork] = []
        errors: list[str] = []
        for yaml_path in sorted(root.glob("*.yaml")):
            try:
                entries = load_jp_tv_yaml_file(yaml_path)
            except (OSError, ValueError) as exc:
                errors.append(f"{yaml_path.name}: {exc}")
                continue
            for entry in entries:
                if domain and normalized_value(entry_domain_slug(entry)) != normalized_value(domain):
                    continue
                if country and normalized_value(entry_country_slug(entry)) != normalized_value(country):
                    continue
                data = entry_collection_type_data(entry)
                rows = data.get("collectioned")
                presses: list[PressRecord] = []
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        press_format = str(row.get("press_format") or "").strip()
                        press_group = str(row.get("press_group") or "").strip()
                        if not press_format or not press_group:
                            continue
                        presses.append(
                            PressRecord(
                                press_format=press_format,
                                press_group=press_group,
                                press_path=str(row.get("press_path") or "").strip(),
                            )
                        )
                works.append(
                    CatalogWork(
                        name=entry_display_name(entry).strip(),
                        path=str(data.get("path") or "").strip(),
                        domain=entry_domain_slug(entry).strip(),
                        country=entry_country_slug(entry).strip(),
                        release_type=entry_release_type_slug(entry).strip(),
                        presses=tuple(presses),
                        source_file=str(yaml_path),
                    )
                )
        if errors:
            joined = "；".join(errors[:10])
            raise ValueError(f"读取作品数据库失败：{joined}")
        return cls(works=tuple(works), catalog_root=root)

    def matching_family_for_root(self, work_root: str | Path) -> tuple[CatalogWork, ...]:
        """Return exact-path and related-name works without raising when absent."""

        root = Path(work_root).expanduser().resolve()
        root_key = path_key(root)
        root_identity = normalized_identity(root.name)
        exact_path = [work for work in self.works if work.path and path_key(work.path) == root_key]
        exact_name = [work for work in self.works if work.identity == root_identity and root_identity]
        anchors = [*exact_path, *exact_name]
        # Prefix-only similarity is not enough to claim a catalog match (for
        # example ``Air`` versus ``Air Gear``).  A family must first be
        # anchored by this exact filesystem path or the exact root name.
        if not anchors:
            return ()
        related = [
            work
            for work in self.works
            if work.identity
            and root_identity
            and (work.identity.startswith(root_identity) or root_identity.startswith(work.identity))
        ]
        combined: list[CatalogWork] = []
        seen: set[tuple[str, str]] = set()
        for work in (*anchors, *related):
            key = (work.source_file, work.name)
            if key in seen:
                continue
            seen.add(key)
            combined.append(work)
        return tuple(combined)

    def family_for_root(self, work_root: str | Path) -> tuple[CatalogWork, ...]:
        root = Path(work_root).expanduser().resolve()
        combined = self.matching_family_for_root(root)
        if not combined:
            raise ValueError(
                f"作品数据库中找不到与目录对应的作品：{root}。"
                "请先在 collection-detail 数据库确认作品名或 path。"
            )
        return tuple(combined)

    def with_works(self, works: tuple[CatalogWork, ...] | list[CatalogWork]) -> "MediaCatalog":
        """Return an in-memory catalog extended with validated draft works."""

        return MediaCatalog(works=(*self.works, *tuple(works)), catalog_root=self.catalog_root)
