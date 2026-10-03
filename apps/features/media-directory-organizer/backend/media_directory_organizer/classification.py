"""Extensible press-group classifiers for layout inside a target folder.

Classifiers decide a canonical category and the relative path *inside* that
category. They never choose an absolute target, rename a file, or move data.
The plan builder materializes ``<press base>_<Category>`` below the validated
press target and keeps the existing collision and filesystem safety checks.

Every category is represented by one overridable ``filter_*`` method. A
press-group implementation can therefore adjust one folder rule for one work
without copying the complete classifier.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol, runtime_checkable

from media_directory_organizer.catalog import (
    normalize_press_group,
    normalized_identity,
    normalized_value,
)
from work_catalog_yaml.media_groups import media_group_classifier_family


CATEGORY_DISC = "Disc"
CATEGORY_CD = "CD"
CATEGORY_OP_ED = "OP+ED"
CATEGORY_IMAGE = "Image"
CATEGORY_MENU = "Menu"
CATEGORY_CM = "CM"
CATEGORY_PV = "PV"
CATEGORY_PREVIEW = "Preview"
CATEGORY_SP = "SP"
CATEGORY_FONTS = "Fonts"
CATEGORY_SUBS = "Subs"
CATEGORY_MV = "MV"
CATEGORY_LIVE = "Live"
CATEGORY_OTHERS = "Others"

LAYOUT_CATEGORIES = (
    CATEGORY_DISC,
    CATEGORY_CD,
    CATEGORY_OP_ED,
    CATEGORY_IMAGE,
    CATEGORY_MENU,
    CATEGORY_CM,
    CATEGORY_PV,
    CATEGORY_PREVIEW,
    CATEGORY_SP,
    CATEGORY_FONTS,
    CATEGORY_SUBS,
    CATEGORY_MV,
    CATEGORY_LIVE,
    CATEGORY_OTHERS,
)

_INVALID_WINDOWS_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_VIDEO_EXTENSIONS = {
    ".avi",
    ".m2ts",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".rmvb",
    ".ts",
    ".webm",
    ".wmv",
}
_CD_AUDIO_EXTENSIONS = {
    ".aac",
    ".ape",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".tak",
    ".tta",
    ".wav",
    ".wv",
}
_DISC_SIDECAR_AUDIO_EXTENSIONS = {".ac3", ".dts", ".mka"}
_CHECKSUM_EXTENSIONS = {
    ".crc",
    ".crc32",
    ".md5",
    ".md5sum",
    ".sfv",
    ".sha1",
    ".sha224",
    ".sha256",
    ".sha256sum",
    ".sha384",
    ".sha512",
    ".xxh",
    ".xxh3",
}
_IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jp2",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
_FONT_EXTENSIONS = {".otc", ".otf", ".ttc", ".ttf", ".woff", ".woff2"}
_SUBTITLE_EXTENSIONS = {".ass", ".idx", ".smi", ".srt", ".ssa", ".sub", ".sup"}
_ARCHIVE_EXTENSIONS = {".7z", ".rar", ".tar", ".zip"}
_DISC_IMAGE_EXTENSIONS = {".bin", ".ccd", ".img", ".mdf", ".mds", ".sub"}

_CATEGORY_COMPONENT_ALIASES: dict[str, tuple[str, ...]] = {
    CATEGORY_DISC: ("disc",),
    CATEGORY_CD: ("cd", "cds", "music", "spcd"),
    CATEGORY_OP_ED: ("op+ed", "oped"),
    CATEGORY_IMAGE: (
        "image",
        "images",
        "booklet",
        "booklets",
        "scan",
        "scans",
        "bd scans",
        "bd-scans",
        "bd_scans",
    ),
    CATEGORY_MENU: ("menu", "menus"),
    CATEGORY_CM: ("cm",),
    CATEGORY_PV: ("pv",),
    CATEGORY_PREVIEW: ("preview", "previews", "yokoku", "予告"),
    CATEGORY_SP: ("sp", "special", "specials", "bonus"),
    CATEGORY_FONTS: ("font", "fonts"),
    CATEGORY_SUBS: ("sub", "subs", "subtitle", "subtitles"),
    CATEGORY_MV: ("mv",),
    CATEGORY_LIVE: ("live",),
    CATEGORY_OTHERS: ("other", "others"),
}

# These are coarse source containers, not final categories. A more specific
# filename or nested folder is allowed to win, e.g. SPs/Menu -> Menu and
# Extras/[Fonts].rar -> Fonts.
_COARSE_CONTAINERS = {
    "sps": CATEGORY_SP,
    "extras": CATEGORY_OTHERS,
    "extra": CATEGORY_OTHERS,
}

_FONT_RE = re.compile(r"(?<![0-9a-z])fonts?(?![0-9a-z])", re.IGNORECASE)
_SUBS_RE = re.compile(
    r"(?<![0-9a-z])(?:subs?|subtitles?)(?![0-9a-z])|字幕",
    re.IGNORECASE,
)
_CD_RE = re.compile(
    r"(?<![0-9a-z])(?:ost|original[ ._-]*soundtrack|soundtrack|"
    r"character[ ._-]*song|drama[ ._-]*cd|spcd[ ._-]*\d*(?:v\d+)?|"
    r"cd[ ._-]*\d+(?:v\d+)?)(?![0-9a-z])|"
    r"オリジナルサウンドトラック|サウンドトラック|キャラクターソング|ドラマ[ ._-]*cd",
    re.IGNORECASE,
)
_AUDIO_ARCHIVE_RE = re.compile(
    r"\[eac\]|\([^)]*(?<![0-9a-z])(?:flac|wav|ape|tak|tta|cue)(?![0-9a-z])[^)]*\)",
    re.IGNORECASE,
)
_MENU_RE = re.compile(r"(?<![0-9a-z])(?:bd[ ._-]*)?menu\d*(?![0-9a-z])|メニュー", re.IGNORECASE)
_CM_RE = re.compile(
    r"(?<![0-9a-z])(?:tv[ ._-]*cm|cm\d*(?:v\d+)?|commercials?)(?![0-9a-z])",
    re.IGNORECASE,
)
_PREVIEW_RE = re.compile(
    r"(?<![0-9a-z])(?:preview|trailer|teaser|yokoku)(?![0-9a-z])|予告",
    re.IGNORECASE,
)
_PV_RE = re.compile(
    r"(?<![0-9a-z])(?:pv\d*(?:v\d+)?|promotion(?:al)?[ ._-]*(?:video|movie))(?![0-9a-z])",
    re.IGNORECASE,
)
_OP_ED_RE = re.compile(
    r"(?<![0-9a-z])(?:nc[ ._-]*(?:op|ed)\d*(?:v\d+)?|"
    r"op\d*(?:v\d+)?|ed\d*(?:v\d+)?|"
    r"clean[ ._-]*(?:op|ed|opening|ending)|"
    r"creditless[ ._-]*(?:op|ed|opening|ending))(?![0-9a-z])",
    re.IGNORECASE,
)
_NC_VERSION_RE = re.compile(r"(?<![0-9a-z])nc[ ._-]*(?:ver|version)(?![0-9a-z])", re.IGNORECASE)
_MV_RE = re.compile(
    r"(?<![0-9a-z])(?:mv\d*|music[ ._-]*video)(?![0-9a-z])",
    re.IGNORECASE,
)
_LIVE_RE = re.compile(
    r"(?:[\[(](?:live|concert|event|fes)(?:\d+)?[\])])|"
    r"(?<![0-9a-z])(?:live[ ._-]*(?:concert|event|fes)|concert|fan[ ._-]*meeting)(?![0-9a-z])",
    re.IGNORECASE,
)
_SP_RE = re.compile(
    r"(?<![0-9a-z])(?:sp[ ._-]*\d+(?:v\d+)?|specials?\d*(?:v\d+)?|"
    r"bonus[ ._-]*video|mini[ ._-]*ova|recap|summary|tokuten)(?![0-9a-z])|特典",
    re.IGNORECASE,
)
_OVA_OAD_RE = re.compile(
    r"(?<![0-9a-z])(?:ova|oad)[ ._-]*\d*(?:v\d+)?(?![0-9a-z])",
    re.IGNORECASE,
)
_IMAGE_RE = re.compile(
    r"(?<![0-9a-z])(?:booklets?|scans?|jackets?|covers?|artbooks?|bd[ ._-]*scans?)(?![0-9a-z])",
    re.IGNORECASE,
)
_OTHERS_RE = re.compile(
    r"(?<![0-9a-z])(?:logo|advice|notice|bansen|interview|iv\d+|info|"
    r"making|commentary|readme|torrent|checksum)(?![0-9a-z])|[\[(]iv[\])]",
    re.IGNORECASE,
)
_COMMENTARY_RE = re.compile(r"(?<![0-9a-z])commentary(?![0-9a-z])", re.IGNORECASE)
_COMMENTARY_INTERVIEW_RE = re.compile(
    r"(?<![0-9a-z])(?:interview|making)(?![0-9a-z])",
    re.IGNORECASE,
)
_RELEASE_SIDECAR_RE = re.compile(
    r"(?<![0-9a-z])(?:x26[45]|ma10p|hi10p|1080p|2160p)(?![0-9a-z])",
    re.IGNORECASE,
)
_RESOLUTION_PRESS_FORMAT_RE = re.compile(
    r"^(?:\d{3,4}[pi]|[248]k|\d{3,4}[x×]\d{3,4})$",
    re.IGNORECASE,
)
_SIDECAR_LANGUAGE_RE = re.compile(
    r"(?:\.(?:sc|tc|chs|cht|jpn?|ja|eng?|commentary|zh[-_]?hans|zh[-_]?hant))+$",
    re.IGNORECASE,
)

_SEASON_EPISODE_RE = re.compile(
    r"(?<![0-9a-z])s\s*0*(?P<season>\d{1,2})[ ._-]*e(?:p(?:isode)?)?\s*"
    r"(?P<episode>\d{1,4}(?:\.\d+)?)(?:v\d+)?(?!\d)",
    re.IGNORECASE,
)
_LABELED_EPISODE_RE = re.compile(
    r"(?<![0-9a-z])(?:ep(?:isode)?|e|#)\s*0*(?P<episode>\d{1,4}(?:\.\d+)?)"
    r"(?:v\d+)?(?!\d)",
    re.IGNORECASE,
)
_CJK_EPISODE_RE = re.compile(
    r"第\s*0*(?P<episode>\d{1,4}(?:\.\d+)?)\s*(?:話|话|集)",
    re.IGNORECASE,
)
_BRACKET_EPISODE_RE = re.compile(
    r"[\[【(]\s*0*(?P<start>\d{1,4}(?:\.\d+)?)"
    r"(?:\s*[-~～]\s*0*(?P<end>\d{1,4}(?:\.\d+)?))?"
    r"(?:v\d+|\+)?(?:\s*(?:fin|end))?"
    r"(?:\s*\([^()\[\]\r\n]{1,80}\))?\s*[\]】)]",
    re.IGNORECASE,
)
_TRAILING_EPISODE_RE = re.compile(
    r"(?<![0-9a-z])(?<!\d\.)0*(?P<start>\d{1,3}(?:\.\d+)?)"
    r"(?:\s*[-~～]\s*0*(?P<end>\d{1,3}(?:\.\d+)?))?"
    r"(?:v\d+|\+)?(?=\s*(?:(?:tv|oa|dc|commentary|full[ ._-]*ver)(?![0-9a-z])|[\[【(]|$))",
    re.IGNORECASE,
)
_NON_EPISODE_NUMBERS = {
    240,
    264,
    265,
    360,
    480,
    576,
    720,
    1080,
    2160,
    4320,
}
_DISC_VERSION_DETAIL_RE = re.compile(
    r"(?<![0-9a-z])(?:"
    r"tv|oa|on[ ._-]*air|dc|director(?:'s)?[ ._-]*cut|commentary|"
    r"full[ ._-]*ver(?:sion)?|"
    r"v\d+|ver(?:sion)?[ ._-]*\d+|rev(?:ision)?[ ._-]*\d+|"
    r"bd|bdrip|blu[ ._-]*ray|dvd|remux|web(?:[ ._-]*dl)?|"
    r"\d{3,4}[pi]|\d{3,4}\s*[x×]\s*\d{3,4}|"
    r"ma\d+p|hi\d+p|\d+bit|x26[45]|h26[45]|avc|hevc|av1|"
    r"flac|aac|ac3|eac3|dts|truehd|pcm|"
    r"chs|cht|sc|tc|jpn?|ja|eng?|zh[ ._-]*hans|zh[ ._-]*hant"
    r")(?![0-9a-z])",
    re.IGNORECASE,
)
_HASH_BRACKET_RE = re.compile(r"[\[【(][0-9a-f]{8,64}[\]】)]", re.IGNORECASE)


def is_vcb_family_group(value: str) -> bool:
    """Return whether a real database group code belongs to the VCB family."""

    if normalized_identity(value).startswith("vcb"):
        return True
    # Several Note.h abbreviations contain VCB without starting with it (VA,
    # VAF, VAL, VTL, VCDM, ...).  Route those by normalized membership rather
    # than by the abbreviation's spelling.
    try:
        return media_group_classifier_family(value) == "VCB"
    except (OSError, ValueError):
        return False


def _safe_relative_path(value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"文件布局路径必须是安全的相对路径：{value}")
    if any(_INVALID_WINDOWS_NAME_RE.search(part) for part in path.parts):
        raise ValueError(f"文件布局路径含 Windows 非法字符：{value}")
    return path


def _safe_category(value: str) -> str:
    category = str(value).strip()
    if (
        not category
        or Path(category).name != category
        or category in {".", ".."}
        or _INVALID_WINDOWS_NAME_RE.search(category)
    ):
        raise ValueError(f"布局分类名称非法：{value}")
    return category


def _normalized_text(value: str | Path) -> str:
    return unicodedata.normalize("NFKC", str(value)).casefold()


def _component_category(component: str) -> str | None:
    value = _normalized_text(component).strip()
    for category, aliases in _CATEGORY_COMPONENT_ALIASES.items():
        for alias in aliases:
            normalized_alias = _normalized_text(alias)
            if value == normalized_alias or value.endswith("_" + normalized_alias):
                return category
    return None


def _structural_layout(relative_path: Path) -> tuple[str | None, Path, str]:
    """Return explicit category, category-inner path and coarse container."""

    parts = relative_path.parts
    directories = parts[:-1]
    if not directories:
        return None, relative_path, ""

    first_normalized = _normalized_text(directories[0]).strip()
    coarse = _COARSE_CONTAINERS.get(first_normalized)
    if coarse is not None:
        # A specific folder immediately below a coarse SPs/Extras container
        # outranks the container itself.
        if len(directories) > 1:
            nested_category = _component_category(directories[1])
            if nested_category is not None:
                return nested_category, Path(*parts[2:]), directories[0]
        return None, Path(*parts[1:]), directories[0]

    category = _component_category(directories[0])
    if category is not None:
        return category, Path(*parts[1:]), ""
    return None, relative_path, ""


def _sidecar_stem(path: Path) -> str:
    """Return the owning media stem for video, subtitle and checksum sidecars."""

    name = unicodedata.normalize("NFKC", path.name)
    suffix = Path(name).suffix.casefold()
    stem = name[: -len(suffix)] if suffix else name
    if suffix in _CHECKSUM_EXTENSIONS:
        nested_suffix = Path(stem).suffix.casefold()
        if nested_suffix in (
            _VIDEO_EXTENSIONS
            | _SUBTITLE_EXTENSIONS
            | _DISC_SIDECAR_AUDIO_EXTENSIONS
            | _CD_AUDIO_EXTENSIONS
        ):
            stem = stem[: -len(nested_suffix)]
    stem = _SIDECAR_LANGUAGE_RE.sub("", stem)
    return normalized_identity(stem)


def _episode_component(raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    whole_raw, dot, fraction_raw = value.partition(".")
    try:
        whole = int(whole_raw)
    except ValueError:
        return None
    if not fraction_raw and (
        whole in _NON_EPISODE_NUMBERS
        or (len(whole_raw) == 4 and 1900 <= whole <= 2099)
    ):
        return None
    if whole > 9999:
        return None
    component = f"{whole:02d}"
    if dot:
        fraction = fraction_raw.rstrip("0") or "0"
        component += f".{fraction}"
    return component


def _episode_range(start: str, end: str = "") -> tuple[str, str] | None:
    first = _episode_component(start)
    if first is None:
        return None
    last = _episode_component(end) if end else None
    if end and last is None:
        return None
    identity = f"e:{first}" if last is None else f"e:{first}-{last}"
    directory = first if last is None else f"{first}-{last}"
    return identity, directory


def _episode_marker_in_text(value: str) -> tuple[str, str] | None:
    text = unicodedata.normalize("NFKC", value)
    season_match = _SEASON_EPISODE_RE.search(text)
    if season_match is not None:
        episode = _episode_component(season_match.group("episode"))
        if episode is not None:
            season = int(season_match.group("season"))
            return f"s:{season}:e:{episode}", f"S{season:02d}E{episode}"

    for pattern in (_LABELED_EPISODE_RE, _CJK_EPISODE_RE):
        matched = pattern.search(text)
        if matched is not None:
            marker = _episode_range(matched.group("episode"))
            if marker is not None:
                return marker

    for matched in _BRACKET_EPISODE_RE.finditer(text):
        marker = _episode_range(matched.group("start"), matched.group("end") or "")
        if marker is not None:
            return marker

    for matched in _TRAILING_EPISODE_RE.finditer(text):
        prefix = text[: matched.start()].rstrip(" ._-").casefold()
        if re.search(
            r"(?:vol(?:ume)?|disc|cd|sp|ova|oad|ncop|nced|op|ed|pv|cm)$",
            prefix,
            re.IGNORECASE,
        ):
            continue
        marker = _episode_range(matched.group("start"), matched.group("end") or "")
        if marker is not None:
            return marker
    return None


def _disc_episode_marker(path: Path) -> tuple[str, str] | None:
    filename = unicodedata.normalize("NFKC", path.name)
    known_suffixes = (
        _VIDEO_EXTENSIONS
        | _SUBTITLE_EXTENSIONS
        | _DISC_SIDECAR_AUDIO_EXTENSIONS
        | _CD_AUDIO_EXTENSIONS
        | _CHECKSUM_EXTENSIONS
    )
    while Path(filename).suffix.casefold() in known_suffixes:
        suffix = Path(filename).suffix
        filename = filename[: -len(suffix)]
    filename = _SIDECAR_LANGUAGE_RE.sub("", filename)
    marker = _episode_marker_in_text(filename)
    if marker is not None:
        return marker
    for component in reversed(path.parts[:-1]):
        marker = _episode_marker_in_text(component)
        if marker is not None:
            return marker
    return None


def _disc_version_core(path: Path) -> str:
    """Return filename identity after removing episode and version details."""

    text = unicodedata.normalize("NFKC", path.name)
    suffix = Path(text).suffix.casefold()
    if suffix:
        text = text[: -len(suffix)]
    if suffix in _CHECKSUM_EXTENSIONS:
        nested_suffix = Path(text).suffix.casefold()
        if nested_suffix in (
            _VIDEO_EXTENSIONS
            | _SUBTITLE_EXTENSIONS
            | _DISC_SIDECAR_AUDIO_EXTENSIONS
            | _CD_AUDIO_EXTENSIONS
        ):
            text = text[: -len(nested_suffix)]
    text = _SIDECAR_LANGUAGE_RE.sub("", text)
    text = _SEASON_EPISODE_RE.sub(" ", text)
    text = _LABELED_EPISODE_RE.sub(" ", text)
    text = _CJK_EPISODE_RE.sub(" ", text)
    text = _BRACKET_EPISODE_RE.sub(" ", text)
    text = _TRAILING_EPISODE_RE.sub(" ", text)
    text = _DISC_VERSION_DETAIL_RE.sub(" ", text)
    text = _HASH_BRACKET_RE.sub(" ", text)
    return normalized_identity(text)


def _disc_version_cores_compatible(left: str, right: str) -> bool:
    """Return whether two same-episode names share a credible title core."""

    if left == right:
        return True
    if not left or not right:
        # An empty core may pair with another empty core (handled above), but
        # must never bridge two otherwise contradictory title cores.
        return False
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return True
    common_prefix = 0
    for lchar, rchar in zip(left, right):
        if lchar != rchar:
            break
        common_prefix += 1
    return common_prefix >= max(4, (len(shorter) * 3) // 5)


@dataclass(frozen=True)
class ClassificationContext:
    """All route-local evidence exposed to overridable folder filters."""

    relative_path: Path
    route_relative_paths: tuple[Path, ...] = field(default_factory=tuple)
    work_name: str = ""
    press_format: str = ""
    press_group: str = ""
    release_type: str = ""
    source_dir_name: str = ""
    target_dir_name: str = ""

    def __post_init__(self) -> None:
        source = _safe_relative_path(self.relative_path)
        route_paths = tuple(_safe_relative_path(path) for path in self.route_relative_paths)
        if source not in route_paths:
            route_paths = (*route_paths, source)
        route_paths = tuple(sorted(set(route_paths), key=lambda path: _normalized_text(path)))
        object.__setattr__(self, "relative_path", source)
        object.__setattr__(self, "route_relative_paths", route_paths)

    @property
    def suffix(self) -> str:
        return self.relative_path.suffix.casefold()

    @property
    def match_text(self) -> str:
        return _normalized_text(self.relative_path)

    @property
    def structural_category(self) -> str | None:
        return _structural_layout(self.relative_path)[0]

    @property
    def inner_relative_path(self) -> Path:
        return _structural_layout(self.relative_path)[1]

    @property
    def coarse_container(self) -> str:
        return _structural_layout(self.relative_path)[2]

    def siblings(self) -> tuple[Path, ...]:
        parent_key = _normalized_text(self.relative_path.parent)
        return tuple(
            path
            for path in self.route_relative_paths
            if path != self.relative_path and _normalized_text(path.parent) == parent_key
        )

    def has_matching_video(self) -> bool:
        stem = _sidecar_stem(self.relative_path)
        return any(
            sibling.suffix.casefold() in _VIDEO_EXTENSIONS
            and _sidecar_stem(sibling) == stem
            for sibling in self.siblings()
        )

    def has_same_episode_video(self) -> bool:
        """Return whether exactly one sibling video owns this episode marker."""

        marker = _disc_episode_marker(self.relative_path)
        if marker is None:
            return False
        episode_key = marker[0]
        matches = [
            sibling
            for sibling in self.siblings()
            if sibling.suffix.casefold() in _VIDEO_EXTENSIONS
            and (_disc_episode_marker(sibling) or (None,))[0] == episode_key
        ]
        return len(matches) == 1

    def has_disc_image_sibling(self) -> bool:
        stem = _sidecar_stem(self.relative_path)
        return any(
            sibling.suffix.casefold() in (_DISC_IMAGE_EXTENSIONS | {".cue"})
            and _sidecar_stem(sibling) == stem
            for sibling in self.siblings()
        )

    def has_audio_sibling(self) -> bool:
        stem = _sidecar_stem(self.relative_path)
        return any(
            sibling.suffix.casefold() in (_CD_AUDIO_EXTENSIONS | {".cue"})
            and _sidecar_stem(sibling) == stem
            for sibling in self.siblings()
        )


@dataclass(frozen=True)
class FolderFilterResult:
    """A category filter's decision for the path inside its target folder."""

    relative_path: Path
    rule_id: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "relative_path", _safe_relative_path(self.relative_path))
        if not self.rule_id.strip() or not self.reason.strip():
            raise ValueError("FolderFilterResult.rule_id/reason 不能为空")


