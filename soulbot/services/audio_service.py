"""Typed, bounded audio artifacts, separate from images and scoped by the server."""

import hashlib
import math
import re
import time
import uuid
import wave
from dataclasses import dataclass
from io import BytesIO

from tinytag import TinyTag, TinyTagException

from soulbot.services.media_service import MediaError

MAX_AUDIO_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class AudioPayload:
    data: bytes
    mime: str
    format: str
    sample_rate: int
    channels: int
    duration: float


def inspect_audio(content):
    if not content or len(content) > MAX_AUDIO_BYTES:
        raise MediaError("音频为空或超过 8MB 限制")
    try:
        tag = TinyTag.get(file_obj=BytesIO(content), tags=False, image=False)
        formats = {"audio/mpeg": "mp3", "audio/wav": "wav", 'audio/wav; codecs="1"': "wav"}
        if tag.mime_type not in formats:
            raise MediaError("语音当前只支持实际 MP3 或 PCM WAV 文件")
        duration, rate, channels = tag.duration, tag.samplerate, tag.channels
        if (
            duration is None
            or not math.isfinite(duration)
            or not 0 < duration <= 180
            or rate is None
            or not 8000 <= rate <= 96000
            or channels not in {1, 2}
        ):
            raise MediaError("音频时长、采样率或声道无效（最多 180 秒、单/双声道）")
        if formats[tag.mime_type] == "wav":
            with wave.open(BytesIO(content), "rb") as stream:
                if (
                    stream.getcomptype() != "NONE"
                    or len(stream.readframes(stream.getnframes()))
                    != stream.getnframes() * channels * stream.getsampwidth()
                ):
                    raise MediaError("WAV 内容不完整或不是支持的 PCM 格式")
        return AudioPayload(
            bytes(content),
            "audio/wav" if formats[tag.mime_type] == "wav" else "audio/mpeg",
            formats[tag.mime_type],
            rate,
            channels,
            duration,
        )
    except (TinyTagException, ValueError, OSError, EOFError, wave.Error):
        raise MediaError("音频格式无法解析，请检查语音服务返回的实际文件") from None


class AudioStore:
    def __init__(self, root, db, clock=time.time):
        self.directory, self.db, self.clock = root / "data" / "audio", db, clock
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save(self, payload: AudioPayload, source="tts", scope="admin"):
        if self.directory.is_symlink():
            raise MediaError("音频目录不能是符号链接")
        audio = inspect_audio(payload.data)
        self.cleanup()
        key, now = uuid.uuid4().hex, self.clock()
        filename = key + "." + audio.format
        path = self.directory / filename
        with path.open("xb") as stream:
            path.chmod(0o600)
            stream.write(audio.data)
        try:
            with self.db.connect() as db:
                db.execute(
                    "INSERT INTO audio_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        filename,
                        audio.mime,
                        audio.format,
                        audio.sample_rate,
                        audio.channels,
                        audio.duration,
                        len(audio.data),
                        hashlib.sha256(audio.data).hexdigest(),
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

    def row(self, key, scope=None):
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise MediaError("音频标识无效")
        with self.db.connect() as db:
            row = db.execute(
                "SELECT * FROM audio_artifacts WHERE id=? AND expires>?", (key, self.clock())
            ).fetchone()
        if not row:
            raise MediaError("音频不存在或已过期，请重新生成")
        item = dict(row)
        if scope is not None and item["scope"] != scope:
            raise MediaError("此音频属于其它会话，不能读取或发送")
        path = self.directory / item["filename"]
        if (
            not re.fullmatch(key + r"\.(mp3|wav)", item["filename"])
            or self.directory.is_symlink()
            or path.is_symlink()
            or not path.is_file()
        ):
            raise MediaError("音频文件已不可用")
        return item

    def info(self, key, scope=None):
        item = self.row(key, scope)
        for field in ("filename", "scope", "sha256"):
            item.pop(field)
        item["url"] = "/api/audio/" + key
        return item

    def read(self, key, scope=None):
        item = self.row(key, scope)
        with (self.directory / item["filename"]).open("rb") as stream:
            content = stream.read(MAX_AUDIO_BYTES + 1)
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise MediaError("音频文件已发生变化，请重新生成")
        return inspect_audio(content)

    def cleanup(self):
        if self.directory.is_symlink():
            raise MediaError("音频目录不能是符号链接")
        with self.db.connect() as db:
            for row in db.execute(
                "SELECT filename FROM audio_artifacts WHERE expires<=?", (self.clock(),)
            ):
                if re.fullmatch(r"[a-f0-9]{32}\.(mp3|wav)", row[0]):
                    (self.directory / row[0]).unlink(missing_ok=True)
            db.execute("DELETE FROM audio_artifacts WHERE expires<=?", (self.clock(),))
