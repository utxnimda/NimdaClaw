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
from typing import Callable, Mapping, Protocol, runtime_checkable

from media_directory_organizer.catalog import normalized_identity, normalized_value
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
    r"character[ ._-]*song|drama[ ._-]*cd|spcd\d*|cd\d+)(?![0-9a-z])|"
    r"オリジナルサウンドトラック|サウンドトラック|キャラクターソング|ドラマ[ ._-]*cd",
    re.IGNORECASE,
)
_AUDIO_ARCHIVE_RE = re.compile(
    r"\[eac\]|\([^)]*(?<![0-9a-z])(?:flac|wav|ape|tak|tta|cue)(?![0-9a-z])[^)]*\)",
    re.IGNORECASE,
)
_MENU_RE = re.compile(r"(?<![0-9a-z])(?:bd[ ._-]*)?menu\d*(?![0-9a-z])|メニュー", re.IGNORECASE)
_CM_RE = re.compile(
    r"(?<![0-9a-z])(?:tv[ ._-]*cm|cm\d*|commercials?)(?![0-9a-z])",
    re.IGNORECASE,
)
_PREVIEW_RE = re.compile(
    r"(?<![0-9a-z])(?:preview|trailer|teaser|yokoku)(?![0-9a-z])|予告",
    re.IGNORECASE,
)
_PV_RE = re.compile(
    r"(?<![0-9a-z])(?:pv\d*|promotion(?:al)?[ ._-]*(?:video|movie))(?![0-9a-z])",
    re.IGNORECASE,
)
_OP_ED_RE = re.compile(
    r"(?<![0-9a-z])(?:ncop\d*|nced\d*|op\d*|ed\d*|"
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
    r"(?<![0-9a-z])(?:sp\d+|specials?\d*|bonus[ ._-]*video|mini[ ._-]*ova|oad|tokuten)(?![0-9a-z])|特典",
    re.IGNORECASE,
)
_IMAGE_RE = re.compile(
    r"(?<![0-9a-z])(?:booklets?|scans?|jackets?|covers?|artbooks?|bd[ ._-]*scans?)(?![0-9a-z])",
    re.IGNORECASE,
)
_OTHERS_RE = re.compile(
    r"(?<![0-9a-z])(?:logo|advice|notice|bansen|interview|iv\d*|info|"
    r"making|commentary|readme|torrent|checksum)(?![0-9a-z])",
    re.IGNORECASE,
)
_RELEASE_SIDECAR_RE = re.compile(
    r"(?<![0-9a-z])(?:x26[45]|ma10p|hi10p|1080p|2160p)(?![0-9a-z])",
    re.IGNORECASE,
)
_SIDECAR_LANGUAGE_RE = re.compile(
    r"(?:\.(?:sc|tc|chs|cht|jpn?|ja|eng?|commentary))+$",
    re.IGNORECASE,
)


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
    stem = _SIDECAR_LANGUAGE_RE.sub("", path.stem)
    return normalized_identity(stem)


@dataclass(frozen=True)
class ClassificationContext:
    """All route-local evidence exposed to overridable folder filters."""

    relative_path: Path
    route_relative_paths: tuple[Path, ...] = field(default_factory=tuple)
    work_name: str = ""
    press_format: str = ""
    press_group: str = ""
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
        if _OTHERS_RE.search(context.match_text):
            return None
        if context.suffix in _VIDEO_EXTENSIONS:
            return self._result(context, "disc-video", "未命中特典规则的普通视频归入正片 Disc")
        if context.suffix in _DISC_SIDECAR_AUDIO_EXTENSIONS:
            return self._result(context, "disc-audio-sidecar", "外挂正片音轨归入 Disc")
        if context.suffix in (_SUBTITLE_EXTENSIONS | _CD_AUDIO_EXTENSIONS) and context.has_matching_video():
            return self._result(context, "disc-matching-sidecar", "与同目录正片同名的字幕或音轨跟随进入 Disc")
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
            if context.suffix in (_ARCHIVE_EXTENSIONS | _CD_AUDIO_EXTENSIONS | _IMAGE_EXTENSIONS | {".cue", ".log"}):
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
        if _SP_RE.search(context.match_text) or normalized_value(context.coarse_container) == "sps":
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
        if _SUBS_RE.search(context.match_text):
            return self._result(context, "subs-name", "文件名明确标识独立字幕包")
        if context.suffix in _SUBTITLE_EXTENSIONS:
            if context.suffix == ".sub" and context.has_disc_image_sibling():
                return None
            if context.has_matching_video():
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
            r"(?<![0-9a-z])(?:logo|advice|notice|iv\d*|info)(?![0-9a-z])",
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
    "is_vcb_family_group",
]