@dataclass(frozen=True)
class LayoutDecision:
    """An immutable category and category-inner layout decision for one file."""

    relative_path: Path
    classifier_id: str
    rule_id: str
    stage: str
    category: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "relative_path", _safe_relative_path(self.relative_path))
        object.__setattr__(self, "category", _safe_category(self.category))
        for field_name in ("classifier_id", "rule_id", "stage", "reason"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"LayoutDecision.{field_name} 不能为空")


def canonical_resolution_episode_subdirectories(
    items: Iterable[tuple[str, str, Path, LayoutDecision]],
    *,
    directory_stem: str,
    press_format: str,
) -> dict[str, Path]:
    """Recognize existing resolution episode bundles, not arbitrary packaging.

    Only direct ``<press stem>_<episode>/<file>`` layouts containing videos
    and their subtitles qualify. Every file must carry its own matching
    episode marker; a parent folder must never supply the missing evidence.
    This is deliberately separate from deciding when a new bundle is needed.
    Filesystem callers must additionally reject links and non-regular files.
    """

    if not is_resolution_press_format(press_format):
        return {}
    safe_stem = _safe_category(directory_stem)
    grouped: dict[tuple[str, str], list[tuple[str, Path, LayoutDecision]]] = {}
    seen_ids: set[str] = set()
    for raw_file_id, raw_scope_id, raw_path, decision in items:
        file_id = str(raw_file_id).strip()
        scope_id = str(raw_scope_id).strip()
        if not file_id or file_id in seen_ids or not scope_id:
            raise ValueError("已有分集布局的 file_id 必须唯一且来源标识不能为空")
        if not isinstance(decision, LayoutDecision):
            raise TypeError("已有分集布局必须接收 LayoutDecision")
        seen_ids.add(file_id)
        path = _safe_relative_path(raw_path)
        if len(path.parts) > 1:
            grouped.setdefault(
                (scope_id, normalized_value(path.parts[0])), []
            ).append((file_id, path, decision))

    preserved: dict[str, Path] = {}
    for rows in grouped.values():
        markers: set[tuple[str, str]] = set()
        video_count = 0
        for _file_id, path, decision in rows:
            if (
                len(path.parts) != 2
                or decision.category != CATEGORY_DISC
                or path.suffix.casefold() not in (_VIDEO_EXTENSIONS | _SUBTITLE_EXTENSIONS)
            ):
                break
            marker = _disc_episode_marker(Path(path.name))
            if marker is None or normalized_value(path.parts[0]) != normalized_value(
                f"{safe_stem}_{marker[1]}"
            ):
                break
            markers.add(marker)
            video_count += path.suffix.casefold() in _VIDEO_EXTENSIONS
        else:
            if len(markers) == 1 and video_count:
                preserved.update(
                    (file_id, Path(path.parts[0])) for file_id, path, _decision in rows
                )
    return preserved


