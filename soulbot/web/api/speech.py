import asyncio
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response
from pydantic import Field

from soulbot.core.events import ChatEvent
from soulbot.services.config_service import Model
from soulbot.services.media_service import MediaError
from soulbot.tools.media_tools import SynthesizeSpeech


class SendAudio(Model):
    confirm: Literal[True]
    audio_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def install_speech_api(app, s, authorized):
    protected = [Depends(authorized)]

    @app.get("/api/tts/status", dependencies=protected)
    async def status():
        return {
            "enabled": s.config.current.tts.enabled,
            "configured": s.speech.configured(),
            "provider": s.config.current.tts.provider,
            "active": len(s.speech.jobs),
            "usage": s.budget.summary(s.config.current)["tts"],
        }

    @app.get("/api/tts/voices", dependencies=protected)
    async def voices():
        settings = s.config.current.tts
        if settings.provider == "edge":
            return {
                "source": "edge_live",
                "items": await s.providers.resolve("tts", "edge").voices(),
            }
        return {
            "source": "manual",
            "items": [],
            "message": "兼容语音接口没有统一音色列表，请按服务商文档填写音色 ID",
        }

    @app.post("/api/tts/synthesize", dependencies=protected)
    async def synthesize(data: SynthesizeSpeech, request: Request):
        task = asyncio.create_task(s.speech.synthesize(data.text))

        async def disconnected():
            while True:
                await asyncio.sleep(0.25)
                if await request.is_disconnected():
                    return

        watcher = asyncio.create_task(disconnected())
        try:
            await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise HTTPException(499, "已停止等待语音；请在费用页核对可能产生的消耗")
            audio = await task
            item = await asyncio.to_thread(s.audio.save, audio)
            await asyncio.to_thread(s.db.audit, "tts_generated", s.config.current.tts.provider)
            return item
        finally:
            watcher.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)

    @app.get("/api/audio/{key}", dependencies=protected)
    async def audio(key: str):
        payload = await asyncio.to_thread(s.audio.read, key)
        return Response(
            payload.data,
            media_type=payload.mime,
            headers={"Content-Disposition": f'inline; filename="soulbot-{key}.{payload.format}"'},
        )

    @app.post("/api/audio/send-test", dependencies=protected)
    async def send(data: SendAudio):
        if not s.config.current.tts.enabled:
            raise MediaError("语音功能未启用")
        try:
            s.tools.permissions.require("synthesize_speech")
        except PermissionError:
            raise HTTPException(403, "语音工具权限未开放，已阻止发送") from None
        owner = s.config.current.personality.owner_qq
        if not owner:
            raise MediaError("请先在人格配置中填写主人 QQ 号")
        bots = s.qq_transport.connected() if s.qq_transport else []
        if not bots:
            raise HTTPException(503, "没有已连接的 QQ 机器人，请先连接 NapCat")
        payload = await asyncio.to_thread(s.audio.read, data.audio_id, "admin")
        now = time.time()
        with s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM metadata WHERE key='audio_owner_test_at'"
            ).fetchone()
            if row and now - float(row[0]) < 60:
                raise HTTPException(429, "语音发送测试每分钟最多一次")
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('audio_owner_test_at',?)", (str(now),)
            )
        event = ChatEvent(bots[0], owner, "web-audio-test", "", directed=True)
        if not await s.agent.executor.send_audio(event, payload, "admin"):
            raise HTTPException(
                502, "语音未获得平台成功回执；提示文字可能已发出，请检查 NapCat 音频转换与 QQ 权限"
            )
        return {"ok": True, "message": "平台已确认语音发送，请在 QQ 核对播放"}
