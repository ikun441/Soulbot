"""Public-post image copies: immutable, metadata-free and independent of transient media."""

import hashlib
import os
import re
import shutil
from io import BytesIO

from PIL import Image, ImageOps
from pydantic import Field

from soulbot.services.config_service import Model
from soulbot.services.media_service import ImagePayload, MediaError, inspect_image

MAX_POST_IMAGE_BYTES = 4 * 1024 * 1024


class QZoneImageInput(Model):
    reference: str = Field(pattern=r"^(emoji|media):[a-f0-9]{32}$")


class QZoneImages:
    def __init__(self, services):
        self.s = services
        self.root = services.root / "data/qzone_images"
        self.root.mkdir(exist_ok=True, mode=0o700)

    def directory(self, key):
        if not re.fullmatch(r"[a-f0-9]{32}", key) or self.root.is_symlink():
            raise MediaError("空间图片目录无效")
        path = self.root / key
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise MediaError("空间图片目录不可用")
        return path

    @staticmethod
    def normalize(content):
        inspect_image(content)
        with Image.open(BytesIO(content)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGBA")
            background = Image.new("RGBA", image.size, "white")
            image = Image.alpha_composite(background, image).convert("RGB")
            image.thumbnail((2048, 2048))
            stream = BytesIO()
            image.save(stream, format="JPEG", quality=90)
            result = stream.getvalue()
            if len(result) > MAX_POST_IMAGE_BYTES:
                raise MediaError("转换后的空间图片超过4MB，请缩小图片后再试")
            return result, image.width, image.height

    def pin(self, key, inputs, scope="admin"):
        if not inputs:
            return []
        from soulbot.tools.media_tools import read_image

        folder = self.directory(key)
        folder.mkdir(mode=0o700)
        result = []
        try:
            for index, item in enumerate(inputs):
                source = "emoji"
                if item.reference.startswith("media:"):
                    source = self.s.media.info(item.reference.split(":")[1], scope)["source"]
                    if scope != "admin" and source not in {"image_generation", "html_render"}:
                        raise MediaError(
                            "会话素材只能使用生成图片或卡片作为空间配图；上传照片、头像及其他私人素材请由管理员另行选择"
                        )
                image = read_image(self.s, item.reference, scope)
                data, width, height = self.normalize(image.data)
                filename = f"{index}.jpg"
                path = folder / filename
                with path.open("xb") as stream:
                    os.chmod(path, 0o600)
                    stream.write(data)
                result.append(
                    {
                        "filename": filename,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                        "mime": "image/jpeg",
                        "width": width,
                        "height": height,
                        "source": source,
                    }
                )
            return result
        except BaseException:
            self.discard(key)
            raise

    def discard(self, key):
        path = self.directory(key)
        if path.exists():
            shutil.rmtree(path)

    def read(self, key, index, metadata):
        if type(index) is not int or not 0 <= index < len(metadata) or index > 2:
            raise MediaError("空间图片不存在")
        item = metadata[index]
        if item.get("filename") != f"{index}.jpg":
            raise MediaError("空间图片索引无效")
        path = self.directory(key) / item["filename"]
        if path.is_symlink():
            raise MediaError("空间图片文件无效")
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_POST_IMAGE_BYTES + 1)
        except OSError:
            raise MediaError("空间图片已清理或缺失，请重新选择配图") from None
        if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise MediaError("空间图片内容已变化，已阻止发布")
        _, mime, width, height = inspect_image(data)
        if mime != "image/jpeg" or (width, height) != (item.get("width"), item.get("height")):
            raise MediaError("空间图片格式已变化")
        return ImagePayload(data, mime)

    @staticmethod
    def public(row, metadata):
        return [
            {
                "mime": item["mime"],
                "width": item["width"],
                "height": item["height"],
                "size": item["size"],
                "url": f"/api/qzone/drafts/{row['id']}/images/{index}?revision={row['revision']}",
            }
            for index, item in enumerate(metadata)
        ]

    def cleanup(self, now):
        if self.root.is_symlink():
            raise MediaError("空间图片目录无效")
        with self.s.db.connect() as db:
            rows = {
                row["images_key"]: dict(row)
                for row in db.execute(
                    "SELECT images_key,status,updated_at FROM qzone_drafts WHERE images_key<>''"
                )
            }
        for path in self.root.iterdir():
            if (
                path.is_symlink()
                or not path.is_dir()
                or not re.fullmatch(r"[a-f0-9]{32}", path.name)
            ):
                continue
            row = rows.get(path.name)
            if (
                row is None
                and path.stat().st_mtime + 86400 < now
                or row is not None
                and row["status"] in {"published", "cancelled", "generation_failed", "rejected"}
                and row["updated_at"] + 30 * 86400 < now
            ):
                self.discard(path.name)
