import asyncio
from typing import Any

from pydantic import Field, field_validator

from soulbot.core.permissions import PermissionEngine
from soulbot.services.config_service import Model
from soulbot.tools.html_render import CardRequest
from soulbot.tools.registry import Tool


class SearchEmoji(Model):
    query: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=12, ge=1, le=50)


class SynthesizeSpeech(Model):
    text: str = Field(min_length=1, max_length=2000)


class GenerateImage(Model):
    prompt: str = Field(min_length=1, max_length=4000)


class EmojiCategories(Model):
    query: str = Field(default="", max_length=100)
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=50, ge=1, le=100)


class BrowseEmoji(Model):
    category: str = Field(default="", max_length=120)
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=100, ge=1, le=200)


class SelectEmoji(Model):
    emoji_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class MemeKey(Model):
    template: str = Field(pattern=r"^[a-zA-Z0-9_\-]{1,100}$")


class MemeSearch(Model):
    query: str = Field(default="", max_length=100)
    offset: int = Field(default=0, ge=0, le=10000)


class GenerateMeme(MemeKey):
    texts: list[str] = Field(default_factory=list, max_length=20)
    images: list[str] = Field(default_factory=list, max_length=6)
    args: dict[str, Any] = Field(default_factory=dict)

    @field_validator("args")
    @classmethod
    def bounded_args(cls, values):
        import json

        if len(json.dumps(values, allow_nan=False).encode()) > 16384:
            raise ValueError("高级参数过大")
        return values

    @field_validator("texts")
    @classmethod
    def short_texts(cls, values):
        if any(len(value) > 500 for value in values):
            raise ValueError("每段文字最多 500 字")
        return values

    @field_validator("images")
    @classmethod
    def image_ids(cls, values):
        import re

        if any(not re.fullmatch(r"(?:emoji|media):[a-f0-9]{32}", value) for value in values):
            raise ValueError("图片只能使用已存在的 emoji:ID 或 media:ID")
        return values


class ToolPermissions(PermissionEngine):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.dynamic = {}

    def policy(self, name):
        if name.startswith("plugin_") and not self.config.current.plugins.enabled:
            return "deny"
        return self.dynamic.get(name, getattr(self.config.current.tools, name, "deny"))

    def require(self, name, *, approved=False):
        self.policies = self.config.current.tools.model_dump() | self.dynamic
        if name.startswith("plugin_") and not self.config.current.plugins.enabled:
            self.policies[name] = "deny"
        super().require(name, approved=approved)


def read_image(s, reference, scope=None):
    kind, key = reference.split(":", 1)
    return s.emoji.read(key) if kind == "emoji" else s.media.read(key, scope)


