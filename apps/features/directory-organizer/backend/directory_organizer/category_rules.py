"""Pure, extensible resource classifiers used by the directory organizer.

A directory is one resource, not a bag of independently classified extensions.
The caller owns traversal, inheritance, naming, conflict checks and execution.
This module neither imports the layout strategies nor accesses the filesystem.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable


VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".m2ts", ".mts", ".ts", ".wmv", ".mov", ".mpg", ".mpeg", ".vob", ".iso", ".webm"})
SUBTITLE_EXTENSIONS = frozenset({".ass", ".ssa", ".srt", ".sub", ".idx", ".sup", ".vtt", ".smi"})
AUDIO_EXTENSIONS = frozenset({".flac", ".wav", ".ape", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wv", ".tta", ".alac", ".cue", ".mka", ".ac3", ".eac3", ".dts", ".thd", ".truehd"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".avif", ".heic", ".pdf"})
FONT_EXTENSIONS = frozenset({".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".fon"})
ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst"})

_BAD_CATEGORY = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_CATEGORY = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:[.]|$)")
_GROUP = re.compile(r"(?i)^(?:vcb(?:[- _]?studio|[a-z]{1,2})?|jsum|mawen1250|(?:mawen1250\s*[&+×]\s*)?vcb[- _]?studio|vcb[- _]?studio\s*[&+×]\s*mawen1250)$")
_GROUP_BRACKETS = re.compile(r"\[([^\[\]]+)\]|【([^【】]+)】|\(([^()]*)\)")
_ALBUM = re.compile(r"(?i)(?:^|[^a-z])(?:album|ost|soundtrack)(?:[^a-z]|$)|原[声聲]|ドラマcd|キャラクターソング")
_AUDIO_PACKAGE = re.compile(r"(?i)^\[(?:eac|hi[- ]?res|flac|ape)\]")
_SUBTITLE_HINT = re.compile(r"(?i)(?:^|[^a-z])(?:subs?|subtitles?|ass|ssa|srt|sup)(?:[^a-z]|$)|字幕")
_FONT_HINT = re.compile(r"(?i)(?:^|[^a-z])fonts?(?:[^a-z]|$)|字体|字體")
_MENU = re.compile(r"(?i)(?:^|[^a-z])menu(?:\d*|s)(?:[^a-z]|$)|メニュー|菜[单單]|選單")
_CM = re.compile(r"(?i)(?:^|[^a-z])cm\d*(?:[^a-z]|$)|コマーシャル")
_OP_ED = re.compile(r"(?i)(?:^|[^a-z])(?:nc[ _-]?(?:op|ed)\d*|(?:op|ed)\d{0,2}|opening|ending|creditless)(?:[^a-z]|$)|ノン(?:クレジット|テロップ)|オープニング|エンディング|片[头頭尾]|[开開][场場][动動][画畫]|[结結]束[动動][画畫]")
_BONUS = re.compile(r"(?i)(?:^|[^a-z])(?:pv\d*|cm\d*|sp\d*|(?:op|ed)?mv\d*|trailer|teaser|commercial|preview|interview|specialtalk|specials?|extras?|bonus(?:es)?)(?:[^a-z]|$)|映像特典|予告|特報")
_LOGO_LABEL = re.compile(r"(?i)(?:producer[ _-]+)?logo")
_EPISODE_LABEL = re.compile(r"(?i)(?:ep?(?:isode)?[ ._-]*)?\d{1,3}(?:v\d+)?")


@dataclass(frozen=True)
class ResourceContext:
    relative_path: str
    is_directory: bool = False
    inside_publisher: bool = False

    @property
    def name(self) -> str:
        return self.relative_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        name = self.name
        return "" if self.is_directory or "." not in name else "." + name.rsplit(".", 1)[-1].casefold()

    @property
    def extension(self) -> str:
        return self.ext


@dataclass(frozen=True)
class CategoryMatch:
    category: str
    reason: str
    keep_directory: bool = True
    protect_contents: bool = False


CategoryFilter = Callable[[ResourceContext], CategoryMatch | None]


def _clean_name(name: str) -> str:
    def remove_group(match):
        value = next(value for value in match.groups() if value is not None).strip()
        return "" if _GROUP.fullmatch(value) else match[0]
    return _GROUP_BRACKETS.sub(remove_group, name).strip()


def _is_logo_resource(name: str) -> bool:
    stem = _clean_name(name.rsplit(".", 1)[0])
    tags = list(_GROUP_BRACKETS.finditer(stem))
    values = [next(value for value in tag.groups() if value is not None).strip() for tag in tags]
    for index, (tag, value) in enumerate(zip(tags, values)):
        if not _LOGO_LABEL.fullmatch(value):
            continue
        # A leading [Logo] followed by an episode tag can be the work title;
        # [Work][Logo] and [Work][Producer Logo] are resource-type labels.
        if not stem[:tag.start()].strip() and any(_EPISODE_LABEL.fullmatch(value) for value in values[index + 1:]):
            continue
        return True
    return bool(_LOGO_LABEL.fullmatch(stem))


def _directory_match(context: ResourceContext, category: str, aliases: set[str], *, protect: bool = False,
                     consume_aliases: frozenset[str] = frozenset()) -> CategoryMatch | None:
    if not context.is_directory:
        return None
    name = _clean_name(context.name)
    normalized = name.casefold().replace(" ", "")
    bare = normalized in aliases
    underscored = normalized.strip("_") in aliases
    prefixed = "_" in normalized and normalized.rsplit("_", 1)[-1] in aliases
    if not (bare or underscored or prefixed):
        return None
    # A bare folder inside SPs is an asset unit (Menu/01.mkv), not a formal
    # category root. Explicit wrappers and old Others roots are consumed.
    keep = context.inside_publisher and bare and normalized not in consume_aliases
    return CategoryMatch(category, "按明确目录类别识别为 " + category, keep_directory=keep, protect_contents=protect)


class CommonCategoryClassifier:
    """First-match registry. Override a filter method or register another one.

    Callbacks receive only ResourceContext and return CategoryMatch or None.
    A replacement preserves its position; new categories are inserted before
    ``before`` (Others by default). Others always remains the final fallback.
    """

    def __init__(self):
        self._category_filters: list[tuple[str, CategoryFilter]] = [
            ("Music", self.filter_music), ("Booklet", self.filter_booklet),
            ("CD", self.filter_cd), ("Subs", self.filter_subs), ("Fonts", self.filter_fonts),
            ("Menu", self.filter_menu), ("CM", self.filter_cm), ("OP+ED", self.filter_op_ed),
            ("Image", self.filter_image), ("Disc", self.filter_disc), ("Others", self.filter_others),
        ]

    @property
    def category_names(self) -> tuple[str, ...]:
        return tuple(category for category, _ in self._category_filters)

    def register_category(self, category: str, callback: CategoryFilter, before: str = "Others") -> None:
        if not isinstance(category, str) or not category or category != category.strip() or len(category) > 255 or category in {".", ".."} or category.endswith(".") or _BAD_CATEGORY.search(category) or _RESERVED_CATEGORY.match(category):
            raise ValueError("分类名必须是安全的 Windows 单层目录名")
        if not callable(callback):
            raise TypeError("分类规则必须是可调用函数")
        existing = next((index for index, (name, _) in enumerate(self._category_filters) if name.casefold() == category.casefold()), None)
        if existing is not None:
            # Retain the canonical name, avoiding two directories that differ
            # only in case on Windows. In particular Others stays last.
            self._category_filters[existing] = (self._category_filters[existing][0], callback)
            return
        if not isinstance(before, str):
            raise ValueError("before 必须指定已注册类别")
        position = next((index for index, (name, _) in enumerate(self._category_filters) if name.casefold() == before.casefold()), None)
        if position is None:
            raise ValueError("before 指定的分类尚未注册：" + before)
        self._category_filters.insert(position, (category, callback))

    def classify_resource(self, context: ResourceContext) -> CategoryMatch:
        if not isinstance(context, ResourceContext) or not isinstance(context.relative_path, str) or not isinstance(context.is_directory, bool) or not isinstance(context.inside_publisher, bool):
            raise TypeError("分类需要有效的 ResourceContext")
        rules = self._category_filters
        explicit_category = None
        if context.is_directory:
            normalized = _clean_name(context.name).casefold()
            roots = [rule for rule in rules if normalized.endswith("_" + rule[0].casefold())]
            if roots:
                # A formal root is authoritative even if its work title has
                # words such as OST/Album. Longest suffix wins for Extra and
                # Foo_Extra registered together; still use that rule's hook.
                selected = max(roots, key=lambda rule: len(rule[0]))
                rules = [selected]
                explicit_category = selected[0]
        for category, callback in rules:
            match = callback(context)
            if match is None and category == explicit_category:
                # Registered rules may only classify raw files. Their explicit
                # generated category roots must remain stable on a later pass.
                # Bare folder names do not opt in to an extension implicitly.
                match = CategoryMatch(category, "识别已注册分类的规范目录根", keep_directory=False)
            if match is None:
                continue
            if not isinstance(match, CategoryMatch):
                raise TypeError("分类规则必须返回 CategoryMatch 或 None：" + category)
            if match.category != category:
                raise ValueError("分类规则返回的类别与登记类别不一致：" + category)
            if not isinstance(match.reason, str) or not isinstance(match.keep_directory, bool) or not isinstance(match.protect_contents, bool):
                raise TypeError("分类匹配的说明与目录标志类型无效：" + category)
            return match
        # Even an overridden Others filter returning None cannot discard a
        # resource. This default uses no subclass callback and never recurses.
        return CommonCategoryClassifier.filter_others(self, context)

    def filter_music(self, context: ResourceContext) -> CategoryMatch | None:
        return _directory_match(context, "Music", {"music"}, protect=True)

    def filter_booklet(self, context: ResourceContext) -> CategoryMatch | None:
        return _directory_match(context, "Booklet", {"booklet", "booklets", "visualfanbook"}, protect=True)

    def filter_cd(self, context: ResourceContext) -> CategoryMatch | None:
        match = _directory_match(context, "CD", {"cd", "cds", "audio", "ost", "soundtrack", "音楽", "音乐", "音樂", "音频", "音頻"}, consume_aliases=frozenset({"cds"}))
        if match:
            return match
        if context.is_directory:
            if _AUDIO_PACKAGE.match(context.name) or _ALBUM.search(context.name):
                return CategoryMatch("CD", "识别音乐专辑目录，整体保留音轨、封面与附属资源")
        elif context.ext in AUDIO_EXTENSIONS:
            return CategoryMatch("CD", "音频或音轨索引文件；明确配套正片的外置音轨由关联阶段处理")
        return None

    def filter_subs(self, context: ResourceContext) -> CategoryMatch | None:
        match = _directory_match(context, "Subs", {"subs", "sub", "subtitle", "subtitles", "字幕"})
        if match:
            return match
        if not context.is_directory and context.ext in ARCHIVE_EXTENSIONS and _SUBTITLE_HINT.search(context.name):
            return CategoryMatch("Subs", "明确字幕压缩包，优先于同时提到的字体，不解压")
        return None

    def filter_fonts(self, context: ResourceContext) -> CategoryMatch | None:
        match = _directory_match(context, "Fonts", {"font", "fonts", "字体", "字體"})
        if match:
            return match
        if not context.is_directory and (context.ext in FONT_EXTENSIONS or context.ext in ARCHIVE_EXTENSIONS and _FONT_HINT.search(context.name)):
            return CategoryMatch("Fonts", "字体文件或明确字体压缩包")
        return None

    def filter_menu(self, context: ResourceContext) -> CategoryMatch | None:
        match = _directory_match(context, "Menu", {"menu", "menus", "メニュー", "菜单", "菜單", "選單"})
        if match:
            return match
        if not context.is_directory and _MENU.search(context.name):
            return CategoryMatch("Menu", "文件名包含明确的 Menu / 菜单标记")
        return None

    def filter_cm(self, context: ResourceContext) -> CategoryMatch | None:
        match = _directory_match(context, "CM", {"cm", "cms", "コマーシャル"})
        if match:
            return match
        if not context.is_directory and context.ext in VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS and _CM.search(context.name):
            return CategoryMatch("CM", "广告视频或配套字幕包含明确 CM 标记")
        return None

    def filter_op_ed(self, context: ResourceContext) -> CategoryMatch | None:
        match = _directory_match(context, "OP+ED", {"op+ed", "ncop+nced", "ncop&nced", "ncoped", "opening&ending", "ncop", "nced", "ncops", "nceds", "op", "ed", "opening", "ending"})
        if match:
            return match
        if not context.is_directory and context.ext in VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS and _OP_ED.search(context.name):
            return CategoryMatch("OP+ED", "开场 / 结束动画或配套字幕")
        return None

    def filter_image(self, context: ResourceContext) -> CategoryMatch | None:
        wrappers = frozenset({"scan", "scans", "picture", "pictures", "图片", "扫图", "掃圖"})
        match = _directory_match(context, "Image", {"image", "images", "artwork", *wrappers}, consume_aliases=wrappers)
        if match:
            return match
        if not context.is_directory and context.ext in IMAGE_EXTENSIONS:
            return CategoryMatch("Image", "图片或扫描文档")
        return None

    def filter_disc(self, context: ResourceContext) -> CategoryMatch | None:
        match = _directory_match(context, "Disc", {"disc", "discs"})
        if match:
            return match
        if not context.is_directory and context.ext in VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS and not _BONUS.search(context.name) and not _is_logo_resource(context.name):
            return CategoryMatch("Disc", "正片视频或字幕")
        return None

    def filter_others(self, context: ResourceContext) -> CategoryMatch:
        match = _directory_match(context, "Others", {"other", "others"}, consume_aliases=frozenset({"other", "others"}))
        if match:
            return match
        return CategoryMatch("Others", "未匹配已注册类别，文件或整个独立目录归入 Others")
