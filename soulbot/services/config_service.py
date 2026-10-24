"""One validated configuration schema for disk and WebUI. No secrets allowed here."""

import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class Identity(Model):
    name: str = Field(default="SoulBot", min_length=1, max_length=40)
    language: Literal["zh-CN"] = "zh-CN"
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("请选择有效时区，例如 Asia/Shanghai") from None
        return value


class Web(Model):
    host: Literal["127.0.0.1", "0.0.0.0", "::1"] = "127.0.0.1"
    port: int = Field(default=2526, ge=1024, le=65535)


class QQ(Model):
    enabled: bool = False
    connection: Literal["reverse", "forward"] = "reverse"
    test_url: str = "ws://127.0.0.1:2525/onebot/v11/ws"

    @field_validator("test_url")
    @classmethod
    def valid_url(cls, value):
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if (
            parts.scheme not in {"ws", "wss"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("请输入 ws:// 或 wss:// 地址，Token 请填写到独立密钥字段")
        try:
            parts.port
        except ValueError:
            raise ValueError("端口号无效") from None
        return value


class LLM(Model):
    base_url: str = "https://api.openai.com/v1"
    model: str = Field(default="", max_length=120)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("请输入不带密钥的 HTTP(S) API 地址")
        return value.rstrip("/")


class Personality(Model):
    description: str = Field(default="温柔、好奇，尊重彼此的边界。", max_length=2000)
    style: str = Field(default="自然、简洁的中文", max_length=500)
    interests: str = Field(default="音乐、日常生活与新知识", max_length=500)
    owner_qq: str = Field(default="", pattern=r"^([1-9][0-9]{4,11})?$")
    owner_title: str = Field(default="主人", max_length=40)
    prompt: str = Field(default="", max_length=8000)


class Behavior(Model):
    group_mode: Literal["silent", "mention_only", "natural", "active"] = "mention_only"
    autonomous: bool = False
    diary: bool = False
    qzone: bool = False
    quiet_hours: bool = True
    daily_tokens: int = Field(default=300000, ge=1000, le=10000000)
    daily_cost_cny: float = Field(default=5, gt=0, le=10000, allow_inf_nan=False)

    @field_validator("autonomous", "diary", "qzone")
    @classmethod
    def unavailable(cls, value):
        if value:
            raise ValueError("当前 M0 版本尚未开放此功能，请等待对应阶段")
        return value


class Appearance(Model):
    motion: bool = True
    wallpaper: str = ""
    wallpaper_kind: Literal["image", "video"] = "image"

    @field_validator("wallpaper")
    @classmethod
    def local_wallpaper(cls, value):
        if value and not re.fullmatch(
            r"/api/wallpaper/[a-f0-9]{32}\.(png|jpg|gif|webp|mp4|webm)", value
        ):
            raise ValueError("请通过壁纸上传功能选择本地文件")
        return value


class Settings(Model):
    soulbot: Identity = Field(default_factory=Identity)
    web: Web = Field(default_factory=Web)
    qq: QQ = Field(default_factory=QQ)
    llm: LLM = Field(default_factory=LLM)
    personality: Personality = Field(default_factory=Personality)
    behavior: Behavior = Field(default_factory=Behavior)
    appearance: Appearance = Field(default_factory=Appearance)


def atomic_write(path: Path, content: str, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".soulbot-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.chmod(temporary, mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ConfigService:
    def __init__(self, root: Path):
        self.path = root / "config.yaml"
        self.backup_path = root / "data" / "config.backup.yaml"
        self.lock = RLock()
        if not self.path.exists():
            atomic_write(self.path, self.dump(Settings()))
        self.current = self.read(self.path)

    @staticmethod
    def dump(settings):
        return yaml.safe_dump(settings.model_dump(), allow_unicode=True, sort_keys=False)

    @staticmethod
    def read(path):
        return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def save(self, settings: Settings):
        with self.lock:
            atomic_write(self.backup_path, self.dump(self.current))
            atomic_write(self.path, self.dump(settings))
            self.current = settings.model_copy(deep=True)

    def restore(self):
        with self.lock:
            restored = self.read(self.backup_path)
            self.save(restored)
            return restored
