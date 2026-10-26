import asyncio
from dataclasses import asdict
from typing import Literal

from fastapi import Depends, HTTPException, Request, UploadFile

from soulbot.services.audio_service import MAX_AUDIO_BYTES
from soulbot.services.config_service import Model


class ASRAction(Model):
    action: Literal["prepare", "cancel"]


def install_asr_api(app, s, authorized):
    protected = [Depends(authorized)]

    @app.get("/api/asr/status", dependencies=protected)
    async def status():
        local = s.recognition.local
        return {
            "enabled": s.config.current.asr.enabled,
            "qq_enabled": s.config.current.asr.qq_enabled,
            "configured": s.recognition.configured(),
            "provider_test": s.recognition.test_status(),
            "provider": s.config.current.asr.provider,
            "local_ready": local.ready(),
            "preparing": bool(local.task and not local.task.done()),
            "message": local.message,
            "active": len(s.recognition.jobs),
            "usage": s.budget.summary(s.config.current)["asr"],
        }

    @app.post("/api/asr/action", dependencies=protected)
    async def action(data: ASRAction):
        local = s.recognition.local
        if data.action == "prepare":
            await local.begin_prepare()
        else:
            await local.cancel_prepare()
        s.db.audit("asr_model_action", data.action)
        return await status()

    @app.post("/api/asr/transcribe", dependencies=protected)
    async def transcribe(request: Request, file: UploadFile):
        try:
            content = await file.read(MAX_AUDIO_BYTES + 1)
        finally:
            await file.close()
        task = asyncio.create_task(s.recognition.transcribe(content))

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
                raise HTTPException(499, "转写已取消；云端服务可能已产生费用")
            result = await task
            s.db.audit("asr_completed", "web_test")
            return asdict(result)
        finally:
            watcher.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)
