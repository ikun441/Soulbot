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
    reverse_ws_url: str = Field(default="", max_length=2048)

    @field_validator("reverse_ws_url")
    @classmethod
    def valid_reverse_url(cls, value):
        from urllib.parse import urlsplit

        if not value:
            return value
        cls.valid_url(value)
        parts = urlsplit(value)
        if (
            re.search(r"\s", value)
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "//" in parts.path
            or not re.fullmatch(r"/[a-zA-Z0-9_./\-]+", parts.path)
            or any(part in {".", "..", ""} for part in parts.path.strip("/").split("/"))
            or parts.path in {"/api", "/static"}
            or parts.path.startswith(("/api/", "/static/"))
        ):
            raise ValueError(
                "反向 WS 地址需要有效接收路径，不能包含空白、查询参数或保留的 API 路径"
            )
        return value

    def requires_token(self, web):
        from urllib.parse import urlsplit

        local = {"127.0.0.1", "localhost", "::1"}
        return web.host not in local or (
            self.connection == "forward" and urlsplit(self.test_url).hostname not in local
        )

    def receiver_url(self, web):
        host = web.host if web.host != "0.0.0.0" else "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return self.reverse_ws_url or f"ws://{host}:{web.port}/onebot/v11/ws"

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
    render_html: Literal["deny", "auto", "approval"] = "auto"
    synthesize_speech: Literal["deny", "auto", "approval"] = "auto"
    generate_image: Literal["deny", "auto", "approval"] = "auto"


class HTMLRenderSettings(Model):
    enabled: bool = False
    width: int = Field(default=800, ge=480, le=1200)
    max_height: int = Field(default=1800, ge=400, le=2400)
    timeout_seconds: float = Field(default=20, ge=5, le=60, allow_inf_nan=False)


class EdgeSpeechSettings(Model):
    voice: str = Field(
        default="zh-CN-XiaoxiaoNeural", min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-]+$"
    )
    rate_percent: int = Field(default=0, ge=-50, le=100)
    pitch_hz: int = Field(default=0, ge=-50, le=50)
    volume_percent: int = Field(default=0, ge=-50, le=50)


class CompatibleSpeechSettings(Model):
    base_url: str = "https://api.openai.com/v1"
    model: str = Field(default="", max_length=120)
    voice: str = Field(default="", max_length=100)
    response_format: Literal["mp3", "wav"] = "mp3"
    speed: float = Field(default=1, ge=0.25, le=4, allow_inf_nan=False)
    supports_instructions: bool = False
    instructions: str = Field(default="", max_length=500)
    price_cny_per_1000_chars: float | None = Field(
        default=None, ge=0, le=10000, allow_inf_nan=False
    )

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        return LLMEndpoint.valid_url(value)

    @model_validator(mode="after")
    def declared_instructions(self):
        if self.instructions and not self.supports_instructions:
            raise ValueError("请先确认语音模型支持 instructions，再设置语音风格指令")
        return self


class SpeechSettings(Model):
    enabled: bool = False
    provider: Literal["edge", "openai_compatible"] = "edge"
    edge: EdgeSpeechSettings = Field(default_factory=EdgeSpeechSettings)
    compatible: CompatibleSpeechSettings = Field(default_factory=CompatibleSpeechSettings)
    timeout_seconds: float = Field(default=40, ge=5, le=120, allow_inf_nan=False)
    max_text_chars: int = Field(default=500, ge=10, le=2000)
    max_duration_seconds: int = Field(default=120, ge=10, le=180)
    max_calls_per_minute: int = Field(default=3, ge=1, le=20)
    max_calls_per_day: int = Field(default=50, ge=1, le=1000)
    max_chars_per_day: int = Field(default=10000, ge=100, le=1000000)


