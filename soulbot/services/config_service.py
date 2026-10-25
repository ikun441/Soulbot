"""One validated configuration schema for disk and WebUI. No secrets allowed here."""

import os
import re
import tempfile
from pathlib import Path
from threading import RLock
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class LLMEndpoint(Model):
    base_url: str = "https://api.openai.com/v1"
    model: str = Field(default="", max_length=120)
    enabled: bool = True
    timeout_seconds: float = Field(default=30, ge=1, le=120, allow_inf_nan=False)
    retries: int = Field(default=1, ge=0, le=2)
    context_window: int = Field(default=16384, ge=1024, le=2000000)
    streaming: bool = False
    supports_tools: bool = False
    stream_usage: bool = False
    max_output_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    input_price_cny_per_million: float | None = Field(
        default=None, ge=0, le=100000, allow_inf_nan=False
    )
    output_price_cny_per_million: float | None = Field(
        default=None, ge=0, le=100000, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def paired_prices(self):
        if (self.input_price_cny_per_million is None) != (
            self.output_price_cny_per_million is None
        ):
            raise ValueError("请同时填写输入和输出价格，或同时留空表示未知")
        return self

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
        try:
            parts.port
        except ValueError:
            raise ValueError("端口号无效") from None
        return value.rstrip("/")


class LLM(LLMEndpoint):
    fallbacks: dict[Literal["fallback_1", "fallback_2"], LLMEndpoint] = Field(default_factory=dict)


class RequestLimits(Model):
    max_input_per_request: int = Field(default=8000, ge=256, le=128000)
    max_output_per_request: int = Field(default=512, ge=16, le=8192)
    max_per_agent_task: int = Field(default=12000, ge=512, le=300000)


class GroupPolicy(Model):
    enabled: bool = True
    mode: Literal["silent", "mention_only", "natural", "active"] = "mention_only"
    allow_proactive: bool = False


class Personality(Model):
    description: str = Field(default="温柔、好奇，尊重彼此的边界。", max_length=2000)
    style: str = Field(default="自然、简洁的中文", max_length=500)
    interests: str = Field(default="音乐、日常生活与新知识", max_length=500)
    owner_qq: str = Field(default="", pattern=r"^([1-9][0-9]{4,11})?$")
    owner_title: str = Field(default="主人", max_length=40)
    prompt: str = Field(default="", max_length=8000)


class Behavior(Model):
    group_mode: Literal["silent", "mention_only", "natural", "active"] = "mention_only"
    private_chat: bool = True
    proactive_group_enabled: bool = False
    group_policies: dict[str, GroupPolicy] = Field(default_factory=dict)
    group_allowlist: list[str] = Field(default_factory=list, max_length=500)
    group_blocklist: list[str] = Field(default_factory=list, max_length=500)
    ignored_bot_ids: list[str] = Field(default_factory=list, max_length=500)
    wake_words: list[str] = Field(default_factory=list, max_length=10)
    followup_window_seconds: int = Field(default=90, ge=0, le=300)
    reply_cooldown_seconds: float = Field(default=8, ge=0, le=600, allow_inf_nan=False)
    group_batch_seconds: float = Field(default=5, ge=0, le=15, allow_inf_nan=False)
    private_batch_seconds: float = Field(default=0.4, ge=0, le=5, allow_inf_nan=False)
    proactive_interval_seconds: int = Field(default=180, ge=180, le=86400)
    max_proactive_per_day: int = Field(default=3, ge=0, le=10)
    global_send_interval_seconds: float = Field(default=1, ge=0.1, le=10, allow_inf_nan=False)
    context_turns: int = Field(default=8, ge=0, le=20)
    context_retention_hours: int = Field(default=24, ge=1, le=168)
    autonomous: bool = False
    diary: bool = False
    qzone: bool = False
    quiet_hours: bool = True
    daily_tokens: int = Field(default=300000, ge=1000, le=10000000)
    daily_cost_cny: float = Field(default=5, gt=0, le=10000, allow_inf_nan=False)

    @field_validator("group_policies", "group_allowlist", "group_blocklist", "ignored_bot_ids")
    @classmethod
    def valid_ids(cls, value):
        if len(value) > 500 or any(not re.fullmatch(r"[1-9][0-9]{4,11}", key) for key in value):
            raise ValueError("请输入有效的 QQ / 群号，最多 500 项")
        return value

    @field_validator("wake_words")
    @classmethod
    def valid_wake_words(cls, value):
        if any(not word.strip() or len(word) > 40 for word in value):
            raise ValueError("唤醒词不能为空，且不能超过 40 字")
        return value

    @field_validator("autonomous", "diary", "qzone")
    @classmethod
    def unavailable(cls, value):
        if value:
            raise ValueError("当前版本尚未开放此功能，请等待对应阶段")
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


class EmojiSettings(Model):
    enabled: bool = True
    directory: Literal["./emoji"] = "./emoji"
    recursive: bool = True
    max_candidates: int = Field(default=12, ge=1, le=50)
    browse_enabled: bool = True
    max_files: int = Field(default=100, ge=1, le=200)
    max_filename_chars: int = Field(default=80, ge=20, le=200)


class MemeExtensionSettings(Model):
    enabled: bool = False
    disabled_ids: list[str] = Field(default_factory=list, max_length=1000)
    max_generations_per_minute: int = Field(default=6, ge=1, le=120)

    @field_validator("disabled_ids")
    @classmethod
    def valid_ids(cls, values):
        if any(not re.fullmatch(r"[a-zA-Z0-9_\-]{1,100}", value) for value in values):
            raise ValueError("表情 ID 只能使用字母、数字、下划线和短横线，最多 100 字符")
        return list(dict.fromkeys(values))


class MemeSettings(Model):
    enabled: bool = False
    managed: bool = True
    auto_install: bool = True
    version: Literal["0.1.14"] = "0.1.14"
    base_url: str = "http://127.0.0.1:2233"
    timeout_seconds: float = Field(default=30, ge=5, le=120, allow_inf_nan=False)
    max_restarts: int = Field(default=3, ge=0, le=5)
    disabled_ids: list[str] = Field(default_factory=list, max_length=1000)
    max_generations_per_minute: int = Field(default=10, ge=1, le=120)
    extensions: dict[Literal["contrib", "emoji", "tudou"], MemeExtensionSettings] = Field(
        default_factory=lambda: {
            name: MemeExtensionSettings() for name in ("contrib", "emoji", "tudou")
        }
    )

    @field_validator("disabled_ids")
    @classmethod
    def valid_disabled_ids(cls, values):
        return MemeExtensionSettings.valid_ids(values)

    @field_validator("base_url")
    @classmethod
    def safe_url(cls, value):
        return LLMEndpoint.valid_url(value)

    @model_validator(mode="after")
    def managed_loopback(self):
        from urllib.parse import urlsplit

        url = urlsplit(self.base_url)
        if self.managed and (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or url.path
            or not url.port
            or url.port < 1024
        ):
            raise ValueError("托管服务地址必须是 http://127.0.0.1:端口（1024 以上）")
        return self


class MediaToolPolicies(Model):
    search_emoji: Literal["deny", "auto", "approval"] = "auto"
    browse_emoji: Literal["deny", "auto", "approval"] = "auto"
    select_emoji: Literal["deny", "auto", "approval"] = "auto"
    generate_meme: Literal["deny", "auto", "approval"] = "auto"
    list_meme_templates: Literal["deny", "auto", "approval"] = "auto"
    get_meme_info: Literal["deny", "auto", "approval"] = "auto"


class Settings(Model):
    soulbot: Identity = Field(default_factory=Identity)
    web: Web = Field(default_factory=Web)
    qq: QQ = Field(default_factory=QQ)
    llm: LLM = Field(default_factory=LLM)
    personality: Personality = Field(default_factory=Personality)
    behavior: Behavior = Field(default_factory=Behavior)
    appearance: Appearance = Field(default_factory=Appearance)
    limits: RequestLimits = Field(default_factory=RequestLimits)
    emoji: EmojiSettings = Field(default_factory=EmojiSettings)
    meme: MemeSettings = Field(default_factory=MemeSettings)
    tools: MediaToolPolicies = Field(default_factory=MediaToolPolicies)

    @model_validator(mode="after")
    def distinct_ports(self):
        from urllib.parse import urlsplit

        if self.meme.managed and urlsplit(self.meme.base_url).port == self.web.port:
            raise ValueError("meme-generator 与 SoulBot 需要使用不同端口")
        return self


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
