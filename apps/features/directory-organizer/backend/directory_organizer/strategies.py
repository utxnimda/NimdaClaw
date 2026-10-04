"""Pure, extensible release-layout suggestions; no disk or database access.

Strategies describe a draft, never execute it.  The application service owns
record matching, confirmation, persistence and every filesystem operation.
Override ``filter_directory``, ``classify_file``, ``publisher_prefix_length``,
``category_directory`` or ``target_for_file`` for a group-specific convention
without duplicating that execution workflow.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Mapping, Sequence

from .category_rules import AUDIO_EXTENSIONS, SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS, CommonCategoryClassifier, ResourceContext


# Only these raw, top-level publisher containers are replaced by category
# roots.  A nested directory is an existing resource unit, not another wrapper
# to discard; formal categories such as CD or _Menu are deliberately absent.
# SP and SPs are the singular/plural names of the same mixed extras wrapper.
PUBLISHER_CONTAINERS = frozenset({"cds", "scan", "scans", "picture", "pictures", "图片", "扫图", "掃圖", "sp", "sps", "specials", "extras", "bonus", "bonuses"})

_BRACKETS = re.compile(r"\[([^\[\]]+)\]|【([^【】]+)】")
_VCB = re.compile(r"(?i)(?<![a-z0-9])(?:vcb(?:[- _]?studio|[a-z]{1,2})?)(?![a-z0-9])")
_JSUM = re.compile(r"(?i)(?<![a-z0-9])jsum(?![a-z0-9])")
_KNOWN_GROUP = re.compile(r"(?i)^(?:vcb(?:[- _]?studio|[a-z]{1,2})?|jsum|mawen1250|(?:mawen1250\s*[&+×]\s*)?vcb[- _]?studio|vcb[- _]?studio\s*[&+×]\s*mawen1250)$")
_FORMAT = re.compile(r"(?i)(?<![a-z0-9])(?P<format>bd[ _-]?rip|dvd[ _-]?rip|web[ _-]?dl|web[ _-]?rip|blu[ _-]?ray|bdremux|remux|2160p|1080p|720p|480p|bd|dvd)(?![a-z0-9])")
_RELEASE_SUFFIX = re.compile(r"(?i)^(?P<title>.+?)_(?P<format>BDRip|DVDRip|WEB-DL|WEBRip|BDREMUX|REMUX|2160p|1080p|720p|480p)(?:\((?P<group>[^()]*)\))?$")
_DATE_OR_TRACKS = re.compile(r"(?i)^(?:\d{4}(?:[-~–]\d{2,4})?|\d{8}|\d+(?:[-~–]\d+)?(?:fin|end|complete)?(?:\s*\+\s*(?:sp|ova|oad|ncop|nced))*|(?:tv|ova|oad|sp|movie))$")
_TECHNICAL = re.compile(r"(?i)\b(?:bd(?:rip|remux)?|dvd(?:rip)?|blu[- _]?ray|web[- _]?(?:dl|rip)|remux|\d{3,4}[pi]|\d{3,4}[x×]\d{3,4}|avc|hevc|x26[45]|h[.]?26[45]|av1|flac|aac|dts(?:-hd)?|truehd|ac3|pcm|hi10p|ma(?:8|10|12)p|(?:8|10|12)bit)\b")
_OP_ED = re.compile(r"(?i)(?:^|[^a-z])(?:nc[ _-]?(?:op|ed)|(?:op|ed)\d{0,2}|opening|ending|creditless)(?:[^a-z]|$)|ノン(?:クレジット|テロップ)|オープニング|エンディング|片[头頭尾]|[开開][场場][动動][画畫]|[结結]束[动動][画畫]")
_MUSIC_HINT = re.compile(r"(?i)(?:^|[^a-z])(?:ost|album|soundtrack|song|songs|track|music|single|opening|ending|ncop|nced)(?:[^a-z]|$)|原声|原聲|主題歌|主题曲|挿入歌|キャラクターソング|ドラマcd")
_COMPANION_TAGS = re.compile(r"(?i)(?:^|(?<=[\s_.\[(-]))(?:jpn|jp|japanese|eng|english|chs|cht|sc|tc|chi|audio|commentary|external|dual|multi|flac|aac|ac3|eac3|dts|truehd|pcm|\d[.]\d(?:ch)?)(?=$|[\s_.\])+-])|外置音轨|外置音軌|音轨|音軌|日语|日語|国语|國語")
_SEASON_EPISODE = re.compile(r"(?i)(?:^|[^a-z0-9])s(?P<s>\d{1,2})[ ._-]*e(?P<e>\d{1,3})(?!\d)")
_EPISODE_PATTERNS = (
    re.compile(r"(?i)(?:^|[^a-z0-9])(?:ep?|episode)[ ._-]*(\d{1,3})(?:v\d+)?(?!\d)"),
    re.compile(r"[\[【](\d{1,3})(?:[vV]\d+)?[\]】]"),
    re.compile(r"第\s*(\d{1,3})\s*[話话集]"),
    re.compile(r"(?i)(?:^|\s)-\s*(\d{1,3})(?:v\d+)?(?=[ _.\[(-]|$)"),
    re.compile(r"(?i)^(\d{1,3})(?:v\d+)?\s*-\s+"),
    re.compile(r"(?i)(?:^|[ _.-])(\d{1,3})(?:v\d+)?(?:[ ._-]*(?:chs|cht|sc|tc|jpn|eng|简体|繁体|簡體|繁體|简|繁))*\s*$"),
)
_WINDOWS_RESERVED = re.compile(r"(?i)^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:[.]|$)")
_INVALID_COMPONENT = re.compile(r'[<>:"|?*\x00-\x1f]')
_SAFE_NAME_TRANSLATION = str.maketrans({'/': '／', '\\': '＼', ':': '：', '*': '＊', '?': '？', '"': '＂', '<': '＜', '>': '＞', '|': '｜'})

@dataclass(frozen=True)
class DirectoryRule:
    category: str
    prefix_length: int = 0
    reason: str = "按所在目录的资源类型分类"
    preserve: bool = False
    existing_root: str = ""
    resource_type: str = ""


@dataclass(frozen=True)
class FileClassification:
    category: str
    reason: str
    prefix_length: int = 0
    preserve: bool = False
    existing_root: str = ""
    resource_type: str = ""
    companion_video: str = ""
    companion_videos: tuple[str, ...] = ()
    directory_owned: bool = False


def normalize_relative_path(value: Any) -> str:
    """Validate a Windows-safe relative file path without resolving a disk path."""
    if not isinstance(value, str) or not value:
        raise ValueError("文件相对路径不能为空")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError("文件路径必须为相对于当前子目录的路径")
    parts = normalized.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("文件相对路径不能包含空层级或 . / ..")
    for part in parts:
        if _INVALID_COMPONENT.search(part) or part[-1:] in {".", " "} or _WINDOWS_RESERVED.match(part):
            raise ValueError("文件路径包含 Windows 不安全的目录或文件名：" + part)
    return normalized


def _issue(code: str, message: str, *, path: str = "", blocking: bool = True) -> dict[str, Any]:
    return {"code": code, "message": message, "error": message, "path": path,
            "blocking": blocking, "level": "error" if blocking else "warning"}


def _extension(path: str) -> str:
    return "." + path.rsplit("/", 1)[-1].rsplit(".", 1)[-1].casefold() if "." in path.rsplit("/", 1)[-1] else ""


def _canonical_format(value: str) -> str:
    compact = re.sub(r"[ _-]", "", value).casefold()
    return {"bd": "BDRip", "bdrip": "BDRip", "bluray": "BDRip", "dvd": "DVDRip", "dvdrip": "DVDRip",
            "webdl": "WEB-DL", "webrip": "WEBRip", "bdremux": "BDREMUX", "remux": "REMUX"}.get(compact, compact)


def _technical_token(value: str) -> bool:
    value = value.strip()
    if not value or _is_group_label(value) or _DATE_OR_TRACKS.fullmatch(value):
        return True
    # A bracket is technical only when all its meaningful text is known data.
    # A real title containing e.g. "BD" must not disappear as a whole.
    residue = _TECHNICAL.sub("", value.replace("_", " "))
    residue = re.sub(r"(?i)\b(?:ch[st]|jp|jpn|eng|chi|dual|audio|multi|sub|subs|hdr(?:10)?|sdr|yuv\d+p|p\d|\d[.]\d)\b", "", residue)
    return not re.sub(r"[\W_\d]", "", residue, flags=re.UNICODE)


def _is_group_label(value: str) -> bool:
    return bool(_KNOWN_GROUP.fullmatch(value) or ((_VCB.search(value) or _JSUM.search(value)) and re.search(r"[&+×]", value)))


def _safe_directory_name(value: str) -> str:
    name = value.translate(_SAFE_NAME_TRANSLATION)
    name = re.sub(r"[\x00-\x1f]", "", name).strip().rstrip(". ")
    if _WINDOWS_RESERVED.match(name):
        name = "_" + name
    return name


def _clean_nested_directory(name: str) -> str:
    """Remove known release-group labels from subdirectories, not filenames."""
    original = name
    name = _BRACKETS.sub(lambda match: "" if _is_group_label((match[1] or match[2]).strip()) else match[0], name)
    name = re.sub(r"\(([^()]*)\)", lambda match: "" if _is_group_label(match[1].strip()) else match[0], name)
    if _is_group_label(name.strip()):
        return ""
    return name.strip(" -") if name != original else name


def _companion_stem(path: str) -> str:
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    stem = _BRACKETS.sub(lambda item: " " if _technical_token(item[1] or item[2]) and not re.fullmatch(r"\d{1,3}(?:[vV]\d+)?", item[1] or item[2]) else item[0], stem)
    stem = re.sub(r"\(([^()]*)\)", lambda item: " " if _technical_token(item[1]) else item[0], stem)
    stem = _TECHNICAL.sub(" ", stem.replace("_", " "))
    return _COMPANION_TAGS.sub(" ", stem).strip(" ._-")


def _companion_key(path: str) -> str:
    return "".join(char for char in unicodedata.normalize("NFKC", _companion_stem(path)).casefold() if char.isalnum())


def infer_release_metadata(source_name: str) -> dict[str, Any]:
    """Extract candidates, never manufacture a database identity."""
    name = str(source_name or "").strip()
    suffix = _RELEASE_SUFFIX.fullmatch(name)
    vcb_match = _VCB.search(name)
    group = "VCBM" if vcb_match and "mawen1250" in name.casefold() else (
        vcb_match[0].upper() if vcb_match and re.fullmatch(r"(?i)vcb[a-z]{1,2}", vcb_match[0])
        else "VCB" if vcb_match else "Jsum" if _JSUM.search(name) else "")
    bracket_titles = [(match[1] or match[2]).strip() for match in _BRACKETS.finditer(name)
                      if not _technical_token(match[1] or match[2])]
    format_match = _FORMAT.search(name)
    press_format = _canonical_format(suffix["format"] if suffix else format_match["format"]) if suffix or format_match else ""
    if suffix:
        title = suffix["title"].strip()
        if suffix["group"] is not None:
            group = suffix["group"].strip()
            if group in {"---", "----"}:
                group = ""
    else:
        outside = _BRACKETS.sub(" ", name)
        outside = re.sub(r"\(([^()]*)\)", lambda item: " " if _is_group_label(item[1].strip()) else item[0], outside)
        outside = re.sub(r"(?i)\s*[-–]\s*(?:mawen1250\s*[&+]\s*)?vcb[- _]?studio.*$", "", outside)
        outside = re.sub(r"(?i)\s*[-–]\s*jsum\s*$", "", outside)
        outside = _FORMAT.sub(" ", outside)
        outside = re.sub(r"(?i)\s+\b(?:19|20)\d{2}\b\s*$", "", outside)
        outside = re.sub(r"\s+", " ", outside).strip(" _-–")
        title = outside if outside and not _technical_token(outside) else bracket_titles[0] if bracket_titles else ""
    # Names already using the standard release suffix are authoritative title
    # candidates; never infer a title from individual filenames inside it.
    issues: list[dict[str, Any]] = []
    if not title:
        issues.append(_issue("title-unresolved", "无法可靠识别作品名，请手动填写或关联数据库作品", path=name))
    if not press_format:
        issues.append(_issue("format-unresolved", "无法识别压制格式，请手动选择或填写压制格式", path=name))
    if re.search(r"\S\s*[+＋]\s*\S", title) or (not suffix and not outside and len(bracket_titles) > 1):
        issues.append(_issue("work-multiple", "目录名可能包含多个作品；请检查文件归属并手动确认数据库关联", path=name, blocking=False))
    return {"title": title, "press_format": press_format, "press_group": group, "issues": issues}


class BaseOrganizer(CommonCategoryClassifier):
    """Shared planning algorithm.  All rule methods are overridable."""

    id = "base"
    label = "基础整理类"
    description = "提供安全路径、资源分类、分集和预览等可复用基础能力"

    def publisher_prefix_length(self, parts: Sequence[str]) -> int:
        """Consume a known first-level publisher wrapper, never asset folders."""
        if not parts:
            return 0
        return int(_clean_nested_directory(parts[0]).casefold().replace(" ", "") in PUBLISHER_CONTAINERS)

    def resource_container_prefix(self, parts: Sequence[str]) -> int:
        if not parts:
            return 0
        first = self.classify_resource(ResourceContext(parts[0], is_directory=True))
        if first.category != "Others":
            return 0
        # Others is an unclassified container, not a permanently locked owner:
        # newly registered rules must be able to claim its loose files/folders.
        return 1 if not first.keep_directory else self.publisher_prefix_length(parts)

    def filter_directory(self, parts: Sequence[str]) -> DirectoryRule | None:
        """Choose a resource directory once, then keep all its contents together.

        Known mixed publisher wrappers expose their immediate children.  A
        category container (CDs/Scans) or an unrecognized asset directory is a
        single unit; the latter belongs wholly to Others, not to many categories
        chosen from its children's extensions.
        """
        if not parts:
            return None
        first = self.classify_resource(ResourceContext(parts[0], is_directory=True))
        start = self.resource_container_prefix(parts)
        if start >= len(parts):
            return None
        owner = first if not start else self.classify_resource(ResourceContext("/".join(parts[:start + 1]), is_directory=True, inside_publisher=True))
        if owner.category == "Others":
            return DirectoryRule(owner.category, start if owner.keep_directory else start + 1, owner.reason)
        # Explicit manual collections remain atomic even inside album/scans
        # packages. Never escape a previously unmatched independent directory;
        # that resource has already been assigned wholly to Others above.
        for index, part in enumerate(parts):
            if index == 0 or "_" in _clean_nested_directory(part):
                match = self.classify_resource(ResourceContext("/".join(parts[:index + 1]), is_directory=True))
                if match.protect_contents:
                    return DirectoryRule(match.category, index if match.keep_directory else index + 1, match.reason)
        return DirectoryRule(owner.category, start if owner.keep_directory else start + 1, owner.reason)

    def classify_file(self, relative_path: str) -> FileClassification:
        parts = relative_path.split("/")[:-1]
        rule = self.filter_directory(parts)
        if rule:
            return FileClassification(rule.category, rule.reason, rule.prefix_length,
                                      rule.preserve, rule.existing_root, rule.resource_type, directory_owned=True)
        match = self.classify_resource(ResourceContext(relative_path))
        return FileClassification(match.category, match.reason, self.resource_container_prefix(parts))

    def refine_related_files(self, classified, targets):
        """Resolve unpacked Subs and external audio against actual main videos.

        Numeric album tracks must not become episodes merely because a video
        with that number exists.  An explicit album container remains CD; loose
        audio must share a normalized filename or consist only of an episode
        number.  Music and Booklet are always manual, preserved containers.
        """
        video_by_name: dict[str, list[str]] = {}
        video_by_episode: dict[str, list[str]] = {}
        for path, item in classified:
            if item.category == "Disc" and _extension(path) in VIDEO_EXTENSIONS:
                video_by_name.setdefault(_companion_key(path), []).append(path)
                episode = self.episode_key(_companion_stem(path) + ".mkv")
                if episode:
                    episode = str(int(episode)) if episode.isdecimal() else episode
                    video_by_episode.setdefault(episode, []).append(path)
        result, issues, blocked = [], [], set()
        for path, item in classified:
            extension = _extension(path)
            extracted_subtitle = item.category == "Subs" and extension in SUBTITLE_EXTENSIONS
            loose_subtitle = item.category == "Disc" and not item.directory_owned and extension in SUBTITLE_EXTENSIONS
            audio = extension in AUDIO_EXTENSIONS - {".cue"} and not item.preserve and item.category == "CD"
            album_rule = self.filter_directory(path.split("/")[:-1])
            if audio and (album_rule is not None or _MUSIC_HINT.search(path) or _OP_ED.search(path.rsplit("/", 1)[-1])):
                audio = False
            if not extracted_subtitle and not loose_subtitle and not audio:
                result.append((path, item))
                continue
            key = _companion_key(path)
            episode = self.episode_key(_companion_stem(path) + ".mkv")
            episode = str(int(episode)) if episode.isdecimal() else episode
            matches = video_by_name.get(key, []) if key else []
            numeric_audio = bool(re.fullmatch(r"(?:\d{1,3}|s\d{1,2}e\d{1,3})", key, re.I))
            if not matches and episode and (extracted_subtitle or loose_subtitle or numeric_audio):
                matches = video_by_episode.get(episode, [])
            if matches:
                result.append((path, FileClassification("Disc", "已解压字幕匹配到实际正片，随对应剧集保存" if extracted_subtitle or loose_subtitle else "文件名 / 集数明确匹配实际正片的外置音轨，随对应剧集保存",
                                                       item.prefix_length if extracted_subtitle or loose_subtitle else 0,
                                                       companion_video=matches[0], companion_videos=tuple(matches))))
                continue
            if extracted_subtitle:
                if path not in targets:
                    issues.append(_issue("subtitle-video-unresolved", "已解压字幕未能对应实际正片目录，请手动选择目标", path=path))
                    blocked.add(path)
            result.append((path, item))
        return result, issues, blocked

    def episode_key(self, relative_path: str) -> str:
        filename = relative_path.rsplit("/", 1)[-1]
        stem = filename.rsplit(".", 1)[0]
        match = _SEASON_EPISODE.search(stem)
        if match:
            return "S" + str(int(match["s"])).zfill(2) + "E" + str(int(match["e"])).zfill(2)
        # Remove codec/group brackets before the numeric fallback.  Explicit
        # numeric episode brackets remain, while 10bit / 1080p never become E10.
        stem = _BRACKETS.sub(lambda item: " " if _technical_token(item[1] or item[2]) and not re.fullmatch(r"\d{1,3}(?:[vV]\d+)?", item[1] or item[2]) else item[0], stem)
        for pattern in _EPISODE_PATTERNS:
            match = pattern.search(stem)
            if match:
                return match[1].zfill(2)
        return ""

    def target_for_file(self, relative_path: str, classification: FileClassification, *, episode_folder: str = "") -> str:
        if classification.preserve:
            return relative_path
        parts = relative_path.split("/")
        # Replacing SPs/Scans/CDs is independent of how a file was classified:
        # filename-based Menu/OP/ED rules also need to consume the wrapper.
        # Inside it, keep asset folders even if a filter used their name as a
        # category hint (e.g. SPs/Menu/01.mkv keeps the inner Menu directory).
        prefix = classification.prefix_length
        parents = [_clean_nested_directory(part) for part in parts[prefix:-1]]
        parents = [part for part in parents if part]
        if episode_folder and not (classification.directory_owned and parents):
            # Keep already assigned episode folders stable on repeated preview.
            if not parents or not re.fullmatch(r"(?i)_(?:\d{1,3}|S\d{2}E\d{2,3})", parents[0]):
                parents.insert(0, episode_folder)
        return "/".join([self.category_suffix(classification.category), *parents, parts[-1]])

    def category_suffix(self, category: str) -> str:
        if category not in self.category_names:
            raise ValueError("归类结果使用了未注册的目录类型：" + category)
        return "_" + category

    def category_directory(self, classification: FileClassification, *, title: str, press_format: str) -> str:
        """Name a new category root; existing roots and manual targets win.

        Release-group names belong only on the release directory.  Keep this
        hook separate from file filtering so derived strategies can customize
        naming without reimplementing preservation or companion handling.
        """
        prefix = "_".join(_safe_directory_name(value) for value in (title, _canonical_format(press_format)) if value)
        return prefix + self.category_suffix(classification.category)

    def prefer_flat_layout(self, press_format: str, classified: Sequence[tuple[str, FileClassification]]) -> bool:
        # Exceptions belong to explicitly selected future derived strategies.
        return False

    def has_single_video_versions(self, classified: Sequence[tuple[str, FileClassification]]) -> bool:
        videos = [path for path, _ in classified if _extension(path) in VIDEO_EXTENSIONS]
        episodes = Counter(str(int(key)) if key.isdecimal() else key for path in videos if (key := self.episode_key(path)))
        return bool(videos) and all(count == 1 for count in episodes.values())

    def existing_layout_roots(self, classified: Sequence[tuple[str, FileClassification]]) -> dict[str, list[str]]:
        roots: dict[str, set[str]] = {}
        for _, item in classified:
            if item.preserve:
                roots.setdefault(item.resource_type or item.category, set()).add(item.existing_root)
        return {key: sorted(value) for key, value in roots.items()}

    def category_root_candidates(self, classification: FileClassification, roots: Mapping[str, list[str]]) -> list[str]:
        # Music/Booklet are manual collections, never destinations for newly
        # discovered audio/images.  New resources use CD/Image respectively.
        return roots.get(classification.resource_type or classification.category, [])

    def propose(self, source_name: str, files: Sequence[Mapping[str, Any]], *, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
        overrides = overrides or {}
        metadata = infer_release_metadata(source_name)
        for key in ("title", "press_format", "press_group"):
            if key in overrides:
                metadata[key] = str(overrides[key] or "").strip()
        if metadata["press_group"] in {"---", "----"}:
            metadata["press_group"] = ""
        issues = [item for item in metadata["issues"] if item["code"] not in {"title-unresolved", "format-unresolved"}]
        if not metadata["title"]:
            issues.append(_issue("title-unresolved", "无法可靠识别作品名，请手动填写或关联数据库作品", path=source_name))
        if not metadata["press_format"]:
            issues.append(_issue("format-unresolved", "无法识别压制格式，请手动选择或填写压制格式", path=source_name))
        release_name = _safe_directory_name(metadata["title"] + ("_" + metadata["press_format"] if metadata["press_format"] else "")
                                            + ("(" + metadata["press_group"] + ")" if metadata["press_group"] else ""))
        release_name = str(overrides.get("release_name", release_name or source_name))
        category_title, category_format = metadata["title"], metadata["press_format"]
        # A standard target release name is authoritative even when the DB
        # uses a different display title.  Nonstandard manual names fall back
        # to the confirmed metadata rather than guessing a new title.
        named_release = _RELEASE_SUFFIX.fullmatch(release_name.replace("\\", "/").rsplit("/", 1)[-1])
        if named_release:
            category_title, category_format = named_release["title"], named_release["format"]
        targets = dict(overrides.get("targets") or {})
        for item in overrides.get("files") or []:
            if isinstance(item, Mapping) and "source_rel" in item and "target_rel" in item:
                targets[str(item["source_rel"])] = item["target_rel"]
        classified: list[tuple[str, FileClassification]] = []
        seen_sources: set[str] = set()
        for item in files:
            source = item.get("relative_path", item.get("source_rel", "")) if isinstance(item, Mapping) else ""
            try:
                path = normalize_relative_path(source)
            except ValueError as error:
                issues.append(_issue("unsafe-source-path", str(error), path=str(source)))
                continue
            if path.casefold() in seen_sources:
                issues.append(_issue("duplicate-source", "同一个来源文件重复出现在扫描结果中", path=path))
                continue
            seen_sources.add(path.casefold())
            classified.append((path, self.classify_file(path)))
        classified, related_issues, blocked_related = self.refine_related_files(classified, targets)
        issues.extend(related_issues)
        flat = self.prefer_flat_layout(metadata["press_format"], classified)
        existing_roots = self.existing_layout_roots(classified)
        episodes = {path: self.episode_key(classification.companion_video or path) for path, classification in classified if classification.category == "Disc"}
        episode_width = max((len(key) for key in episodes.values() if key.isdecimal()), default=2)
        episodes = {path: str(int(key)).zfill(episode_width) if key.isdecimal() else key for path, key in episodes.items()}
        counts = Counter(episode for episode in episodes.values() if episode)
        video_episodes = {episodes[path] for path, classification in classified if classification.category == "Disc" and _extension(path) in VIDEO_EXTENSIONS}
        organized_episodes: dict[tuple[str, str], set[str]] = {}
        for path, classification in classified:
            if classification.preserve and classification.category == "Disc" and _extension(path) in VIDEO_EXTENSIONS:
                parent = path.rsplit("/", 1)[0]
                if parent != classification.existing_root and episodes.get(path):
                    organized_episodes.setdefault((classification.existing_root.casefold(), episodes[path]), set()).add(parent)
        result_files: list[dict[str, Any]] = []
        for path, classification in classified:
            key = episodes.get(path, "")
            folder = "_" + key if key and counts[key] > 1 and key in video_episodes else ""
            target = path if flat else self.target_for_file(path, classification, episode_folder=folder)
            reason = "仅有正片及字幕且没有需拆分的多版本，保留现有简洁布局" if flat else classification.reason
            if folder and not flat and not classification.preserve:
                reason += "；同集存在多个资源，归入 " + folder
            if not flat and not classification.preserve and not classification.companion_video and path not in blocked_related:
                root_choices = self.category_root_candidates(classification, existing_roots)
                existing_root = root_choices[0] if len(root_choices) == 1 else ""
                if len(root_choices) > 1 and path not in targets:
                    target = path
                    reason = "存在多个同类目录，保留来源位置并等待手动选择"
                    issues.append(_issue("existing-category-ambiguous", "散落文件可进入多个已有分类目录，请手动填写目标：" + "、".join(root_choices[:10]), path=path))
                if existing_root:
                    canonical_prefix = self.category_suffix(classification.category) + "/"
                    if target.startswith(canonical_prefix):
                        target = existing_root + "/" + target[len(canonical_prefix):]
                        episode_roots = organized_episodes.get((existing_root.casefold(), key), set())
                        if classification.category == "Disc" and len(episode_roots) == 1:
                            target = next(iter(episode_roots)) + "/" + path.rsplit("/", 1)[-1]
                        elif classification.category == "Disc" and len(episode_roots) > 1 and path not in targets:
                            target = path
                            issues.append(_issue("existing-episode-ambiguous", "同集存在多个已有资源目录，请手动选择散落文件所属目录", path=path))
                        reason += "；复用当前已有分类目录 " + existing_root
                elif not root_choices:
                    canonical_prefix = self.category_suffix(classification.category) + "/"
                    if target.startswith(canonical_prefix):
                        category_root = self.category_directory(classification, title=category_title, press_format=category_format)
                        target = category_root + "/" + target[len(canonical_prefix):]
            if path in blocked_related:
                target = path
                reason = "未能唯一关联实际正片，保持来源位置并等待手动目标"
            if path in targets:
                target = targets[path]
                reason = "采用用户手动指定的文件目标路径"
            try:
                target = normalize_relative_path(target)
            except ValueError as error:
                issues.append(_issue("unsafe-target-path", str(error), path=path))
                target = path
            result_files.append({"source_rel": path, "target_rel": target, "category": classification.category, "reason": reason})
        by_source = {item["source_rel"]: item for item in result_files}
        for path, classification in classified:
            if classification.companion_video and path not in targets and path not in blocked_related:
                video_targets = [by_source[candidate]["target_rel"] for candidate in classification.companion_videos]
                parents = {target.rsplit("/", 1)[0] + "/" if "/" in target else "" for target in video_targets}
                if len(parents) == 1:
                    by_source[path]["target_rel"] = next(iter(parents)) + path.rsplit("/", 1)[-1]
                    by_source[path]["reason"] = classification.reason + "；对应正片：" + classification.companion_video
                else:
                    code = "subtitle-video-ambiguous" if _extension(path) in SUBTITLE_EXTENSIONS else "audio-video-ambiguous"
                    issues.append(_issue(code, "匹配的正片版本将进入不同目录，请为配套字幕 / 音轨手动选择目标", path=path))
                    by_source[path]["target_rel"] = path
                    by_source[path]["reason"] = "多个正片版本的最终目录不同，等待手动选择配套资源去向"
                    blocked_related.add(path)
        destinations: dict[str, str] = {}
        for item in result_files:
            target = item["target_rel"]
            previous = destinations.get(target.casefold())
            if previous is not None and previous != item["source_rel"]:
                issues.append(_issue("target-conflict", "多个来源文件将写入同一目标：" + previous + " / " + item["source_rel"], path=target))
            destinations[target.casefold()] = item["source_rel"]
        suggested_moves = sum(item["source_rel"] != item["target_rel"] for item in result_files)
        has_existing = bool(existing_roots)
        unresolved_layout = bool(blocked_related) or any(item["code"] in {"existing-category-ambiguous", "existing-episode-ambiguous"} for item in issues)
        kind = "partial" if (suggested_moves or unresolved_layout) and has_existing else "unorganized" if suggested_moves else "organized" if has_existing else "flat" if flat else "organized"
        assessment = {"kind": kind, "preserved_files": len(result_files) - suggested_moves, "suggested_moves": suggested_moves,
                      "reason": {"partial": "已有分类内容原位保留，只整理散落资源或手动指定项",
                                 "unorganized": "发现尚未归类或需拆分的资源，提供分类建议", "organized": "已有资源分类及内部层级，保留原有命名和结构",
                                 "flat": "目录仅有正片及字幕，现有简洁布局无需新增分类层级"}[kind]}
        if not result_files:
            assessment["reason"] = "目录没有可归类文件，保持现有结构"
        return {"strategy_id": self.id, "strategy_reason": "", "title": metadata["title"], "press_format": metadata["press_format"],
                "press_group": metadata["press_group"], "release_name": release_name, "files": result_files, "issues": issues,
                "layout_assessment": assessment}


class GenericOrganizer(BaseOrganizer):
    id = "generic"
    label = "通用"
    description = "按已注册分类函数依次筛选文件和目录；未匹配资源统一进入 Others"


STRATEGIES: dict[str, type[BaseOrganizer]] = {GenericOrganizer.id: GenericOrganizer}


def list_strategies() -> list[dict[str, str]]:
    return [{"id": "auto", "label": "自动", "description": "自动选择整理规则；当前使用通用规则"},
            *[{"id": item.id, "label": item.label, "description": item.description} for item in STRATEGIES.values()]]


def propose_layout(source_name: str, files: Sequence[Mapping[str, Any]], strategy_id: str = "auto", overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Public pure-planning entry point; relative targets exclude release root."""
    strategy_id = str(strategy_id or "auto")
    if strategy_id == "auto":
        selected = "generic"
        reason = "使用通用分类函数数组；压制组仅作为作品数据，不改变默认分类规则"
    else:
        if strategy_id not in STRATEGIES:
            raise ValueError("未知整理类：" + strategy_id)
        selected = strategy_id
        reason = "用户手动选择：" + STRATEGIES[selected].label
    result = STRATEGIES[selected]().propose(source_name, files, overrides=overrides)
    result["strategy_reason"] = reason
    return result