def disc_version_subdirectories(
    items: Iterable[tuple[str, str, Path, LayoutDecision]],
    *,
    directory_stem: str,
    release_type: str = "",
    press_format: str = "",
) -> dict[str, Path]:
    """Plan one stable episode directory for a multi-file Disc episode bundle.

    Each item is ``(file_id, source_scope_id, source_relative_path, decision)``.
    Opaque source scope IDs prevent a sidecar in one release directory from
    attaching to a same-named video in another release directory.  Episode
    version counting, however, intentionally spans all scopes in this one
    already-validated work/format/group route.  A bundle is eligible when it
    has multiple compatible video versions, or when one video has at least one
    matching Disc sidecar such as an external subtitle, audio track or checksum.
    Resolution-named releases (for example ``1080p``) are the exception: a
    single video plus subtitles is the ordinary release shape and stays flat;
    only actual multiple video versions create an episode directory.
    """

    safe_stem = _safe_category(directory_stem)
    if normalized_value(release_type) in {"movie", "film"}:
        return {}
    resolution_release = is_resolution_press_format(press_format)
    rows: list[tuple[str, str, Path, LayoutDecision]] = []
    seen_ids: set[str] = set()
    for raw_file_id, raw_scope_id, raw_path, decision in items:
        file_id = str(raw_file_id).strip()
        scope_id = str(raw_scope_id).strip()
        if not file_id or file_id in seen_ids:
            raise ValueError("Disc 多版本布局的 file_id 必须非空且唯一")
        if not scope_id:
            raise ValueError("Disc 多版本布局的 source_scope_id 不能为空")
        if not isinstance(decision, LayoutDecision):
            raise TypeError("Disc 多版本布局必须接收 LayoutDecision")
        path = _safe_relative_path(raw_path)
        seen_ids.add(file_id)
        rows.append((file_id, scope_id, path, decision))

    markers_by_file: dict[str, tuple[str, str, str]] = {}
    video_ids_by_owner: dict[tuple[str, str, str], set[str]] = {}
    sidecar_ids_by_video: dict[str, set[str]] = {}
    video_ids_by_episode: dict[str, list[str]] = {}
    entry_by_episode: dict[str, str] = {}
    for file_id, scope_id, path, decision in rows:
        if decision.category != CATEGORY_DISC or path.suffix.casefold() not in _VIDEO_EXTENSIONS:
            continue
        marker = _disc_episode_marker(path)
        if marker is None:
            continue
        episode_key, entry_key = marker
        core = _disc_version_core(path)
        classified_marker = (episode_key, entry_key, core)
        markers_by_file[file_id] = classified_marker
        entry_by_episode.setdefault(episode_key, entry_key)
        video_ids_by_episode.setdefault(episode_key, []).append(file_id)
        owner_key = (
            scope_id,
            _normalized_text(path.parent),
            _sidecar_stem(path),
        )
        video_ids_by_owner.setdefault(owner_key, set()).add(file_id)

    disc_sidecar_extensions = (
        _SUBTITLE_EXTENSIONS
        | _DISC_SIDECAR_AUDIO_EXTENSIONS
        | _CD_AUDIO_EXTENSIONS
        | _CHECKSUM_EXTENSIONS
    )
    for file_id, scope_id, path, decision in rows:
        if (
            decision.category != CATEGORY_DISC
            or path.suffix.casefold() not in disc_sidecar_extensions
        ):
            continue
        owner_key = (
            scope_id,
            _normalized_text(path.parent),
            _sidecar_stem(path),
        )
        for video_id in video_ids_by_owner.get(owner_key, set()):
            sidecar_ids_by_video.setdefault(video_id, set()).add(file_id)

    # The verified Macross Delta pre-air edition uses 0.89 for what becomes
    # episode 01.  Do not generalize this to arbitrary 0.xx specials; alias
    # only exact 0.89 when a core-compatible explicit 01 exists in this route.
    episode_one_ids = tuple(video_ids_by_episode.get("e:01", ()))
    if episode_one_ids:
        episode_one_cores = {
            markers_by_file[file_id][2] for file_id in episode_one_ids
        }
        for episode_key, file_ids in tuple(video_ids_by_episode.items()):
            if episode_key != "e:00.89":
                continue
            for file_id in file_ids:
                marker = markers_by_file[file_id]
                if any(
                    _disc_version_cores_compatible(marker[2], core)
                    for core in episode_one_cores
                ):
                    markers_by_file[file_id] = ("e:01", "01", marker[2])

        video_ids_by_episode = {}
        entry_by_episode = {}
        for file_id, marker in markers_by_file.items():
            video_ids_by_episode.setdefault(marker[0], []).append(file_id)
            entry_by_episode.setdefault(marker[0], marker[1])

    eligible_video_ids: set[str] = set()
    eligible_cores_by_episode: dict[str, set[str]] = {}
    for episode_key, file_ids in video_ids_by_episode.items():
        if len(file_ids) == 1:
            video_id = file_ids[0]
            if resolution_release or not sidecar_ids_by_video.get(video_id):
                continue
            eligible_video_ids.add(video_id)
            eligible_cores_by_episode[episode_key] = {
                markers_by_file[video_id][2]
            }
            continue
        if len(file_ids) < 2:
            continue
        remaining = set(file_ids)
        components: list[set[str]] = []
        while remaining:
            seed = remaining.pop()
            component = {seed}
            frontier = [seed]
            while frontier:
                current = frontier.pop()
                current_core = markers_by_file[current][2]
                compatible = {
                    candidate
                    for candidate in remaining
                    if _disc_version_cores_compatible(
                        current_core,
                        markers_by_file[candidate][2],
                    )
                }
                if not compatible:
                    continue
                remaining.difference_update(compatible)
                component.update(compatible)
                frontier.extend(compatible)
            components.append(component)
        version_components = [component for component in components if len(component) > 1]
        # More than one unrelated multi-version core reusing one episode number
        # is ambiguous because both would require the same target directory.
        if len(version_components) != 1:
            continue
        chosen = version_components[0]
        eligible_video_ids.update(chosen)
        eligible_cores_by_episode[episode_key] = {
            markers_by_file[file_id][2] for file_id in chosen
        }

    if not eligible_video_ids:
        return {}

    planned: dict[str, Path] = {}
    for file_id, scope_id, path, decision in rows:
        if decision.category != CATEGORY_DISC:
            continue
        marker = markers_by_file.get(file_id)
        if path.suffix.casefold() in _VIDEO_EXTENSIONS:
            if file_id not in eligible_video_ids:
                continue
        elif marker is None:
            owner_key = (
                scope_id,
                _normalized_text(path.parent),
                _sidecar_stem(path),
            )
            owner_ids = video_ids_by_owner.get(owner_key, set()) & eligible_video_ids
            owner_markers = {markers_by_file[owner_id] for owner_id in owner_ids}
            if len(owner_markers) == 1:
                marker = next(iter(owner_markers))
        if marker is None:
            direct_marker = _disc_episode_marker(path)
            if direct_marker is not None:
                marker = (*direct_marker, _disc_version_core(path))
        if marker is None:
            continue
        eligible_cores = eligible_cores_by_episode.get(marker[0], set())
        if not eligible_cores or not any(
            _disc_version_cores_compatible(marker[2], core)
            for core in eligible_cores
        ):
            continue
        directory = Path(f"{safe_stem}_{entry_by_episode[marker[0]]}")
        _safe_relative_path(directory)
        planned[file_id] = directory
    return planned


