"""Administrator-only QZone drafts and explicit publication; no model approval endpoint."""

from fastapi import Depends, HTTPException, Query

from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import ProviderError
from soulbot.services.qzone_service import DraftCreate, DraftEdit, DraftPublish, DraftReview


def install_qzone_api(app, s, authorized):
    protected = [Depends(authorized)]

    def sync(operation):
        try:
            return operation()
        except StateConflict:
            raise
        except LookupError:
            raise HTTPException(404, "空间草稿不存在") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    async def asynchronous(operation):
        try:
            return await operation()
        except (StateConflict, ProviderError):
            raise
        except LookupError:
            raise HTTPException(404, "空间草稿不存在") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/qzone/status", dependencies=protected)
    def status():
        return s.qzone.status()

    @app.post("/api/qzone/capability", dependencies=protected)
    async def capability(bot_id: str = Query(pattern=r"^[1-9][0-9]{4,11}$")):
        result = dict(await s.qzone.capability(bot_id))
        result.pop("connection", None)
        return result

    @app.get("/api/qzone/drafts", dependencies=protected)
    def listing(
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=30, ge=1, le=100),
    ):
        return s.qzone.listing(offset, limit)

    @app.post("/api/qzone/drafts", dependencies=protected)
    async def create(data: DraftCreate):
        return await asynchronous(lambda: s.qzone.create(data))

    @app.get("/api/qzone/drafts/{key}", dependencies=protected)
    def detail(key: int):
        return sync(lambda: s.qzone.get(key))

    @app.put("/api/qzone/drafts/{key}", dependencies=protected)
    def edit(key: int, data: DraftEdit):
        return sync(lambda: s.qzone.edit(key, data))

    @app.post("/api/qzone/drafts/{key}/review", dependencies=protected)
    def review(key: int, data: DraftReview):
        return sync(lambda: s.qzone.review(key, data))

    @app.post("/api/qzone/drafts/{key}/publish", dependencies=protected)
    async def publish(key: int, data: DraftPublish):
        return await asynchronous(lambda: s.qzone.publish(key, data))
