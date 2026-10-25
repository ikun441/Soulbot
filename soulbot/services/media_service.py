"""Validated private images. Callers use opaque IDs, never model supplied paths/URLs."""

import os
import re
import time
import uuid
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 16 * 1024 * 1024
FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "GIF": ("gif", "image/gif"),
    "WEBP": ("webp", "image/webp"),
}


class MediaError(Exception):
    """Safe, user-facing media/service failure."""


@dataclass(frozen=True)
class ImagePayload:
    data: bytes
    mime: str


def inspect_image(content):
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise MediaError("图片为空或超过 16MB 限制")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                if image.format not in FORMATS or image.width * image.height > 16000000:
                    raise MediaError("仅支持 PNG/JPEG/GIF/WebP，图片不能超过 1600 万像素")
                frames = getattr(image, "n_frames", 1)
                if frames > 300 or frames * image.width * image.height > 100000000:
                    raise MediaError("动态图不能超过 300 帧，总帧像素不能超过 1 亿")
                extension, mime = FORMATS[image.format]
                width, height = image.size
                image.verify()
        return extension, mime, width, height
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise MediaError("无法读取图片，请检查文件是否完整") from None


class MediaStore:
    def __init__(self, root, db, clock=time.time):
        self.directory, self.db, self.clock = root / "data" / "media", db, clock
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save(self, content, source="meme", scope="admin"):
        if self.directory.is_symlink():
            raise MediaError("图片产物目录不能是符号链接")
        extension, mime, width, height = inspect_image(content)
        self.cleanup()
        key = uuid.uuid4().hex
        filename = key + "." + extension
        path = self.directory / filename
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(content)
        now = self.clock()
        try:
            with self.db.connect() as db:
                db.execute(
                    "INSERT INTO media_artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        filename,
                        mime,
                        width,
                        height,
                        len(content),
                        source,
                        now,
                        now + 86400,
                        scope,
                    ),
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self.info(key)

    def info(self, key, scope=None):
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise MediaError("图片标识无效")
        with self.db.connect() as db:
            row = db.execute(
                "SELECT * FROM media_artifacts WHERE id=? AND expires>?", (key, self.clock())
            ).fetchone()
        if not row:
            raise MediaError("图片不存在或已过期，请重新生成")
        item = dict(row)
        if scope is not None and item["scope"] != scope:
            raise MediaError("此图片属于其它会话，不能读取或发送")
        path = self.directory / item["filename"]
        if (
            self.directory.is_symlink()
            or path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(self.directory.resolve())
        ):
            raise MediaError("图片文件已不可用")
        item["url"] = "/api/media/" + key
        item.pop("filename")
        item.pop("scope")
        return item

    def read(self, key, scope=None):
        info = self.info(key, scope)
        matches = list(self.directory.glob(key + ".*"))
        if len(matches) != 1 or matches[0].is_symlink():
            raise MediaError("图片文件已不可用")
        try:
            with matches[0].open("rb") as stream:
                content = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            raise MediaError("图片文件无法访问，请重新生成") from None
        _, mime, _, _ = inspect_image(content)
        if mime != info["mime"]:
            raise MediaError("图片文件已发生变化，请重新生成")
        return ImagePayload(content, mime)

    def cleanup(self):
        with self.db.connect() as db:
            for row in db.execute(
                "SELECT filename FROM media_artifacts WHERE expires<=?", (self.clock(),)
            ):
                path = self.directory / row[0]
                if path.parent == self.directory:
                    path.unlink(missing_ok=True)
            db.execute("DELETE FROM media_artifacts WHERE expires<=?", (self.clock(),))