def register_media_tools(s):
    s.tools.permissions = ToolPermissions(s.config)

    def concise(result):
        return {
            "total": result["total"],
            "items": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "category": item["category"][:80],
                    "tags": item["tags"][:4],
                }
                for item in result["items"]
            ],
        }

    async def search(args):
        return concise(await asyncio.to_thread(s.emoji.search, args.query, args.limit))

    async def categories(args):
        return await asyncio.to_thread(s.emoji.categories, args.query, args.offset, args.limit)

    async def browse(args):
        return concise(
            await asyncio.to_thread(s.emoji.browse, args.offset, args.limit, args.category)
        )

    async def select(args):
        await asyncio.to_thread(s.emoji.read, args.emoji_id)
        return {
            "attachment": "emoji:" + args.emoji_id,
            "message": "已选择图片，只有发送执行器确认后才算发出",
        }

    async def templates(args):
        keys = await s.meme.client.keys()
        keys = [key for key in keys if args.query.casefold() in key.casefold()]
        return {
            "total": len(keys),
            "templates": keys[args.offset : args.offset + 20],
            "note": "模板 ID；生成前调用 get_meme_info 验证数量和参数",
        }

    async def info(args):
        value = await s.meme.client.info(args.template)
        return {
            "template": value["key"],
            "keywords": value["keywords"],
            "params_type": value["params_type"],
        }

    async def generate(args, context):
        images = [
            await asyncio.to_thread(read_image, s, reference, context.scope)
            for reference in args.images
        ]
        content = await s.meme.client.generate(args.template, args.texts, images, args.args)
        result = await asyncio.to_thread(s.media.save, content, "meme", context.scope or "admin")
        return {
            "attachment": "media:" + result["id"],
            "message": "图片已生成，只有发送执行器确认后才算发出",
        }

    async def render(args, context):
        provider = s.providers.resolve("html_render")
        content = await provider.render(args)
        result = await asyncio.to_thread(
            s.media.save, content, "html_render", context.scope or "admin"
        )
        return {
            "attachment": "media:" + result["id"],
            "message": "卡片已生成，只有发送执行器确认后才算发出",
        }

    async def speech(args, context):
        audio = await s.speech.synthesize(args.text, context.purpose)
        result = await asyncio.to_thread(s.audio.save, audio, "tts", context.scope or "admin")
        return {
            "attachment": "audio:" + result["id"],
            "message": "AI 合成语音已生成，需发送执行器确认才算发出",
        }

    async def image(args, context):
        generated = await s.images.generate(args.prompt, context.purpose)
        item = await asyncio.to_thread(
            s.media.save, generated.data, "image_generation", context.scope or "admin"
        )
        return {
            "attachment": "media:" + item["id"],
            "message": "AI 图片已生成；发送执行器确认后才算发出",
        }

    for name, description, schema, handler in [
        (
            "generate_image",
            "按简短描述生成一张 AI 图片；只填写提示词，不传文件或 URL。",
            GenerateImage,
            image,
        ),
        (
            "synthesize_speech",
            "将短文本合成为 AI 语音；只拟发送一次，不能冒充真人录音。",
            SynthesizeSpeech,
            speech,
        ),
        (
            "search_emoji",
            "按文件名、分类、标签检索本地表情，返回有限候选 ID。",
            SearchEmoji,
            search,
        ),
        (
            "list_emoji_categories",
            "先检索本地表情分类名称与数量，再用 browse_emoji 按分类分页挑选。支持空分类，offset 用于下一页分类。",
            EmojiCategories,
            categories,
        ),
        (
            "browse_emoji",
            "按 category 分类名称分页浏览表情列表。先用 list_emoji_categories 获取分类；不足时增加 offset，不遍历全库。",
            BrowseEmoji,
            browse,
        ),
        (
            "select_emoji",
            "只选择 search_emoji 或 browse_emoji 返回的本地 emoji_id。生成工具返回的 media 附件已经会发送，不要传给此工具；不能传路径或网址。",
            SelectEmoji,
            select,
        ),
        ("list_meme_templates", "检索/分页列举 meme 模板英文 ID。", MemeSearch, templates),
        ("get_meme_info", "读取模板图片/文字数量、默认文字和高级参数 Schema。", MemeKey, info),
        (
            "generate_meme",
            "使用模板及已知图片 ID 生成一张拟发送图片；不下载用户网址。",
            GenerateMeme,
            generate,
        ),
        (
            "render_html",
            "生成预定义文字、引用或清单卡片；只填结构化文本，不接受 HTML、脚本或资源网址。",
            CardRequest,
            render,
        ),
    ]:
        s.tools.register(
            Tool(
                name,
                description,
                schema,
                handler,
                timeout=620
                if name == "generate_image"
                else 150
                if name == "synthesize_speech"
                else 120
                if name in {"generate_meme", "render_html"}
                else 30,
                max_calls=1
                if name in {"synthesize_speech", "generate_image"}
                else 2
                if name in {"generate_meme", "render_html"}
                else 5,
                with_context=name
                in {"generate_meme", "render_html", "synthesize_speech", "generate_image"},
            )
        )


def specifications(s, event=None):
    config = s.config.current

    def compact(value, names=False):
        if isinstance(value, dict):
            return {
                key: compact(
                    item, not names and key in {"properties", "$defs", "patternProperties"}
                )
                for key, item in value.items()
                if names or key not in {"title", "default"}
            }
        if isinstance(value, list):
            return [compact(v) for v in value]
        return value

    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description
                + (
                    " 当前为本地 ComfyUI，请把用户画面要求准确转换为简洁英文视觉提示词再调用；不要只把中文原文传给模型，不要改变主体（猫与猫娘不同）。"
                    if tool.name == "generate_image"
                    and config.image_generation.provider == "comfyui"
                    and config.image_generation.comfy.tool_prompt_language == "en"
                    else ""
                ),
                "parameters": compact(tool.schema.model_json_schema()),
            },
        }
        for tool in s.tools.tools.values()
        if s.tools.permissions.policy(tool.name) in {"auto", "approval"}
        and (
            True
            if tool.name == "discover_tools" or tool.name.startswith("plugin_")
            else config.diary.enabled
            if tool.name == "create_diary"
            else config.qzone.enabled
            and bool(config.personality.owner_qq)
            and (
                event is None
                or (not event.group_id and event.user_id == config.personality.owner_qq)
            )
            if tool.name == "create_qzone_draft"
            else config.search.enabled
            if tool.name == "search_web"
            else config.memory.enabled
            if tool.name in {"retrieve_memory", "browse_memory", "save_memory"}
            else config.image_generation.enabled and s.images.configured()
            if tool.name == "generate_image"
            else config.tts.enabled and s.speech.configured()
            if tool.name == "synthesize_speech"
            else config.html_render.enabled and s.renderer.ready()
            if tool.name == "render_html"
            else config.emoji.enabled
            if tool.name.endswith("emoji") or tool.name == "list_emoji_categories"
            else config.meme.enabled
        )
    ]
