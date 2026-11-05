"""Immutable, private media snapshots pinned to an approved proactive job."""

import hashlib
import json
import os
import re
import shutil

from pydantic import Field

from soulbot.services.audio_service import MAX_AUDIO_BYTES, inspect_audio
from soulbot.services.config_service import Model
from soulbot.services.media_service import MAX_IMAGE_BYTES, ImagePayload, MediaError, inspect_image


class AttachmentInput(Model):
    reference: str = Field(pattern=r"^(emoji|media|audio):[a-f0-9]{32}$")


class ProactiveMedia:
    def __init__(self, services):
        self.s = services
        self.root = services.root / "data/proactive_media"
        self.root.mkdir(exist_ok=True, mode=0o700)

    def directory(self, key):
        if not re.fullmatch(r"[a-f0-9]{32}", key) or self.root.is_symlink():
            raise MediaError("主动附件目录无效")
        path = self.root / key
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise MediaError("主动附件目录不可用")
        return path

    def pin(self, key, attachments):
        if not attachments:
            return []
        from soulbot.tools.media_tools import read_image

        folder = self.directory(key)
        folder.mkdir(mode=0o700)
        result = []
        try:
            for index, attachment in enumerate(attachments):
                reference = attachment.reference
                if reference.startswith("audio:"):
                    payload = self.s.audio.read(reference.split(":", 1)[1], "admin")
                    kind, extension, source = "audio", payload.format, "tts"
                else:
                    payload = read_image(self.s, reference, "admin")
                    extension, _, _, _ = inspect_image(payload.data)
                    kind = "image"
                    source = (
                        "emoji"
                        if reference.startswith("emoji:")
                        else self.s.media.info(reference.split(":", 1)[1], "admin")["source"]
                    )
                filename = str(index) + "." + extension
                with (folder / filename).open("xb") as stream:
                    os.chmod(folder / filename, 0o600)
                    stream.write(payload.data)
                result.append(
                    {
                        "kind": kind,
                        "filename": filename,
                        "mime": payload.mime,
                        "size": len(payload.data),
                        "sha256": hashlib.sha256(payload.data).hexdigest(),
                        "source": source,
                    }
                )
            return result
        except BaseException:
            self.discard(key)
            raise

    def discard(self, key):
        folder = self.directory(key)
        if folder.exists():
            shutil.rmtree(folder)

    def read(self, key, index, metadata):
        if type(index) is not int or not 0 <= index < len(metadata) or index > 1:
            raise MediaError("附件不存在")
        item = metadata[index]
        filename = item.get("filename", "")
        if not re.fullmatch(str(index) + r"\.(png|jpg|gif|webp|mp3|wav)", filename):
            raise MediaError("附件索引无效")
        path = self.directory(key) / filename
        if path.is_symlink():
            raise MediaError("附件文件无效")
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            raise MediaError("附件已清理或不可用，请重新创建草稿") from None
        if len(content) != item.get("size") or hashlib.sha256(content).hexdigest() != item.get(
            "sha256"
        ):
            raise MediaError("附件内容已变化，已阻止发送")
        if item.get("kind") == "audio":
            if len(content) > MAX_AUDIO_BYTES:
                raise MediaError("语音附件过大")
            payload = inspect_audio(content)
        else:
            _, mime, _, _ = inspect_image(content)
            payload = ImagePayload(content, mime)
        if payload.mime != item.get("mime"):
            raise MediaError("附件格式已变化")
        return payload

    def allowed(self, item):
        cfg = self.s.config.current
        source = item.get("source")
        if item["kind"] == "audio":
            return cfg.tts.enabled and cfg.tools.synthesize_speech == "auto"
        if source == "image_generation":
            return cfg.image_generation.enabled and cfg.tools.generate_image == "auto"
        if source == "emoji":
            return cfg.emoji.enabled and cfg.tools.select_emoji == "auto"
        if source == "meme":
            return cfg.meme.enabled and cfg.tools.generate_meme == "auto"
        if source == "html_render":
            return cfg.html_render.enabled and cfg.tools.render_html == "auto"
        return True

    @staticmethod
    def public(key, metadata):
        return [
            {
                "kind": item["kind"],
                "mime": item["mime"],
                "size": item["size"],
                "url": f"/api/proactive/jobs/{key}/attachments/{index}",
            }
            for index, item in enumerate(metadata)
        ]

    def cleanup(self, now):
        if self.root.is_symlink():
            raise MediaError("主动附件目录无效")
        with self.s.db.connect() as db:
            rows = {
                row["id"]: dict(row)
                for row in db.execute("SELECT id,status,latest_at FROM proactive_jobs")
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
                and row["status"] not in {"pending_approval", "scheduled", "waiting", "running"}
                and row["latest_at"] + 7 * 86400 < now
            ):
                self.discard(path.name)

    def metadata(self, key):
        with self.s.db.connect() as db:
            row = db.execute(
                "SELECT attachments_json FROM proactive_jobs WHERE id=?", (key,)
            ).fetchone()
        if not row:
            raise LookupError("任务不存在")
        return json.loads(row[0])