def is_resolution_press_format(value: str) -> bool:
    """Return whether a press format is itself a display resolution."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    normalized = re.sub(r"\s+", "", normalized)
    return bool(_RESOLUTION_PRESS_FORMAT_RE.fullmatch(normalized))


def _checked_decision(source: Path, decision: LayoutDecision) -> LayoutDecision:
    target = _safe_relative_path(decision.relative_path)
    if target.name != source.name:
        raise ValueError(f"布局分类器不得修改文件名：{source.name} -> {target.name}")
    return decision


@runtime_checkable
class PressGroupClassifier(Protocol):
    classifier_id: str

    def classify(self, context: ClassificationContext) -> LayoutDecision:
        ...


FolderFilter = Callable[[ClassificationContext], FolderFilterResult | None]


class CommonClassifier:
    """Shared filter orchestration; every target folder is a virtual method."""

    classifier_id = "common-layout"
    rule_prefix = "common"
    stage = "common"

    def folder_filters(self) -> tuple[tuple[str, FolderFilter], ...]:
        """Return first-match filter order; subclasses may override if needed."""

        return (
            (CATEGORY_FONTS, self.filter_fonts),
            (CATEGORY_CD, self.filter_cd),
            (CATEGORY_MENU, self.filter_menu),
            (CATEGORY_CM, self.filter_cm),
            (CATEGORY_PREVIEW, self.filter_preview),
            (CATEGORY_PV, self.filter_pv),
            (CATEGORY_OP_ED, self.filter_op_ed),
            (CATEGORY_SUBS, self.filter_subs),
            (CATEGORY_MV, self.filter_mv),
            (CATEGORY_LIVE, self.filter_live),
            (CATEGORY_SP, self.filter_sp),
            (CATEGORY_IMAGE, self.filter_image),
            (CATEGORY_DISC, self.filter_disc),
            (CATEGORY_OTHERS, self.filter_others),
        )

    def classify(self, context: ClassificationContext) -> LayoutDecision:
        if not isinstance(context, ClassificationContext):
            context = ClassificationContext(Path(context))
        for category, folder_filter in self.folder_filters():
            result = folder_filter(context)
            if result is None:
                continue
            if not isinstance(result, FolderFilterResult):
                raise TypeError(
                    f"{self.classifier_id}.{folder_filter.__name__} 必须返回 FolderFilterResult 或 None"
                )
            return LayoutDecision(
                relative_path=result.relative_path,
                classifier_id=self.classifier_id,
                rule_id=result.rule_id,
                stage=self.stage,
                category=category,
                reason=result.reason,
            )
        raise ValueError(f"{self.classifier_id}.filter_others 必须为安全路径提供兜底分类")

    def _result(
        self,
        context: ClassificationContext,
        rule: str,
        reason: str,
        *,
        relative_path: Path | None = None,
    ) -> FolderFilterResult:
        return FolderFilterResult(
            relative_path=relative_path or context.inner_relative_path,
            rule_id=f"{self.rule_prefix}-{rule}",
            reason=reason,
        )

    def _existing(
        self,
        context: ClassificationContext,
        category: str,
    ) -> FolderFilterResult | None:
        if context.structural_category != category:
            return None
        slug = normalized_identity(category) or "category"
        return self._result(
            context,
            f"existing-{slug}",
            f"来源已位于明确的 {category} 分类子树，保留分类内层级",
        )

    @staticmethod
    def _blocked_by_existing_category(context: ClassificationContext) -> bool:
        return context.structural_category is not None

    # These methods are intentionally independent virtual hooks. A group/work
    # subclass can add one special case, then call super() for common behavior.
    def filter_disc(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_DISC)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if (
            context.suffix in _VIDEO_EXTENSIONS
            and _COMMENTARY_RE.search(context.match_text)
            and not _COMMENTARY_INTERVIEW_RE.search(context.match_text)
            and _disc_episode_marker(context.relative_path) is not None
        ):
            return self._result(
                context,
                "disc-episode-commentary-video",
                "带明确正片集号的 commentary 视频作为该集版本归入 Disc",
            )
        if context.suffix in _CHECKSUM_EXTENSIONS and context.has_matching_video():
            return self._result(
                context,
                "disc-matching-checksum",
                "与同目录正片同名的校验文件跟随进入 Disc",
            )
        resolution_episode_sidecar = (
            is_resolution_press_format(context.press_format)
            and context.suffix in _SUBTITLE_EXTENSIONS
            and context.has_same_episode_video()
        )
        if context.suffix in (
            _DISC_SIDECAR_AUDIO_EXTENSIONS | _SUBTITLE_EXTENSIONS | _CD_AUDIO_EXTENSIONS
        ) and (context.has_matching_video() or resolution_episode_sidecar):
            return self._result(
                context,
                "disc-matching-sidecar",
                "与同目录正片同名的字幕或音轨跟随进入 Disc",
            )
        if _OTHERS_RE.search(context.match_text):
            return None
        if context.suffix in _VIDEO_EXTENSIONS:
            return self._result(context, "disc-video", "未命中特典规则的普通视频归入正片 Disc")
        if context.suffix in _DISC_SIDECAR_AUDIO_EXTENSIONS:
            return self._result(context, "disc-audio-sidecar", "外挂正片音轨归入 Disc")
        return None

    def filter_cd(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_CD)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if context.suffix in _DISC_IMAGE_EXTENSIONS and context.has_disc_image_sibling():
            return self._result(context, "cd-disc-image", "同名碟片镜像文件组归入 CD")
        if context.suffix == ".cue":
            return self._result(context, "cd-cue", "cue 音轨索引归入 CD")
        if context.suffix == ".log" and context.has_audio_sibling():
            return self._result(context, "cd-rip-log", "与音频同名的抓轨日志归入 CD")
        if _CD_RE.search(context.match_text):
            if context.suffix in (
                _ARCHIVE_EXTENSIONS
                | _CD_AUDIO_EXTENSIONS
                | _IMAGE_EXTENSIONS
                | _VIDEO_EXTENSIONS
                | {".cue", ".log"}
            ):
                return self._result(context, "cd-name", "文件名明确标识 OST/CD/SPCD/音乐内容")
        if context.suffix in _ARCHIVE_EXTENSIONS and _AUDIO_ARCHIVE_RE.search(context.match_text):
            return self._result(context, "cd-audio-archive", "包含 EAC/抓轨格式标记的音频压缩包归入 CD")
        if context.suffix in _CD_AUDIO_EXTENSIONS:
            if context.has_matching_video() or _RELEASE_SIDECAR_RE.search(context.match_text):
                return None
            return self._result(context, "cd-audio", "独立音乐音频归入 CD")
        return None

    def filter_op_ed(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_OP_ED)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if _OP_ED_RE.search(context.match_text):
            return self._result(context, "op-ed-name", "文件名明确标识 OP/ED/NCOP/NCED")
        return None

    def filter_image(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_IMAGE)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if _IMAGE_RE.search(context.match_text) and context.suffix in (_IMAGE_EXTENSIONS | _ARCHIVE_EXTENSIONS):
            return self._result(context, "image-name", "文件名明确标识扫描图、Booklet、封面或画集")
        if context.suffix in _IMAGE_EXTENSIONS:
            return self._result(context, "image-extension", "未处于 CD/Menu 子树的独立图片归入 Image")
        return None

    def filter_menu(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_MENU)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if _MENU_RE.search(context.match_text):
            return self._result(context, "menu-name", "文件名明确标识 BD/DVD Menu")
        return None

    def filter_cm(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_CM)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if _CM_RE.search(context.match_text):
            return self._result(context, "cm-name", "文件名明确标识 CM/TVCM/Commercial")
        return None

    def filter_pv(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_PV)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if _PV_RE.search(context.match_text):
            return self._result(context, "pv-name", "文件名明确标识 PV/Promotion Video")
        return None

    def filter_preview(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_PREVIEW)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if _PREVIEW_RE.search(context.match_text):
            return self._result(context, "preview-name", "文件名明确标识 Preview/Trailer/予告")
        return None

    def filter_sp(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_SP)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        release_type = normalized_value(context.release_type)
        ova_or_oad_extra = (
            release_type not in {"ova", "oad"}
            and _OVA_OAD_RE.search(context.match_text) is not None
        )
        if (
            _SP_RE.search(context.match_text)
            or ova_or_oad_extra
            or normalized_value(context.coarse_container) == "sps"
        ):
            return self._result(context, "sp-name", "明确的 SP/Special/Bonus Video 归入 SP")
        return None

    def filter_fonts(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_FONTS)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if context.suffix in _FONT_EXTENSIONS or _FONT_RE.search(context.match_text):
            return self._result(context, "fonts-name-or-extension", "字体文件或明确的 Fonts 字体包归入 Fonts")
        return None

    def filter_subs(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_SUBS)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        resolution_episode_sidecar = (
            is_resolution_press_format(context.press_format)
            and context.suffix in _SUBTITLE_EXTENSIONS
            and context.has_same_episode_video()
        )
        if _SUBS_RE.search(context.match_text) and not resolution_episode_sidecar:
            return self._result(context, "subs-name", "文件名明确标识独立字幕包")
        if context.suffix in _SUBTITLE_EXTENSIONS:
            if context.suffix == ".sub" and context.has_disc_image_sibling():
                return None
            if context.has_matching_video() or resolution_episode_sidecar:
                return None
            return self._result(context, "subs-standalone", "没有同名正片的独立字幕文件归入 Subs")
        return None

    def filter_mv(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_MV)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if _MV_RE.search(context.match_text):
            return self._result(context, "mv-name", "文件名明确标识 MV/Music Video")
        return None

    def filter_live(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_LIVE)
        if existing is not None or self._blocked_by_existing_category(context):
            return existing
        if _LIVE_RE.search(context.match_text):
            return self._result(context, "live-name", "文件名明确标识演唱会或现场活动")
        return None

    def filter_others(self, context: ClassificationContext) -> FolderFilterResult | None:
        existing = self._existing(context, CATEGORY_OTHERS)
        if existing is not None:
            return existing
        return self._result(context, "others-fallback", "所有专属文件夹 Filter 均未命中，归入 Others")


class GenericClassifier(CommonClassifier):
    """Common rules used when no dedicated press-group subclass exists."""

    classifier_id = "generic-layout"
    rule_prefix = "generic"
    stage = "fallback"


class JsumClassifier(GenericClassifier):
    classifier_id = "jsum-layout"
    rule_prefix = "jsum"
    stage = "press-group"

    def filter_cd(self, context: ClassificationContext) -> FolderFilterResult | None:
        if context.structural_category is None and context.suffix in _ARCHIVE_EXTENSIONS:
            if re.search(r"(?<![0-9a-z])(?:spcd\d*|cd\d+|ost)(?![0-9a-z])", context.match_text, re.IGNORECASE):
                return self._result(context, "cd-archive", "JSUM 的 CD/SPCD/OST 压缩包归入 CD")
        return super().filter_cd(context)

    def filter_others(self, context: ClassificationContext) -> FolderFilterResult | None:
        if context.structural_category is None and re.search(
            r"(?<![0-9a-z])(?:logo|advice|bansen)(?![0-9a-z])",
            context.match_text,
            re.IGNORECASE,
        ):
            return self._result(context, "others-extra-name", "JSUM 的 Logo/Advice/Bansen 归入 Others")
        return super().filter_others(context)


class VcbClassifier(GenericClassifier):
    """Common VCB behavior shared by every real group code beginning VCB."""

    classifier_id = "vcb-layout"
    rule_prefix = "vcb"
    stage = "press-group"

    def filter_op_ed(self, context: ClassificationContext) -> FolderFilterResult | None:
        if context.structural_category is None and _NC_VERSION_RE.search(context.match_text):
            return self._result(context, "op-ed-nc-version", "VCB 的 NC Ver. 视频归入 OP+ED")
        return super().filter_op_ed(context)

    def filter_others(self, context: ClassificationContext) -> FolderFilterResult | None:
        if context.structural_category is None and re.search(
            r"(?<![0-9a-z])(?:logo|advice|notice|iv\d+|info)(?![0-9a-z])|[\[(]iv[\])]",
            context.match_text,
            re.IGNORECASE,
        ):
            return self._result(context, "others-extra-name", "VCB 的 Logo/Advice/Notice/IV/Info 归入 Others")
        return super().filter_others(context)


class VcbmClassifier(VcbClassifier):
    """Compatibility name; VCBM now uses the same VCB-family implementation."""

    classifier_id = "vcb-layout"


class PreserveLayoutClassifier(GenericClassifier):
    """Compatibility shim; generic categorization replaces preserve-layout."""

    def __init__(self, classifier_id: str = "generic-layout") -> None:
        self.classifier_id = classifier_id


class FallbackClassifier(GenericClassifier):
    classifier_id = "fallback-layout"
    rule_prefix = "generic"
    stage = "fallback"


@dataclass(frozen=True)
class ClassifierRegistry:
    by_group: Mapping[str, PressGroupClassifier]
    fallback: PressGroupClassifier
    by_family_prefix: Mapping[str, PressGroupClassifier] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized: dict[str, PressGroupClassifier] = {}
        for raw_group, classifier in self.by_group.items():
            group = normalized_value(raw_group)
            if not group:
                raise ValueError("分类器注册的压制组不能为空")
            if group in normalized:
                raise ValueError(f"压制组分类器重复注册：{raw_group}")
            if not isinstance(classifier, PressGroupClassifier):
                raise TypeError(f"压制组 {raw_group} 的分类器不符合 PressGroupClassifier")
            normalized[group] = classifier

        family: dict[str, PressGroupClassifier] = {}
        for raw_prefix, classifier in self.by_family_prefix.items():
            prefix = normalized_identity(raw_prefix)
            if not prefix:
                raise ValueError("分类器 family prefix 不能为空")
            if prefix in family:
                raise ValueError(f"分类器 family prefix 重复注册：{raw_prefix}")
            if not isinstance(classifier, PressGroupClassifier):
                raise TypeError(f"family {raw_prefix} 的分类器不符合 PressGroupClassifier")
            family[prefix] = classifier

        if not isinstance(self.fallback, PressGroupClassifier):
            raise TypeError("fallback 不符合 PressGroupClassifier")
        object.__setattr__(self, "by_group", MappingProxyType(normalized))
        object.__setattr__(self, "by_family_prefix", MappingProxyType(family))

    def classifier_for(self, press_group: str) -> PressGroupClassifier | None:
        if not normalize_press_group(press_group):
            return None
        exact = self.by_group.get(normalized_value(press_group))
        if exact is not None:
            return exact
        # The historic abbreviation does not always start with its release
        # group (VA/VAF/VAL/VTL/VCDM all contain VCB as a member).  Resolve
        # the normalized registry family before retaining prefix matching as
        # a compatibility fallback for local/custom group codes.
        try:
            registered_family = normalized_identity(
                media_group_classifier_family(press_group)
            )
        except (OSError, ValueError):
            registered_family = ""
        if registered_family:
            registered = self.by_family_prefix.get(registered_family)
            if registered is not None:
                return registered
        identity = normalized_identity(press_group)
        matches = [
            (prefix, classifier)
            for prefix, classifier in self.by_family_prefix.items()
            if identity.startswith(prefix)
        ]
        if not matches:
            return None
        longest = max(len(prefix) for prefix, _ in matches)
        strongest = {id(classifier): classifier for prefix, classifier in matches if len(prefix) == longest}
        if len(strongest) != 1:
            raise ValueError(f"压制组 {press_group} 同时命中多个 family 分类器")
        return next(iter(strongest.values()))

    def classify(
        self,
        context_or_group: ClassificationContext | str,
        relative_path: str | Path | None = None,
        **context_values: object,
    ) -> LayoutDecision:
        if isinstance(context_or_group, ClassificationContext):
            if relative_path is not None or context_values:
                raise TypeError("传入 ClassificationContext 时不能再提供 relative_path/context 参数")
            context = context_or_group
        else:
            if relative_path is None:
                raise TypeError("按 press_group 分类时必须提供 relative_path")
            context = ClassificationContext(
                relative_path=Path(relative_path),
                press_group=str(context_or_group),
                **context_values,
            )
        classifier = self.classifier_for(context.press_group) or self.fallback
        decision = classifier.classify(context)
        return _checked_decision(context.relative_path, decision)


_JSUM_CLASSIFIER = JsumClassifier()
_VCB_CLASSIFIER = VcbClassifier()
_FALLBACK_CLASSIFIER = FallbackClassifier()

DEFAULT_CLASSIFIER_REGISTRY = ClassifierRegistry(
    by_group={"JSUM": _JSUM_CLASSIFIER},
    by_family_prefix={"VCB": _VCB_CLASSIFIER},
    fallback=_FALLBACK_CLASSIFIER,
)


__all__ = [
    "CATEGORY_CD",
    "CATEGORY_CM",
    "CATEGORY_DISC",
    "CATEGORY_FONTS",
    "CATEGORY_IMAGE",
    "CATEGORY_LIVE",
    "CATEGORY_MENU",
    "CATEGORY_MV",
    "CATEGORY_OP_ED",
    "CATEGORY_OTHERS",
    "CATEGORY_PREVIEW",
    "CATEGORY_PV",
    "CATEGORY_SP",
    "CATEGORY_SUBS",
    "ClassificationContext",
    "ClassifierRegistry",
    "CommonClassifier",
    "DEFAULT_CLASSIFIER_REGISTRY",
    "FallbackClassifier",
    "FolderFilterResult",
    "GenericClassifier",
    "JsumClassifier",
    "LAYOUT_CATEGORIES",
    "LayoutDecision",
    "PreserveLayoutClassifier",
    "PressGroupClassifier",
    "VcbClassifier",
    "VcbmClassifier",
    "canonical_resolution_episode_subdirectories",
    "disc_version_subdirectories",
    "is_resolution_press_format",
    "is_vcb_family_group",
]
