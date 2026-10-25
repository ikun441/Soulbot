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


class BrowseEmoji(Model):
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

    def require(self, name):
        self.policies = self.config.current.tools.model_dump()
        super().require(name)


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

    async def browse(args):
        return concise(await asyncio.to_thread(s.emoji.browse, args.offset, args.limit))

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

    for name, description, schema, handler in [
        (
            "search_emoji",
            "按文件名、分类、标签检索本地表情，返回有限候选 ID。",
            SearchEmoji,
            search,
        ),
        ("browse_emoji", "分页浏览本地表情，避免读取整个表情库。", BrowseEmoji, browse),
        (
            "select_emoji",
            "选择检索到的 emoji_id，作为本轮拟发送图片；不能传路径或网址。",
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
                timeout=120 if name in {"generate_meme", "render_html"} else 30,
                max_calls=2 if name in {"generate_meme", "render_html"} else 5,
                with_context=name in {"generate_meme", "render_html"},
            )
        )


def specifications(s):
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
                "description": tool.description,
                "parameters": compact(tool.schema.model_json_schema()),
            },
        }
        for tool in s.tools.tools.values()
        if getattr(config.tools, tool.name, "deny") == "auto"
        and (
            config.html_render.enabled and s.renderer.ready()
            if tool.name == "render_html"
            else config.emoji.enabled
            if tool.name.endswith("emoji")
            else config.meme.enabled
        )
    ]