class ASRSettings(Model):
    enabled: bool = False
    qq_enabled: bool = False
    provider: Literal["local", "openai_compatible"] = "local"
    base_url: str = "https://api.openai.com/v1"
    model: str = Field(default="", max_length=120)
    language: str = Field(default="zh", pattern=r"^([a-z]{2})?$")
    timeout_seconds: float = Field(default=90, ge=5, le=180, allow_inf_nan=False)
    max_duration_seconds: float = Field(default=60, ge=1, le=180, allow_inf_nan=False)
    max_calls_per_minute: int = Field(default=3, ge=1, le=20)
    max_calls_per_day: int = Field(default=50, ge=1, le=1000)
    max_seconds_per_day: int = Field(default=1800, ge=60, le=86400)
    price_cny_per_minute: float | None = Field(default=None, ge=0, le=1000, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        return LLMEndpoint.valid_url(value)


class ImageGenerationSettings(Model):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    model: str = Field(default="", max_length=120)
    size: str = Field(
        default="", max_length=24, pattern=r"^(auto|[1-9][0-9]{1,3}x[1-9][0-9]{1,3})?$"
    )
    quality: str = Field(default="", max_length=24, pattern=r"^[a-zA-Z0-9_-]*$")
    response_format: Literal["omit", "b64_json"] = "omit"
    output_format: Literal["omit", "png", "jpeg", "webp"] = "omit"
    timeout_seconds: float = Field(default=120, ge=5, le=180, allow_inf_nan=False)
    max_prompt_chars: int = Field(default=1000, ge=10, le=4000)
    max_calls_per_minute: int = Field(default=2, ge=1, le=10)
    max_images_per_day: int = Field(default=10, ge=1, le=100)
    price_cny_per_image: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        return LLMEndpoint.valid_url(value)

    @field_validator("size")
    @classmethod
    def bounded_size(cls, value):
        if value and value != "auto":
            width, height = map(int, value.split("x"))
            if width * height > 16000000:
                raise ValueError("请求图片不能超过 1600 万像素")
        return value


class EmotionValues(Model):
    valence: float = Field(default=60, ge=0, le=100, allow_inf_nan=False)
    energy: float = Field(default=55, ge=0, le=100, allow_inf_nan=False)
    curiosity: float = Field(default=60, ge=0, le=100, allow_inf_nan=False)
    sociability: float = Field(default=50, ge=0, le=100, allow_inf_nan=False)
    fatigue: float = Field(default=10, ge=0, le=100, allow_inf_nan=False)


class EmotionSettings(Model):
    enabled: bool = True
    automatic: bool = True
    baseline: EmotionValues = Field(default_factory=EmotionValues)
    recovery_half_life_minutes: float = Field(default=180, ge=5, le=10080, allow_inf_nan=False)
    interaction_cooldown_seconds: int = Field(default=60, ge=10, le=3600)
    high_threshold: float = Field(default=75, ge=60, le=95, allow_inf_nan=False)
    low_threshold: float = Field(default=25, ge=5, le=40, allow_inf_nan=False)
    hysteresis: float = Field(default=10, ge=1, le=20, allow_inf_nan=False)
    min_state_seconds: int = Field(default=60, ge=0, le=3600)

    @model_validator(mode="after")
    def separate_thresholds(self):
        if self.low_threshold + self.hysteresis >= self.high_threshold - self.hysteresis:
            raise ValueError("情绪高低阈值之间需要保留足够的滞回区间")
        return self


class RelationshipSettings(Model):
    enabled: bool = True
    automatic: bool = True
    affinity_per_interaction: int = Field(default=1, ge=1, le=5)
    reward_cooldown_seconds: int = Field(default=300, ge=60, le=86400)
    max_daily_gain: int = Field(default=5, ge=1, le=20)


class DiarySettings(Model):
    enabled: bool = False
    auto_generate: bool = True
    generation_time: str = Field(default="00:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    min_events: int = Field(default=2, ge=1, le=100)
    fact_retention_days: int = Field(default=90, ge=7, le=365)


class ProactiveSettings(Model):
    enabled: bool = False
    evaluation_interval_seconds: int = Field(default=30, ge=5, le=300)
    max_per_user_day: int = Field(default=1, ge=1, le=3)
    max_daily_total: int = Field(default=10, ge=1, le=100)
    min_interval_minutes: int = Field(default=1440, ge=60, le=10080)
    jitter_max_seconds: int = Field(default=120, ge=0, le=3600)


class MemorySettings(Model):
    enabled: bool = False
    max_items: int = Field(default=4, ge=1, le=10)
    max_chars: int = Field(default=1200, ge=200, le=4000)


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
    html_render: HTMLRenderSettings = Field(default_factory=HTMLRenderSettings)
    tts: SpeechSettings = Field(default_factory=SpeechSettings)
    asr: ASRSettings = Field(default_factory=ASRSettings)
    image_generation: ImageGenerationSettings = Field(default_factory=ImageGenerationSettings)
    emotion: EmotionSettings = Field(default_factory=EmotionSettings)
    relationships: RelationshipSettings = Field(default_factory=RelationshipSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    proactive: ProactiveSettings = Field(default_factory=ProactiveSettings)
    diary: DiarySettings = Field(default_factory=DiarySettings)

    @model_validator(mode="after")
    def distinct_ports(self):
        from urllib.parse import urlsplit

        if self.meme.managed and urlsplit(self.meme.base_url).port == self.web.port:
            raise ValueError("meme-generator 与 SoulBot 需要使用不同端口")
        if self.asr.qq_enabled and self.qq.connection != "reverse":
            raise ValueError(
                "QQ 语音识别当前需要反向 WebSocket；正向驱动的单帧限制可能中断大音频响应"
            )
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
    def __init__(self, root: Path, environment=None):
        from soulbot.services.environment_service import (
            REVERSE_WS,
            EnvironmentError,
            EnvironmentService,
        )

        self.environment = environment or EnvironmentService(root)
        self.path = root / "config.yaml"
        self.backup_path = root / "data" / "config.backup.yaml"
        self.lock = RLock()
        if not self.path.exists():
            atomic_write(self.path, self.dump(Settings()))
        self.current = self.read(self.path)
        file_values = self.environment.values()
        if REVERSE_WS in os.environ or REVERSE_WS in file_values:
            reverse = os.environ.get(REVERSE_WS, file_values.get(REVERSE_WS)) or ""
            data = self.current.model_dump()
            data["qq"]["reverse_ws_url"] = reverse
            try:
                self.current = Settings.model_validate(data)
            except ValueError:
                raise EnvironmentError(
                    "SOULBOT_ONEBOT_REVERSE_WS_URL 无效；请填写带接收路径的 ws:// 或 wss:// 地址，Token 使用独立配置"
                ) from None
        if REVERSE_WS not in file_values:
            self.environment.update({REVERSE_WS: self.current.qq.reverse_ws_url})

    @staticmethod
    def dump(settings):
        return yaml.safe_dump(settings.model_dump(), allow_unicode=True, sort_keys=False)

    @staticmethod
    def read(path):
        return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def save(self, settings: Settings):
        from soulbot.services.environment_service import REVERSE_WS, EnvironmentError

        with self.lock:
            override = os.environ.get(REVERSE_WS)
            if override is not None and settings.qq.reverse_ws_url != override:
                raise EnvironmentError("反向 WS 地址由进程环境变量管理，请修改该变量并重启")
            self.environment.update({REVERSE_WS: settings.qq.reverse_ws_url})
            atomic_write(self.backup_path, self.dump(self.current))
            atomic_write(self.path, self.dump(settings))
            self.current = settings.model_copy(deep=True)

    def restore(self):
        with self.lock:
            restored = self.read(self.backup_path)
            self.save(restored)
            return restored
