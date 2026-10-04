"""Bounded Bangumi cover acquisition and content-addressed, offline-only reads."""
from __future__ import annotations

import hashlib
from http.client import HTTPException
import io
from pathlib import Path
import re
import socket
import ssl
import stat
import struct
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import warnings

from work_catalog_yaml.layout import feature_data_root
from work_catalog_yaml.persistence import atomic_write_bytes
from .model import COVER_MIMES, validate_cover_reference
from .network import failure_reason

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_DIMENSION = 12000
MAX_PIXELS = 40_000_000
MAX_FRAMES = 64
MAX_DECODED_PIXELS = 80_000_000
DOWNLOAD_TIMEOUT = 20
SOCKET_TIMEOUT = 8
ASSET_NAME = re.compile(r"([0-9a-f]{64})\.(jpg|png|gif|webp)\Z")
_COVER_PATH = re.compile(r"/(?:r/[1-9][0-9]{0,3}(?:x[1-9][0-9]{0,3})?/)?pic/cover/[lcmsg]/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+\.(?:jpe?g|png|gif|webp)\Z")


def validate_source_url(value) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 for char in value):
        raise ValueError("封面地址缺失或无效")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname == "lain.bgm.tv" and parsed.port in {None, 443}
                 and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment
                 and _COVER_PATH.fullmatch(parsed.path))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("封面地址不属于允许的 Bangumi HTTPS 封面 CDN 路径")
    return value


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("封面地址发生重定向，已拒绝访问其他地址")


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("封面下载超过总时限")
    return min(SOCKET_TIMEOUT, remaining)


def _download(url, *, user_agent, deadline=None):
    url = validate_source_url(url)
    deadline = min(deadline, time.monotonic() + DOWNLOAD_TIMEOUT) if deadline is not None else time.monotonic() + DOWNLOAD_TIMEOUT
    request = Request(url, headers={"User-Agent": user_agent, "Accept": "image/jpeg,image/png,image/gif,image/webp",
                                   "Accept-Encoding": "identity"}, method="GET")
    try:
        with build_opener(_NoRedirects()).open(request, timeout=_remaining(deadline)) as response:
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if mime not in COVER_MIMES.values():
                raise ValueError("封面响应不是允许的 JPEG、PNG、GIF 或 WebP 图片")
            if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise ValueError("封面响应不允许额外内容编码")
            length = response.headers.get("Content-Length")
            if length and (not length.isdecimal() or not 0 < int(length) <= MAX_IMAGE_BYTES):
                raise ValueError("封面响应超出安全大小限制")
            chunks, size = [], 0
            read = getattr(response, "read1", response.read)
            while True:
                _remaining(deadline)
                chunk = read(min(65536, MAX_IMAGE_BYTES + 1 - size))
                _remaining(deadline)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError("封面响应超出安全大小限制")
                chunks.append(chunk)
            if length and size != int(length):
                raise ValueError("封面图片未完整接收")
            return b"".join(chunks), mime
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise ValueError(f"封面下载失败（HTTP {status}）") from None
    except (URLError, TimeoutError, socket.timeout, socket.gaierror, ssl.SSLError, ConnectionError, HTTPException) as exc:
        raise ValueError(f"封面连接失败或超时：{failure_reason(exc)}") from None


def _image_dimensions(size):
    width, height = size
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION and width * height <= MAX_PIXELS):
        raise ValueError("封面尺寸超出安全范围")
    return width, height


def inspect_image(data, mime):
    """Validate structure and decode bounded pixel/frame data before storing it.

    Image.open alone is lazy and only recognizes a header; verify plus a new
    open/load pass is necessary to reject header-only and truncated pictures.
    Keep the downloaded encoding unchanged after validation.
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("封面图片为空或超出安全大小限制")
    try:
        from PIL import Image, ImageFile, UnidentifiedImageError
    except ImportError:
        raise RuntimeError("封面完整性校验需要 Pillow；请更新应用或安装工程声明的 Pillow 依赖，其他作品功能仍可使用") from None
    if ImageFile.LOAD_TRUNCATED_IMAGES:
        raise RuntimeError("图片运行环境启用了不安全的截断图片读取，已停止封面导入；请恢复默认图片校验配置")
    formats = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=list(formats)) as picture:
                extension = formats[picture.format]
                if mime != COVER_MIMES[extension]:
                    raise ValueError("封面 MIME 类型与实际图片格式不一致")
                width, height = _image_dimensions(picture.size)
                picture.verify()
            with Image.open(io.BytesIO(data), formats=list(formats)) as picture:
                total_pixels = 0
                # Do not use n_frames: some decoders discover it by scanning an
                # unbounded animation. Seek/load only a bounded frame sequence.
                for index in range(MAX_FRAMES + 1):
                    try:
                        picture.seek(index)
                    except EOFError:
                        break
                    if index == MAX_FRAMES:
                        raise ValueError("封面动画帧数超出安全范围")
                    frame_width, frame_height = _image_dimensions(picture.size)
                    total_pixels += frame_width * frame_height
                    if total_pixels > MAX_DECODED_PIXELS:
                        raise ValueError("封面累计解码像素超出安全范围")
                    picture.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("封面解码尺寸超出安全范围") from None
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError, IndexError, struct.error):
        raise ValueError("封面图片不完整、损坏或无法解码；不接受只有文件头的伪装图片") from None
    return {"extension": extension, "mime_type": mime, "width": width, "height": height}


def _is_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def asset_root(root=None):
    path = (Path(root) if root is not None else feature_data_root("catalog-library") / "assets").absolute()
    for candidate in (path, *path.parents):
        if _is_link(candidate) or (candidate.exists() and not candidate.is_dir()):
            raise ValueError("封面目录必须为普通目录，不允许符号链接或联接")
    return path


def read_asset(filename, *, root=None):
    match = ASSET_NAME.fullmatch(filename) if isinstance(filename, str) else None
    if match is None:
        raise ValueError("封面资源名称无效")
    path = asset_root(root) / filename
    if _is_link(path) or not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ValueError("封面资源不存在或不是安全的普通文件")
    with path.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    if hashlib.sha256(data).hexdigest() != match[1]:
        raise ValueError("封面资源校验失败")
    mime = COVER_MIMES[match[2]]
    inspect_image(data, mime)
    return data, mime


def cover_url(cover):
    try:
        validate_cover_reference(cover)
    except (ValueError, TypeError):
        return ""
    return f"/api/catalog-library/assets/{cover['sha256']}.{cover['extension']}"


def acquire_cover(subject, *, user_agent, root=None, deadline=None):
    images = subject.get("images")
    if not isinstance(images, dict):
        raise ValueError("Bangumi 作品没有封面地址")
    # Image fields are provider data, not arbitrary user supplied URLs.
    url = next((images.get(key) for key in ("large", "common", "medium", "small", "grid") if images.get(key)), None)
    data, mime = _download(url, user_agent=user_agent, deadline=deadline)
    info = inspect_image(data, mime)
    digest = hashlib.sha256(data).hexdigest()
    path = asset_root(root) / f"{digest}.{info['extension']}"
    if path.exists() or _is_link(path):
        read_asset(path.name, root=root)
    else:
        atomic_write_bytes(path, data)
    return {"sha256": digest, **info, "provider": "bangumi", "external_id": str(subject["id"]), "source_url": url}
