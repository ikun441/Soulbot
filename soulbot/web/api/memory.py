"""Administrator review and scope-bound previews for long-term memories."""

from typing import Literal

from fastapi import Depends, HTTPException, Query
from pydantic import Field

from soulbot.core.events import ChatEvent
from soulbot.core.memory import MemoryCreate, MemoryEdit
from soulbot.services.config_service import Model


class MemoryPreview(Model):
    bot_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    user_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    group_id: str = Field(default="", pattern=r"^([1-9][0-9]{4,11})?$")
    query: str = Field(default="", max_length=1000)


def install_memory_api(app, s, authorized):
    protected = [Depends(authorized)]

    def change(operation):
        try:
            return operation()
        except LookupError:
            raise HTTPException(404, "记忆不存在") from None
        except ValueError:
            raise HTTPException(422, "记忆参数不符合要求，请检查授权、有效期或候选数量") from None

    @app.get("/api/memories", dependencies=protected)
    def listing(
        query: str = Query(default="", max_length=80),
        status: Literal["all", "candidate", "approved", "rejected", "archived"] = "all",
        offset: int = Query(default=0, ge=0, le=1000000),
        limit: int = Query(default=30, ge=1, le=100),
    ):
        return s.memory.listing(query, status, offset, limit)

    @app.post("/api/memories", dependencies=protected)
    def create(data: MemoryCreate):
        return change(lambda: s.memory.create(data))

    @app.put("/api/memories/{key}", dependencies=protected)
    def edit(key: int, data: MemoryEdit):
        # StateConflict is a ValueError subtype: preserve the global 409 handler.
        from soulbot.core.social_state import StateConflict

        try:
            return s.memory.edit(key, data)
        except StateConflict:
            raise
        except LookupError:
            raise HTTPException(404, "记忆不存在") from None
        except ValueError:
            raise HTTPException(422, "请检查授权及有效期；过期记忆不能直接通过审核") from None

    @app.delete("/api/memories/{key}", dependencies=protected)
    def delete(key: int, revision: int = Query(ge=0)):
        try:
            s.memory.delete(key, revision)
        except LookupError:
            raise HTTPException(404, "记忆不存在") from None
        return {"ok": True}

    @app.post("/api/memories/preview", dependencies=protected)
    def preview(data: MemoryPreview):
        event = ChatEvent(
            data.bot_id, data.user_id, "memory-preview", data.query, group_id=data.group_id or None
        )
        return {
            "enabled": s.config.current.memory.enabled,
            "permitted": s.memory.permitted(event),
            "items": s.memory.retrieve(event),
        }
