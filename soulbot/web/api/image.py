import asyncio

from fastapi import Depends, HTTPException, Request

from soulbot.tools.media_tools import GenerateImage


def install_image_api(app, s, authorized):
    protected = [Depends(authorized)]

    @app.get("/api/image/status", dependencies=protected)
    async def status():
        return {
            "enabled": s.config.current.image_generation.enabled,
            "configured": s.images.configured(),
            "active": len(s.images.jobs),
            "usage": s.budget.summary(s.config.current)["image_generation"],
        }

    @app.post("/api/image/generate", dependencies=protected)
    async def generate(data: GenerateImage, request: Request):
        task = asyncio.create_task(s.images.generate(data.prompt))

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
            return artifact
        finally:
            watcher.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, watcher, return_exceptions=True)
