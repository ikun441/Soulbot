import asyncio
from typing import Literal

from fastapi import Depends, HTTPException, Request

from soulbot.services.config_service import ComfySettings
from soulbot.tools.media_tools import GenerateImage


class ImagePreview(GenerateImage):
    provider_id: Literal["auto", "primary", "fallback_1", "fallback_2"] = "auto"


def install_image_api(app, s, authorized):
    protected = [Depends(authorized)]

    @app.get("/api/image/status", dependencies=protected)
    async def status():
        return {
            "enabled": s.config.current.image_generation.enabled,
            "configured": s.images.configured(),
            "provider_test": s.images.diagnostics(),
            "routes": s.images.router.status(),
            "active": len(s.images.jobs),
            "usage": s.budget.summary(s.config.current)["image_generation"],
        }

    @app.post("/api/image/comfyui/probe", dependencies=protected)
    async def probe(data: ComfySettings):
        return await s.providers.resolve("image_generation", "comfyui").probe(data)

    @app.post("/api/image/generate", dependencies=protected)
    async def generate(data: ImagePreview, request: Request):
        task = asyncio.create_task(
            s.images.generate(
                data.prompt, only=None if data.provider_id == "auto" else data.provider_id
            )
        )

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
                raise HTTPException(499, "已取消等待；生图服务可能已产生费用")
            image = await task
            artifact = await asyncio.to_thread(s.media.save, image.data, "image_generation")
            s.db.audit("image_generated", "web_test")
            return {**artifact, "provider_id": image.provider_id, "provider": image.provider}
        finally:
            watcher.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)
