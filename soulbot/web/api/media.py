import asyncio
import time
from typing import Literal

from fastapi import Depends, Form, HTTPException, Query, Response, UploadFile
from pydantic import Field, field_validator

from soulbot.core.events import ChatEvent
from soulbot.services.config_service import Model
from soulbot.services.media_service import MAX_IMAGE_BYTES, MediaError
from soulbot.tools.media_tools import GenerateMeme, read_image, specifications


class Labels(Model):
    tags: list[str] = Field(default_factory=list, max_length=20)
    aliases: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tags", "aliases")
    @classmethod
    def valid_labels(cls, values):
        if any(not value.strip() or len(value) > 40 for value in values):
            raise ValueError("标签/别名不能为空，且不能超过 40 字")
        return list(dict.fromkeys(value.strip() for value in values))


class MemeAction(Model):
    action: Literal["install", "start", "stop"]


class SendImage(Model):
    confirm: Literal[True]
    image: str = Field(pattern=r"^(emoji|media):[a-f0-9]{32}$")


def install_media_api(app, s, authorized):
    protected = [Depends(authorized)]

    @app.get("/api/emoji", dependencies=protected)
    async def emoji(
        query: str = Query(default="", max_length=100),
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=100, ge=1, le=200),
    ):
        if query.strip():
            return await asyncio.to_thread(s.emoji.search, query, limit)
        return await asyncio.to_thread(s.emoji.browse, offset, limit)

    @app.post("/api/emoji/refresh", dependencies=protected)
    async def refresh():
        result = await asyncio.to_thread(s.emoji.refresh)
        await asyncio.to_thread(s.db.audit, "emoji_refreshed")
        return result

    @app.post("/api/emoji/upload", dependencies=protected)
    async def upload_emoji(file: UploadFile, category: str = Form(default="其他", max_length=40)):
        content = await file.read(MAX_IMAGE_BYTES + 1)
        item = await asyncio.to_thread(s.emoji.upload, file.filename, content, category)
        await asyncio.to_thread(s.db.audit, "emoji_uploaded")
        return item

    @app.put("/api/emoji/{key}/labels", dependencies=protected)
    async def labels(key: str, data: Labels):
        item = await asyncio.to_thread(s.emoji.update, key, data.tags, data.aliases)
        await asyncio.to_thread(s.db.audit, "emoji_labels_saved")
        return item

    @app.get("/api/emoji/{key}/image", dependencies=protected)
    async def emoji_image(key: str):
        image = await asyncio.to_thread(s.emoji.read, key)
        return Response(image.data, media_type=image.mime)

    @app.post("/api/media/upload", dependencies=protected)
    async def upload_image(file: UploadFile):
        content = await file.read(MAX_IMAGE_BYTES + 1)
        return await asyncio.to_thread(s.media.save, content, "upload")

    @app.get("/api/media/{key}", dependencies=protected)
    async def media(key: str):
        image = await asyncio.to_thread(s.media.read, key)
        return Response(image.data, media_type=image.mime)

    @app.get("/api/meme/status", dependencies=protected)
    async def status():
        return s.meme.status()

    @app.post("/api/meme/action", dependencies=protected)
    async def action(data: MemeAction):
        if data.action == "stop":
            if not s.config.current.meme.managed:
                raise MediaError("外部模式不能停止第三方进程")
            await s.meme.close()
        else:
            if data.action == "start" and not s.config.current.meme.enabled:
                raise MediaError("请先启用 meme-generator 并保存配置")
            if s.meme.task and not s.meme.task.done():
                if data.action == "start" or s.meme.status()["busy"]:
                    return s.meme.status()
                await s.meme.close()
            await s.meme.begin(
                data.action == "install"
                or (s.config.current.meme.auto_install and not s.meme.ready())
            )
        await asyncio.to_thread(s.db.audit, "meme_service_action", data.action)
        return s.meme.status()

    @app.post("/api/meme/test", dependencies=protected)
    async def test():
        return await s.meme.client.health()

    @app.get("/api/meme/templates", dependencies=protected)
    async def templates(
        query: str = Query(default="", max_length=100),
        offset: int = Query(default=0, ge=0, le=10000),
    ):
        values = await s.meme.client.keys()
        values = [key for key in values if query.casefold() in key.casefold()]
        return {"items": values[offset : offset + 60], "offset": offset, "total": len(values)}

    @app.get("/api/meme/templates/{key}", dependencies=protected)
    async def info(key: str):
        return await s.meme.client.info(key)

    @app.post("/api/meme/templates/{key}/preview", dependencies=protected)
    async def preview(key: str):
        content = await s.meme.client.preview(key)
        return await asyncio.to_thread(s.media.save, content, "meme_preview")

    @app.post("/api/meme/generate", dependencies=protected)
    async def generate(data: GenerateMeme):
        images = [await asyncio.to_thread(read_image, s, reference) for reference in data.images]
        content = await s.meme.client.generate(data.template, data.texts, images, data.args)
        item = await asyncio.to_thread(s.media.save, content, "meme")
        await asyncio.to_thread(s.db.audit, "meme_generated", data.template)
        return item

    @app.get("/api/tools", dependencies=protected)
    async def tools():
        return {
            "policies": s.config.current.tools.model_dump(),
            "available": specifications(s),
            "max_calls_per_task": 5,
            "approval_available": False,
        }

    @app.post("/api/media/send-test", dependencies=protected)
    async def send_image(data: SendImage):
        owner = s.config.current.personality.owner_qq
        if not owner:
            raise MediaError("请先在人格配置中填写主人 QQ 号")
        bots = s.qq_transport.connected() if s.qq_transport else []
        if not bots:
            raise HTTPException(503, "没有已连接的 QQ 机器人，请先连接 NapCat")
        image = await asyncio.to_thread(read_image, s, data.image)
        now = time.time()
        with s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM metadata WHERE key='media_owner_test_at'"
            ).fetchone()
            if row and now - float(row[0]) < 60:
                raise HTTPException(429, "图片测试每分钟最多一次")
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('media_owner_test_at',?)", (str(now),)
            )
        event = ChatEvent(bots[0], owner, "web-image-test", "", directed=True)
        if not await s.agent.executor.send_image(event, image, "admin"):
            raise HTTPException(502, "图片未获平台成功回执，请检查 QQ 登录和权限")
        return {"ok": True, "message": "平台已确认图片发送给主人，请在 QQ 核对接收"}
